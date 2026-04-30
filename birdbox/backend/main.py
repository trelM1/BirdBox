from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx, os, re, json, math
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional
import anthropic

load_dotenv()

app = FastAPI()

MAPS_KEY      = os.getenv("GOOGLE_MAPS_API_KEY")
ELEVEN_KEY    = os.getenv("ELEVENLABS_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Snowflake ─────────────────────────────────────────────────
def get_snowflake():
    try:
        import snowflake.connector
        return snowflake.connector.connect(
            account   = os.getenv("SNOWFLAKE_ACCOUNT"),
            user      = os.getenv("SNOWFLAKE_USER"),
            password  = os.getenv("SNOWFLAKE_PASSWORD"),
            warehouse = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
            database  = os.getenv("SNOWFLAKE_DATABASE",  "SMARTCANE_DB"),
            schema    = os.getenv("SNOWFLAKE_SCHEMA",    "PUBLIC"),
        )
    except Exception as e:
        print(f"Snowflake error: {e}")
        return None

def log_to_snowflake(table: str, data: dict):
    import threading
    def _log():
        try:
            conn = get_snowflake()
            if not conn: return
            cur = conn.cursor()
            if table == "OBSTACLE_EVENTS":
                cur.execute(
                    "INSERT INTO OBSTACLE_EVENTS (lat, lng, level, message) VALUES (%s,%s,%s,%s)",
                    (data.get("lat"), data.get("lng"), data.get("level"), data.get("message"))
                )
            elif table == "VOICE_COMMANDS":
                cur.execute(
                    "INSERT INTO VOICE_COMMANDS (user_text, ai_response) VALUES (%s,%s)",
                    (data.get("user_text"), data.get("ai_response"))
                )
            elif table == "LOCATION_PINGS":
                cur.execute(
                    "INSERT INTO LOCATION_PINGS (lat, lng, address) VALUES (%s,%s,%s)",
                    (data.get("lat"), data.get("lng"), data.get("address", ""))
                )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"Snowflake log error: {e}")
    threading.Thread(target=_log, daemon=True).start()

# ── WebSocket manager ─────────────────────────────────────────
class ConnectionManager:
    def __init__(self): self.active: List[WebSocket] = []
    async def connect(self, ws):
        await ws.accept(); self.active.append(ws)
    def disconnect(self, ws):
        if ws in self.active: self.active.remove(ws)
    async def broadcast(self, data):
        for ws in self.active:
            try: await ws.send_json(data)
            except: pass

manager = ConnectionManager()

