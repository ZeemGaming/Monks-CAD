"""
ER:LC API Server & SSH Bridge Gateway
API-only backend for CAD state, ER:LC proxying, WebSockets, and remote shell control.
"""
import os
import sys
import logging
import subprocess
import asyncio
import httpx
import aiohttp
import uvicorn
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Security, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("API-Server")

ERLC_API_BASE = "https://api.erlc.gg/v2"
PRC_API_KEY = os.getenv("PRC_API_KEY")
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")

security = HTTPBearer(auto_error=False)

async def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    if BRIDGE_API_KEY:
        if not credentials or credentials.credentials != BRIDGE_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key.")
    return True

# --- Async Background Tasks & Lifespan ---

async def poll_server():
    """Polls ER:LC server data every 30 seconds to maintain CAD state sync."""
    while True:
        try:
            await asyncio.sleep(30)
            await sync_from_server()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Poll error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log public IP on startup
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org") as resp:
                if resp.status == 200:
                    ip = await resp.text()
                    logger.info(f"=== API SERVER PUBLIC IP: {ip} ===")
                else:
                    logger.warning("Failed to retrieve public IP address.")
    except Exception as e:
        logger.error(f"Error fetching IP: {e}")

    # Start background polling task
    poll_task = asyncio.create_task(poll_server())

    yield  # API Server runs here

    # Shutdown sequence
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    await erc_client.close()

