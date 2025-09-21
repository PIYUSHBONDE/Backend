import os
import httpx
import json
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# --- Config ---
SERVICE_ACCOUNT_FILE = r"C:\Users\Piyush\Downloads\gen-ai-hackathon-471718-b72da0ad4da6.json"
PROJECT_ID = "gen-ai-hackathon-471718"
LOCATION = "us-east4"
RESOURCE_ID = "1265828154639908864"
USER_ID = "test_user"
SESSION_ID = "8163984337155391488"  # create one using SDK if you don't have

# Base URL for Agent Engine
BASE_URL = f"https://us-east4-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{LOCATION}/agentEngines/{RESOURCE_ID}"

# --- Get Access Token ---
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
creds.refresh(Request())
token = creds.token

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

# --- Prepare request body ---
# For sending a message to an existing session
body = {
    "method": "send_message",
    "parameters": {
        "user_id": USER_ID,
        "session_id": SESSION_ID,
        "message": "Can you provide requirements for Patient Registration feature please?"
    }
}

# --- Make the POST request ---
url = f"{BASE_URL}:execute"
with httpx.Client(timeout=120) as client:
    resp = client.post(url, headers=headers, json=body)
    print(resp.status_code)
    print(json.dumps(resp.json(), indent=2))
