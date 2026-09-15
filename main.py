import os
import aiohttp
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="ER:LC CAD Backend API")

ERLC_SERVER_KEY = os.getenv("ERLC_SERVER_KEY", "RUcUFvbCdZBMnNwDMehN-ZOlCCPSreCQPrtvNHYMxZPbstkBxgplcHyKOoYXH")
ERLC_BASE_URL = "https://api.erlc.gg/v1"

class CommandRequest(BaseModel):
    command: str

@app.get("/")
async def root():
    return {"status": "Koyeb FastAPI Backend Active"}

@app.post("/api/command")
async def execute_command(payload: CommandRequest):
    headers = {
        "Server-Key": ERLC_SERVER_KEY,
        "Content-Type": "application/json"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{ERLC_BASE_URL}/server/command",
            headers=headers,
            json={"command": payload.command}
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=resp.status, detail=await resp.text())
            return await resp.json()