# ── Distance helpers ──────────────────────────────────────────
def haversine(lat1, lng1, lat2, lng2) -> float:
    """Returns distance in meters between two GPS coords."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlng  = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlng/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def bearing(lat1, lng1, lat2, lng2) -> str:
    """Returns cardinal direction from point 1 to point 2."""
    dLng = math.radians(lng2 - lng1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dLng) * math.cos(lat2r)
    y = math.cos(lat1r)*math.sin(lat2r) - math.sin(lat1r)*math.cos(lat2r)*math.cos(dLng)
    b = (math.degrees(math.atan2(x, y)) + 360) % 360
    dirs = ["north","northeast","east","southeast","south","southwest","west","northwest"]
    return dirs[round(b / 45) % 8]

def format_distance(meters: float) -> str:
    if meters < 1000:
        return f"{round(meters)} meters"
    return f"{round(meters/1000, 1)} km"


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
        address = data["results"][0]["formatted_address"]
        log_to_snowflake("LOCATION_PINGS", {"lat": lat, "lng": lng, "address": address})
        return {"address": address}
    return {"address": "Unknown location", "error": data["status"]}


# ── 2. Nearby places with distance + direction ────────────────
@app.get("/places/nearby")
async def nearby_places(
    lat: float, lng: float,
    query: str = Query(...)
):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": query, "location": f"{lat},{lng}", "radius": 2000, "key": MAPS_KEY}
        )
    data = r.json()
    results = []
    for p in data.get("results", [])[:3]:
        loc  = p["geometry"]["location"]
        dist = haversine(lat, lng, loc["lat"], loc["lng"])
        dir_ = bearing(lat, lng, loc["lat"], loc["lng"])
        results.append({
            "name":      p["name"],
            "address":   p.get("vicinity") or p.get("formatted_address", ""),
            "lat":       loc["lat"],
            "lng":       loc["lng"],
            "place_id":  p["place_id"],
            "distance":  round(dist),
            "distance_text": format_distance(dist),
            "direction": dir_,
        })
    # sort by distance
    results.sort(key=lambda x: x["distance"])
    return {"places": results}


# ── 3. Directions (walking, metric) ──────────────────────────
@app.get("/directions")
async def get_directions(
    origin_lat: float, origin_lng: float,
    dest_lat: float,   dest_lng: float
):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params={
                "origin":      f"{origin_lat},{origin_lng}",
                "destination": f"{dest_lat},{dest_lng}",
                "mode":        "walking",
                "units":       "metric",
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
            "instruction":  instruction,
            "distance_m":   s["distance"]["value"],       # meters (for GPS tracking)
            "distance_text": s["distance"]["text"],
            "duration_text": s["duration"]["text"],
            "end_lat":      s["end_location"]["lat"],
            "end_lng":      s["end_location"]["lng"],
        })
    return {
        "total_distance": leg["distance"]["text"],
        "total_duration": leg["duration"]["text"],
        "dest_lat":       dest_lat,
        "dest_lng":       dest_lng,
        "steps":          steps
    }


# ── 4. Navigate — full flow: search + speak options ───────────
class NavigateRequest(BaseModel):
    query:    str
    user_lat: float
    user_lng: float

@app.post("/navigate")
async def navigate(req: NavigateRequest):
    """Returns places with spoken introduction ready for TTS."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": req.query, "location": f"{req.user_lat},{req.user_lng}", "radius": 2000, "key": MAPS_KEY}
        )
    data = r.json()
    places = []
    for p in data.get("results", [])[:3]:
        loc  = p["geometry"]["location"]
        dist = haversine(req.user_lat, req.user_lng, loc["lat"], loc["lng"])
        dir_ = bearing(req.user_lat, req.user_lng, loc["lat"], loc["lng"])
        places.append({
            "name":          p["name"],
            "address":       p.get("vicinity") or p.get("formatted_address", ""),
            "lat":           loc["lat"],
            "lng":           loc["lng"],
            "distance":      round(dist),
            "distance_text": format_distance(dist),
            "direction":     dir_,
        })
    places.sort(key=lambda x: x["distance"])

    if not places:
        return {"places": [], "speech": f"Sorry, I couldn't find any {req.query} nearby."}

    # build spoken response
    parts = [f"I found {len(places)} option{'s' if len(places)>1 else ''}."]
    for i, p in enumerate(places):
        parts.append(f"Option {i+1}: {p['name']}, {p['distance_text']} to the {p['direction']}.")
    parts.append("Which one would you like?")
    speech = " ".join(parts)

    return {"places": places, "speech": speech}


# ── 5. Claude Vision ──────────────────────────────────────────
class VisionRequest(BaseModel):
    image: str

@app.post("/analyze")
async def analyze_image(req: VisionRequest):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image}},
            {"type": "text", "text": """Obstacle detection for a smart cane. Analyze this camera frame.
Respond ONLY with JSON (no markdown):
{"level": "safe"|"warning"|"urgent", "message": "max 10 words"}
- urgent: stairs, vehicle moving toward you, glass door, low ceiling, road crossing, large object blocking path
- warning: object close in path, bicycle, dog, curb, construction
- safe: clear path, people standing still nearby (bystanders are NOT obstacles), open space
Important: people simply walking nearby or standing around are NOT obstacles — only warn if someone is directly blocking the path or rushing toward the user."""}
        ]}]
    )
    raw = message.content[0].text.strip()
    return json.loads(raw.replace("```json","").replace("```","").strip())


