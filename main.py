import os
import logging
import subprocess
import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Configure logging to print directly to stdout (visible in Render & Replit logs)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SSH-Bridge")

app = FastAPI(title="SSH & Replit Bridge Tool")

# Enable CORS for remote requests and dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SSHCommand(BaseModel):
    command: str

@app.on_event("startup")
async def log_outbound_ip():
    """Logs public outbound IP on boot (useful for egress checks)."""
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

# Handles GET and HEAD methods so Render's health checker gets 200 OK instead of 405
@app.api_route("/", methods=["GET", "HEAD"])
async def status():
    return {"status": "SSH Bridge Active"}

@app.post("/exec")
async def execute_shell(payload: SSHCommand):
    """Executes a shell command and returns stdout, stderr, and exit code."""
    try:
        result = subprocess.run(
            payload.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Command execution timed out.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    # Dynamically bind to PORT for Replit/Render compatibility (defaults to 8000)
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
