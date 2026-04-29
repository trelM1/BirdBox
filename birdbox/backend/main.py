from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx, os, re, json
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional
import anthropic

load_dotenv()

app = FastAPI()

MAPS_KEY       = os.getenv("GOOGLE_MAPS_API_KEY")
ELEVEN_KEY     = os.getenv("ELEVENLABS_API_KEY")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Snowflake connection ──────────────────────────────────────
def get_snowflake():
    try:
        import snowflake.connector
        conn = snowflake.connector.connect(
            account   = os.getenv("SNOWFLAKE_ACCOUNT"),
            user      = os.getenv("SNOWFLAKE_USER"),
            password  = os.getenv("SNOWFLAKE_PASSWORD"),
            warehouse = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
            database  = os.getenv("SNOWFLAKE_DATABASE",  "SMARTCANE_DB"),
            schema    = os.getenv("SNOWFLAKE_SCHEMA",    "PUBLIC"),
        )
        return conn
    except Exception as e:
        print(f"Snowflake connection error: {e}")
        return None

def log_to_snowflake(table: str, data: dict):
    """Fire-and-forget Snowflake insert — won't crash the app if it fails."""
    try:
        conn = get_snowflake()
        if not conn: return
        cur = conn.cursor()
        if table == "OBSTACLE_EVENTS":
            cur.execute(
                "INSERT INTO OBSTACLE_EVENTS (lat, lng, level, message) VALUES (%s, %s, %s, %s)",
                (data.get("lat"), data.get("lng"), data.get("level"), data.get("message"))
            )
        elif table == "VOICE_COMMANDS":
            cur.execute(
                "INSERT INTO VOICE_COMMANDS (user_text, ai_response) VALUES (%s, %s)",
                (data.get("user_text"), data.get("ai_response"))
            )
        elif table == "LOCATION_PINGS":
            cur.execute(
                "INSERT INTO LOCATION_PINGS (lat, lng, address) VALUES (%s, %s, %s)",
                (data.get("lat"), data.get("lng"), data.get("address", ""))
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Snowflake log error: {e}")


# ── WebSocket manager (dashboard) ────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        for ws in self.active:
            try:
                await ws.send_json(data)
            except:
                pass

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
    address = "Unknown location"
    if data["status"] == "OK":
        address = data["results"][0]["formatted_address"]
        # log to Snowflake
        log_to_snowflake("LOCATION_PINGS", {"lat": lat, "lng": lng, "address": address})
        return {"address": address}
    return {"address": address, "error": data["status"]}


# ── 2. Nearby places ─────────────────────────────────────────
@app.get("/places/nearby")
async def nearby_places(lat: float, lng: float, query: str = Query(...)):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": query, "location": f"{lat},{lng}", "radius": 2000, "key": MAPS_KEY}
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
async def get_directions(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float):
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
    leg = data["routes"][0]["legs"][0]
    steps = []
    for s in leg["steps"]:
        steps.append({
            "instruction": re.sub(r"<[^>]+>", "", s["html_instructions"]),
            "distance":    s["distance"]["text"],
            "duration":    s["duration"]["text"],
        })
    return {
        "total_distance": leg["distance"]["text"],
        "total_duration": leg["duration"]["text"],
        "steps": steps
    }


# ── 4. Claude Vision — obstacle detection ────────────────────
class VisionRequest(BaseModel):
    image: str  # base64 jpeg

@app.post("/analyze")
async def analyze_image(req: VisionRequest):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image}
                },
                {
                    "type": "text",
                    "text": """You are the obstacle detection module for a smart cane used by a visually impaired person.
Analyze this camera frame. Respond with ONLY a JSON object (no markdown):
{"level": "safe" | "warning" | "urgent", "message": "one short sentence max 12 words"}
- urgent: stairs, vehicle, fast-moving person, glass door, low ceiling, traffic light
- warning: person nearby, door, bicycle, dog, road ahead, crosswalk
- safe: clear path ahead"""
                }
            ]
        }]
    )
    raw = message.content[0].text.strip()
    result = json.loads(raw.replace("```json", "").replace("```", "").strip())
    return result