# ── 6. Claude Chat ────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:    str
    location:   Optional[dict] = None
    log_only:   bool = False
    ai_response: Optional[str] = None

@app.post("/chat")
async def chat(req: ChatRequest):
    # log-only mode: frontend already has the reply, just log it
    if req.log_only and req.ai_response:
        log_to_snowflake("VOICE_COMMANDS", {"user_text": req.message, "ai_response": req.ai_response})
        return {"ok": True}

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    location_str = ""
    if req.location:
        loc  = dict(req.location)
        addr = loc.get("address")
        lat  = loc.get("lat")
        lng  = loc.get("lng")
        if addr and addr != "Unknown location":
            location_str = f"\nUser's current location: {addr} (coordinates: {lat}, {lng})"
        elif lat and lng:
            location_str = f"\nUser's current GPS coordinates: {lat}, {lng}"
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=f"""You are BirdBox, a voice assistant for a visually impaired user walking outdoors.
Be very concise — 1-2 sentences max. Help with navigation, obstacles, and general questions.{location_str}""",
        messages=[{"role": "user", "content": req.message}]
    )
    reply = message.content[0].text.strip()
    log_to_snowflake("VOICE_COMMANDS", {"user_text": req.message, "ai_response": reply})
    return {"reply": reply}


# ── 7. ElevenLabs TTS ─────────────────────────────────────────
class TTSRequest(BaseModel):
    text:     str
    api_key:  str = ""
    voice_id: str = "Rachel"

@app.post("/tts")
async def text_to_speech(req: TTSRequest):
    key = req.api_key or ELEVEN_KEY
    voice_map = {"Rachel":"21m00Tcm4TlvDq8ikWAM","Adam":"pNInz6obpgDQGcFmaJgB","Bella":"EXAVITQu4vr4xnSDxMaL"}
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


# ── 8. Location update → dashboard ───────────────────────────
class LocationUpdate(BaseModel):
    lat:     float
    lng:     float
    level:   str = "safe"
    message: str = ""

@app.post("/location/update")
async def location_update(update: LocationUpdate):
    await manager.broadcast({
        "type": "location", "lat": update.lat, "lng": update.lng,
        "level": update.level, "message": update.message,
    })
    if update.message and update.message != "Location acquired":
        log_to_snowflake("OBSTACLE_EVENTS", {
            "lat": update.lat, "lng": update.lng,
            "level": update.level, "message": update.message,
        })
    return {"ok": True}


# ── 9. Analytics ──────────────────────────────────────────────
@app.get("/analytics")
async def get_analytics():
    try:
        conn = get_snowflake()
        if not conn: return {"error": "Snowflake not connected"}
        cur = conn.cursor()
        cur.execute("SELECT level, COUNT(*) FROM OBSTACLE_EVENTS GROUP BY level ORDER BY COUNT(*) DESC")
        level_counts = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT timestamp, level, message, lat, lng FROM OBSTACLE_EVENTS ORDER BY timestamp DESC LIMIT 10")
        recent = [{"timestamp":str(r[0]),"level":r[1],"message":r[2],"lat":r[3],"lng":r[4]} for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) FROM VOICE_COMMANDS")
        voice_count = cur.fetchone()[0]
        cur.execute("SELECT message, COUNT(*) as cnt FROM OBSTACLE_EVENTS WHERE level != 'safe' GROUP BY message ORDER BY cnt DESC LIMIT 5")
        top_obstacles = [{"message":r[0],"count":r[1]} for r in cur.fetchall()]
        cur.close(); conn.close()
        return {"level_counts":level_counts,"recent_events":recent,"voice_commands":voice_count,"top_obstacles":top_obstacles}
    except Exception as e:
        return {"error": str(e)}


# ── 10. WebSocket ─────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ── 11. Config endpoint (serves keys to frontend securely) ────
@app.get("/config")
async def get_config():
    return {
        "eleven_key":    ELEVEN_KEY    or "",
        "anthropic_key": ANTHROPIC_KEY or "",
    }