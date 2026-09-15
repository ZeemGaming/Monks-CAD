import os
import logging
import subprocess
import asyncio
import aiohttp
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Security, UploadFile, File
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SSH-Bridge")

# Explicitly defining 'app' for Uvicorn
app = FastAPI(
    title="SSH & Replit Bridge API",
    description="Full-featured management API for web dashboard integration.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")
security = HTTPBearer(auto_error=False)

async def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    if BRIDGE_API_KEY:
        if not credentials or credentials.credentials != BRIDGE_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key.")
    return True

# --- Data Models ---

class SSHCommand(BaseModel):
    command: str
    timeout: Optional[int] = 30

class DirectoryRequest(BaseModel):
    path: Optional[str] = "."

class ERLCCommandRequest(BaseModel):
    command: str
    server_key: Optional[str] = None

# --- Startup Event ---

@app.on_event("startup")
async def log_outbound_ip():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org") as resp:
                if resp.status == 200:
                    ip = await resp.text()
                    logger.info(f"=== SSH BRIDGE PUBLIC IP: {ip} ===")
                else:
                    logger.warning("Failed to retrieve public IP address from ipify.")
    except Exception as e:
        logger.error(f"Error fetching IP: {e}")

# --- API Endpoints ---

@app.api_route("/", methods=["GET", "HEAD"])
async def status():
    return {"status": "SSH Bridge Active", "authenticated": bool(BRIDGE_API_KEY)}

@app.post("/api/erlc/command", dependencies=[Depends(verify_api_key)])
async def execute_erlc_command(payload: ERLCCommandRequest):
    """Executes ER:LC commands directly from Render's whitelisted outbound IP."""
    key = payload.server_key or os.getenv("PRC_API_KEY") or os.getenv("ERLC_SERVER_KEY")
    if not key:
        raise HTTPException(status_code=400, detail="No ER:LC Server Key provided or found in environment variables.")

    url = "https://api.erlc.gg/v2/server/command"
    headers = {
        "server-key": key,
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={"command": payload.command}, headers=headers) as resp:
            text = await resp.text()
            return {"status_code": resp.status, "response": text}

@app.get("/api/system/info", dependencies=[Depends(verify_api_key)])
async def system_info():
    return {
        "cwd": os.getcwd(),
        "user": os.getenv("USER", "unknown"),
        "python_version": os.getenv("PYTHON_VERSION", "3.x"),
        "port": os.getenv("PORT", "8000")
    }

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
