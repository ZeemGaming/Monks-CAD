import os
import requests

COMMAND = ":h Testing from Remote Server Management."

BRIDGE_URL = "https://monks-cad.onrender.com"

# Fill this with the exact secret string you put in Render's environment settings
BRIDGE_API_KEY = "YOUR_BRIDGE_SECRET_KEY_HERE"

# Your ER:LC Server Key
PRC_API_KEY = os.getenv("PRC_API_KEY") or os.getenv("ERLC_API_KEY") or "YOUR_ERLC_KEY_HERE"

# Set up the Bearer token header for your FastAPI Bridge
headers = {
    "Authorization": f"Bearer {BRIDGE_API_KEY}",
    "Content-Type": "application/json"
}

url = f"{BRIDGE_URL}/api/erlc/command"
payload = {
    "command": COMMAND,
    "server_key": PRC_API_KEY
}

print(f"Sending '{COMMAND}' through {BRIDGE_URL}...")
response = requests.post(url, json=payload, headers=headers)

print("Status Code:", response.status_code)
print("Response:", response.text)