app = FastAPI(
    title="ER:LC Dedicated API Gateway",
    description="Backend API engine for CAD state, ER:LC proxying, and WebSocket feeds.",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- In-Memory CAD State ---

class CADState:
    def __init__(self):
        self.calls: List[Dict] = []
        self.units: Dict[str, Dict] = {}
        self.officers: Dict[str, Dict] = {}
        self.last_server_fetch = None
        self.websocket_clients: List[WebSocket] = []

    async def broadcast(self, event: str, data: Any):
        message = {"event": event, "data": data, "timestamp": datetime.now().isoformat()}
        disconnected = []
        for ws in self.websocket_clients:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            if ws in self.websocket_clients:
                self.websocket_clients.remove(ws)

cad_state = CADState()

# --- ER:LC API Client ---

class ERCClient:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10.0)

    def _get_headers(self, custom_key: Optional[str] = None):
        key = custom_key or PRC_API_KEY or os.getenv("ERLC_SERVER_KEY") or os.getenv("ERLC_API_KEY")
        if not key:
            raise HTTPException(status_code=400, detail="No ER:LC API key available.")
        return {"server-key": key}

    async def get_server_info(self, include_players=True, include_emergency=True, include_vehicles=True, custom_key=None):
        headers = self._get_headers(custom_key)
        params = {
            "Players": "true" if include_players else "false",
            "EmergencyCalls": "true" if include_emergency else "false",
            "Vehicles": "true" if include_vehicles else "false",
            "Staff": "true",
            "KillLogs": "true",
            "CommandLogs": "true",
            "ModCalls": "true",
            "JoinLogs": "true",
            "Queue": "true"
        }
        resp = await self.client.get(f"{ERLC_API_BASE}/server", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def run_command(self, command: str, custom_key: Optional[str] = None):
        headers = {**self._get_headers(custom_key), "Content-Type": "application/json"}
        resp = await self.client.post(
            f"{ERLC_API_BASE}/server/command",
            headers=headers,
            json={"command": command}
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.client.aclose()

erc_client = ERCClient()

# --- Pydantic Data Models ---

class CallCreate(BaseModel):
    call_number: int
    team: str
    caller: str
    position: List[float]
    description: str
    position_descriptor: str
    priority: int = 1
    status: str = "pending"
    assigned_units: List[str] = []

class UnitUpdate(BaseModel):
    unit_id: str
    callsign: str
    officer_name: str
    status: str
    location: Optional[List[float]] = None

class OfficerUpdate(BaseModel):
    officer_id: str
    name: str
    callsign: str
    rank: str
    status: str
    unit_id: Optional[str] = None

class CommandRequest(BaseModel):
    command: str

class ERLCCommandRequest(BaseModel):
    command: str
    server_key: Optional[str] = None

class SSHCommand(BaseModel):
    command: str
    timeout: Optional[int] = 30

class DirectoryRequest(BaseModel):
    path: Optional[str] = "."

# --- Health & Base Endpoints ---

@app.get("/")
@app.get("/api/health")
async def health():
    return {
        "status": "online",
        "service": "Dedicated API Server",
        "authenticated": bool(BRIDGE_API_KEY)
    }

@app.get("/api/system/info", dependencies=[Depends(verify_api_key)])
async def system_info():
    return {
        "cwd": os.getcwd(),
        "user": os.getenv("USER", "unknown"),
        "python_version": sys.version,
        "port": os.getenv("PORT", "8000")
    }

# --- ER:LC Proxy & Sync Endpoints ---

@app.post("/api/command")
async def run_command_cad(cmd: CommandRequest):
    try:
        result = await erc_client.run_command(cmd.command)
        return result
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)

@app.post("/api/erlc/command", dependencies=[Depends(verify_api_key)])
async def execute_erlc_command(payload: ERLCCommandRequest):
    key = payload.server_key or PRC_API_KEY or os.getenv("ERLC_SERVER_KEY") or os.getenv("ERLC_API_KEY")
    if not key:
        raise HTTPException(status_code=400, detail="No ER:LC Server Key provided or found in environment variables.")

    url = f"{ERLC_API_BASE}/server/command"
    headers = {"server-key": key, "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={"command": payload.command}, headers=headers) as resp:
            text = await resp.text()
            return {"status_code": resp.status, "response": text}

@app.get("/api/server/status")
async def server_status():
    try:
        data = await erc_client.get_server_info()
        cad_state.last_server_fetch = datetime.now()
        return data
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"ER:LC API error: {e.response.text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/sync")
async def sync_from_server():
    try:
        data = await erc_client.get_server_info()
        cad_state.last_server_fetch = datetime.now()

        new_calls = []
        raw_calls = data.get("EmergencyCalls", [])

        for index, ec in enumerate(raw_calls):
            call_number = ec.get("CallNumber") or ec.get("ID") or (index + 1000)
            existing = next((c for c in cad_state.calls if c["call_number"] == call_number), None)
            
            caller_name = str(ec.get("Caller") or ec.get("Player") or "911 Civilian")
            description = ec.get("Description") or ec.get("Call") or ec.get("Details") or "Emergency Call Received"
            location_desc = ec.get("PositionDescriptor") or ec.get("Location") or "Unknown Location"
            position = ec.get("Position") or [0, 0]

            call_data = {
                "call_number": call_number,
                "team": ec.get("Team", "Police"),
                "caller": caller_name,
                "position": position,
                "description": description,
                "position_descriptor": location_desc,
                "priority": ec.get("Priority", 1),
                "status": "pending",
                "assigned_units": ec.get("AssignedUnits", []),
                "created_at": datetime.fromtimestamp(ec.get("StartedAt", 0)).isoformat() if ec.get("StartedAt") else datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "source": "erlc_api"
            }

            if not existing:
                cad_state.calls.append(call_data)
                new_calls.append(call_data)
            else:
                existing.update(call_data)

        for player in data.get("Players", []):
            team = player.get("Team", "")
            if team in ["Police", "Sheriff", "State Police", "Fire", "EMS", "DOT"]:
                callsign = player.get("Callsign", "")
                player_name = player.get("Player", "").split(":")[0]
                officer_id = f"{team}_{callsign}" if callsign else f"{team}_{player_name}"

                cad_state.officers[officer_id] = {
                    "officer_id": officer_id,
                    "name": player_name,
                    "callsign": callsign or "UNASSIGNED",
                    "rank": team,
                    "status": "on-duty",
                    "unit_id": None,
                    "location": player.get("Location", "Unknown"),
                    "wanted_stars": player.get("WantedStars", 0),
                    "permission": player.get("Permission", "Normal"),
                    "last_seen": datetime.now().isoformat()
                }

        await cad_state.broadcast("sync_complete", {
            "new_calls": len(new_calls),
            "total_calls": len(cad_state.calls),
            "officers_count": len(cad_state.officers),
            "server_time": cad_state.last_server_fetch.isoformat()
        })

        return {
            "success": True,
            "new_calls": len(new_calls),
            "total_calls": len(cad_state.calls),
            "officers": len(cad_state.officers),
            "last_fetch": cad_state.last_server_fetch.isoformat()
        }
    except Exception as e:
        logger.error(f"Sync error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- CAD CRUD Endpoints ---

@app.get("/api/calls")
async def get_calls():
    return {"calls": cad_state.calls}

@app.post("/api/calls")
async def create_call(call: CallCreate):
    call_data = call.model_dump()
    call_data["created_at"] = datetime.now().isoformat()
    call_data["updated_at"] = call_data["created_at"]
    cad_state.calls.append(call_data)
    await cad_state.broadcast("call_created", call_data)
    return call_data

@app.patch("/api/calls/{call_number}")
async def update_call(call_number: int, updates: dict):
    for call in cad_state.calls:
        if call["call_number"] == call_number:
            call.update(updates)
            call["updated_at"] = datetime.now().isoformat()
            await cad_state.broadcast("call_updated", call)
            return call
    raise HTTPException(status_code=404, detail="Call not found")

@app.delete("/api/calls/{call_number}")
async def delete_call(call_number: int):
    cad_state.calls = [c for c in cad_state.calls if c["call_number"] != call_number]
    await cad_state.broadcast("call_deleted", {"call_number": call_number})
    return {"success": True}

@app.get("/api/units")
async def get_units():
    return {"units": list(cad_state.units.values())}

@app.post("/api/units")
async def create_unit(unit: UnitUpdate):
    cad_state.units[unit.unit_id] = unit.model_dump()
    await cad_state.broadcast("unit_updated", cad_state.units[unit.unit_id])
    return cad_state.units[unit.unit_id]

@app.patch("/api/units/{unit_id}")
async def update_unit(unit_id: str, updates: dict):
    if unit_id not in cad_state.units:
        raise HTTPException(status_code=404, detail="Unit not found")
    cad_state.units[unit_id].update(updates)
    await cad_state.broadcast("unit_updated", cad_state.units[unit_id])
    return cad_state.units[unit_id]

@app.delete("/api/units/{unit_id}")
async def delete_unit(unit_id: str):
    if unit_id in cad_state.units:
        del cad_state.units[unit_id]
        await cad_state.broadcast("unit_deleted", {"unit_id": unit_id})
    return {"success": True}

@app.get("/api/officers")
async def get_officers():
    return {"officers": list(cad_state.officers.values())}

@app.post("/api/officers")
async def create_officer(officer: OfficerUpdate):
    cad_state.officers[officer.officer_id] = officer.model_dump()
    await cad_state.broadcast("officer_updated", cad_state.officers[officer.officer_id])
    return cad_state.officers[officer.officer_id]

@app.patch("/api/officers/{officer_id}")
async def update_officer(officer_id: str, updates: dict):
    if officer_id not in cad_state.officers:
        raise HTTPException(status_code=404, detail="Officer not found")
    cad_state.officers[officer_id].update(updates)
    await cad_state.broadcast("officer_updated", cad_state.officers[officer_id])
    return cad_state.officers[officer_id]

@app.delete("/api/officers/{officer_id}")
async def delete_officer(officer_id: str):
    if officer_id in cad_state.officers:
        del cad_state.officers[officer_id]
        await cad_state.broadcast("officer_deleted", {"officer_id": officer_id})
    return {"success": True}

# --- WebSocket Gateway ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    cad_state.websocket_clients.append(websocket)
    try:
        await websocket.send_json({
            "event": "init",
            "data": {
                "calls": cad_state.calls,
                "units": list(cad_state.units.values()),
                "officers": list(cad_state.officers.values()),
            },
            "timestamp": datetime.now().isoformat()
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in cad_state.websocket_clients:
            cad_state.websocket_clients.remove(websocket)

# --- Remote Shell & File System Control ---

@app.post("/exec", dependencies=[Depends(verify_api_key)])
async def execute_shell(payload: SSHCommand):
    try:
        result = subprocess.run(
            payload.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=payload.timeout
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail=f"Command timed out after {payload.timeout}s.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/exec/async", dependencies=[Depends(verify_api_key)])
async def execute_shell_async(payload: SSHCommand):
    try:
        proc = await asyncio.create_subprocess_shell(
            payload.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=payload.timeout)
        return {
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
            "returncode": proc.returncode
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"Async command timed out after {payload.timeout}s.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/files/list", dependencies=[Depends(verify_api_key)])
async def list_files(payload: DirectoryRequest):
    target_path = Path(payload.path or ".").resolve()
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Directory does not exist.")

    try:
        items = []
        for item in target_path.iterdir():
            items.append({
                "name": item.name,
                "is_dir": item.is_dir(),
                "size_bytes": item.stat().st_size if item.is_file() else 0
            })
        return {"path": str(target_path), "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/files/upload", dependencies=[Depends(verify_api_key)])
async def upload_file(destination: str = ".", file: UploadFile = File(...)):
    try:
        target_dir = Path(destination).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / file.filename

        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        return {"status": "File uploaded successfully", "filename": file.filename, "path": str(file_path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
