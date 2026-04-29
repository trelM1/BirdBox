from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx, os, re, json
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List

load_dotenv()

app = FastAPI()
MAPS_KEY   = os.getenv("GOOGLE_MAPS_API_KEY")
ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── WebSocket connection manager (for dashboard) ──────────────
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:  # ADD SAFETY CHECK
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        disconnected = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                disconnected.append(ws)  # COLLECT DEAD CONNECTIONS
        
        # CLEANUP DEAD CONNECTIONS
        for ws in disconnected:
            self.disconnect(ws)

manager = ConnectionManager()

# ── 1. Reverse geocode ────────────────────────────────────────
@app.get("/location/address")
async def get_address(lat: float, lng: float):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lng}", "key": MAPS_KEY}
        )
    data = r.json()
    if data["status"] == "OK":
        return {"address": data["results"][0]["formatted_address"]}
    return {"address": "Unknown location", "error": data["status"]}

@app.post("/location/update")
async def location_update(update: LocationUpdate):
    print(f"📍 Location update: {update.lat}, {update.lng} ({update.level})")  # DEBUG
    await manager.broadcast({  # ← Uses the manager instance above
        "type":     "location",
        "lat":      update.lat,
        "lng":      update.lng,
        "level":    update.level,
        "message":  update.message,
        "timestamp": update.dict()
    })
    return {"ok": True, "connections": len(manager.active)}

# ── 2. Nearby places ─────────────────────────────────────────
@app.get("/places/nearby")
async def nearby_places(
    lat: float,
    lng: float,
    query: str = Query(..., description="e.g. Starbucks, pharmacy")
):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={
                "query": query,
                "location": f"{lat},{lng}",
                "radius": 2000,
                "key": MAPS_KEY
            }
        )
    data = r.json()
    results = []
    for p in data.get("results", [])[:3]:
        loc = p["geometry"]["location"]
        results.append({
            "name":     p["name"],
            "address":  p.get("vicinity") or p.get("formatted_address", ""),
            "lat":      loc["lat"],
            "lng":      loc["lng"],
            "place_id": p["place_id"],
        })
    return {"places": results}


# ── 3. Directions ────────────────────────────────────────────
@app.get("/directions")
async def get_directions(
    origin_lat: float,
    origin_lng: float,
    dest_lat:   float,
    dest_lng:   float,
):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params={
                "origin":      f"{origin_lat},{origin_lng}",
                "destination": f"{dest_lat},{dest_lng}",
                "mode":        "walking",
                "key":         MAPS_KEY
            }
        )
    data = r.json()
    if data["status"] != "OK":
        return {"steps": [], "error": data["status"]}

    leg   = data["routes"][0]["legs"][0]
    steps = []
    for s in leg["steps"]:
        instruction = re.sub(r"<[^>]+>", "", s["html_instructions"])
        steps.append({
            "instruction": instruction,
            "distance":    s["distance"]["text"],
            "duration":    s["duration"]["text"],
        })
    return {
        "total_distance": leg["distance"]["text"],
        "total_duration": leg["duration"]["text"],
        "steps": steps
    }


# ── 4. ElevenLabs TTS ────────────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    api_key: str = ""          # sent from frontend, falls back to .env
    voice_id: str = "Rachel"   # default ElevenLabs voice

@app.post("/tts")
async def text_to_speech(req: TTSRequest):
    key      = req.api_key or ELEVEN_KEY
    voice_id = req.voice_id

    # resolve voice name → ID if needed
    voice_map = {
        "Rachel": "21m00Tcm4TlvDq8ikWAM",
        "Adam":   "pNInz6obpgDQGcFmaJgB",
        "Bella":  "EXAVITQu4vr4xnSDxMaL",
        "Antoni": "ErXwobaYiN019PkySvjV",
    }
    if voice_id in voice_map:
        voice_id = voice_map[voice_id]

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
            headers={
                "xi-api-key":   key,
                "Content-Type": "application/json",
            },
            json={
                "text":     req.text,
                "model_id": "eleven_turbo_v2",   # fastest model
                "voice_settings": {
                    "stability":        0.5,
                    "similarity_boost": 0.75
                }
            },
            timeout=15.0
        )

    if r.status_code != 200:
        return {"error": f"ElevenLabs error {r.status_code}"}

    # stream audio back to browser
    return StreamingResponse(
        iter([r.content]),
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline; filename=speech.mp3"}
    )


# ── 5. Location update (phone → dashboard via WebSocket) ─────
class LocationUpdate(BaseModel):
    lat:      float
    lng:      float
    level:    str = "safe"     # safe | warning | urgent
    message:  str = ""

@app.post("/test/location")
async def test_location():
    """Test endpoint to simulate phone location updates"""
    await manager.broadcast({
        "type": "location",
        "lat": 40.7128,
        "lng": -74.0060,
        "level": "urgent",
        "message": "Test location update!"
    })
    return {"status": "broadcasted", "active_connections": len(manager.active)}


# ── 6. WebSocket endpoint (dashboard connects here) ──────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()   # keep alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:  # ADD THIS
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)