# ── 5. Claude Chat — voice assistant ─────────────────────────
class ChatRequest(BaseModel):
    message: str
    location: Optional[dict] = None

@app.post("/chat")
async def chat(req: ChatRequest):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    system = """You are SmartCane, a voice assistant for a visually impaired user walking outdoors.
Be very concise — 1-2 sentences max. Help with navigation, obstacles, and general questions.
The user is walking so keep responses short and clear."""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=120,
        system=system,
        messages=[{"role": "user", "content": req.message}]
    )
    reply = message.content[0].text.strip()

    # log to Snowflake
    log_to_snowflake("VOICE_COMMANDS", {
        "user_text":   req.message,
        "ai_response": reply
    })

    return {"reply": reply}


# ── 6. ElevenLabs TTS ────────────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    api_key: str = ""
    voice_id: str = "Rachel"

@app.post("/tts")
async def text_to_speech(req: TTSRequest):
    key = req.api_key or ELEVEN_KEY
    voice_map = {
        "Rachel": "21m00Tcm4TlvDq8ikWAM",
        "Adam":   "pNInz6obpgDQGcFmaJgB",
        "Bella":  "EXAVITQu4vr4xnSDxMaL",
    }
    vid = voice_map.get(req.voice_id, req.voice_id)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}/stream",
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={"text": req.text, "model_id": "eleven_turbo_v2",
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
            timeout=15.0
        )
    if r.status_code != 200:
        return {"error": f"ElevenLabs {r.status_code}"}
    return StreamingResponse(iter([r.content]), media_type="audio/mpeg")


# ── 7. Location update → dashboard + Snowflake ───────────────
class LocationUpdate(BaseModel):
    lat:     float
    lng:     float
    level:   str = "safe"
    message: str = ""

@app.post("/location/update")
async def location_update(update: LocationUpdate):
    # broadcast to dashboard
    await manager.broadcast({
        "type":    "location",
        "lat":     update.lat,
        "lng":     update.lng,
        "level":   update.level,
        "message": update.message,
    })
    # log obstacle events to Snowflake (skip plain location pings)
    if update.message and update.message != "Location acquired":
        log_to_snowflake("OBSTACLE_EVENTS", {
            "lat":     update.lat,
            "lng":     update.lng,
            "level":   update.level,
            "message": update.message,
        })
    return {"ok": True}


# ── 8. Analytics endpoint (for dashboard) ────────────────────
@app.get("/analytics")
async def get_analytics():
    try:
        conn = get_snowflake()
        if not conn:
            return {"error": "Snowflake not connected"}
        cur = conn.cursor()

        # total events by level
        cur.execute("""
            SELECT level, COUNT(*) as count
            FROM OBSTACLE_EVENTS
            GROUP BY level
            ORDER BY count DESC
        """)
        level_counts = {row[0]: row[1] for row in cur.fetchall()}

        # most recent 10 events
        cur.execute("""
            SELECT timestamp, level, message, lat, lng
            FROM OBSTACLE_EVENTS
            ORDER BY timestamp DESC
            LIMIT 10
        """)
        recent = [
            {"timestamp": str(r[0]), "level": r[1], "message": r[2], "lat": r[3], "lng": r[4]}
            for r in cur.fetchall()
        ]

        # total voice commands
        cur.execute("SELECT COUNT(*) FROM VOICE_COMMANDS")
        voice_count = cur.fetchone()[0]

        # most common obstacles
        cur.execute("""
            SELECT message, COUNT(*) as cnt
            FROM OBSTACLE_EVENTS
            WHERE level != 'safe'
            GROUP BY message
            ORDER BY cnt DESC
            LIMIT 5
        """)
        top_obstacles = [{"message": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.close()
        conn.close()

        return {
            "level_counts":   level_counts,
            "recent_events":  recent,
            "voice_commands": voice_count,
            "top_obstacles":  top_obstacles,
        }
    except Exception as e:
        return {"error": str(e)}


# ── 9. WebSocket (dashboard) ─────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
