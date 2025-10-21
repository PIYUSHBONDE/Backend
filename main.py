import os
import json
import base64
import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from typing import List, Optional
from dotenv import load_dotenv
import vertexai
from vertexai import agent_engines
from agent_api import normalize_agent_payload
from google.cloud import storage
from fastapi.middleware.cors import CORSMiddleware
from urllib.parse import quote_plus
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from datetime import datetime, timezone
from google.genai import types

from memory_agent.agent import memory_agent

# --- 1. SETUP ---
load_dotenv()

AGENT_RESOURCE_ID = os.getenv("AGENT_RESOURCE_ID")
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
GOOGLE_CLOUD_STAGING_BUCKET = os.getenv("GOOGLE_CLOUD_STAGING_BUCKET")
BUCKET_NAME = os.getenv("BUCKET_NAME")

JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# Database Configuration
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PUBLIC_IP = os.getenv("DB_PUBLIC_IP")
DB_NAME = os.getenv("DB_NAME")
encoded_password = quote_plus(DB_PASSWORD)

db_url = f"postgresql+psycopg2://{DB_USER}:{encoded_password}@{DB_PUBLIC_IP}/{DB_NAME}"

# You can also move these to your .env file
JIRA_PROJECT_KEY = "SCRUM" 
JIRA_ISSUE_TYPE_NAME = "Task"

vertexai.init(
    project=GOOGLE_CLOUD_PROJECT,
    location=GOOGLE_CLOUD_LOCATION,
    staging_bucket=GOOGLE_CLOUD_STAGING_BUCKET
)

try:
    session_service = DatabaseSessionService(db_url=db_url)
    
    runner = Runner(
        agent=memory_agent,
        app_name="HealthCase AI", # Use a consistent app name
        session_service=session_service,
    )
    print("✅ ADK Runner and Session Service initialized successfully.")
except Exception as e:
    print(f"❌ Failed to initialize ADK services: {e}")
    # You might want to exit the app if this fails
    # exit(1)


app = FastAPI(title="HealthCase AI Agent API")


origins = [
    "https://frontend-app-983620134812.us-east4.run.app",
    "http://localhost:5173",
    "*",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, # Use the corrected list
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AgentRequest(BaseModel):
    user_id: str
    session_id: str
    message: str

class TestCasePayload(BaseModel):
    title: str = Field(..., example="User Login with Valid Credentials")
    preconditions: Optional[List[str]] = Field(None)
    steps: Optional[List[str]] = Field(None)
    expected_result: Optional[str] = Field(None, alias="expected") # Frontend sends 'expected'
    
class NewSessionRequest(BaseModel):
    user_id: str

class SendMessageRequest(BaseModel):
    user_id: str
    message: str

# --- 2. SYNC HELPERS ---
def call_vertex_agent(user_id: str, session_id: str, message: str) -> list:
    remote_app = agent_engines.get(AGENT_RESOURCE_ID)
    responses = []
    for event in remote_app.stream_query(
        user_id=user_id, session_id=session_id, message=message
    ):
        responses.append(event)
    return responses

def upload_and_query_agent(contents: bytes, filename: str, user_id: str, session_id: str) -> dict:
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(f"corpus/{filename}")
    blob.upload_from_string(contents)
    gs_url = f"gs://{BUCKET_NAME}/corpus/{filename}"

    message_text = f"Please add this document to the Requirements Corpus: {gs_url}"
    agent_response = call_vertex_agent(user_id, session_id, message_text)
    normalized_response = [normalize_agent_payload(event) for event in agent_response]

    return {"gs_url": gs_url, "agent_response": normalized_response}

# --- Jira Helpers ---
def get_jira_auth_header() -> str:
    auth_string = f"{JIRA_EMAIL}:{JIRA_API_TOKEN}"
    auth_bytes = auth_string.encode("ascii")
    return f"Basic {base64.b64encode(auth_bytes).decode('ascii')}"

def format_description_for_jira(data: TestCasePayload) -> dict:
    # ... (function from previous response to format description)
    pass


# --- 3. ENDPOINTS ---
@app.post("/agent/run")
async def run_agent(req: AgentRequest):
    raw_response = await run_in_threadpool(
        call_vertex_agent,
        user_id=req.user_id,
        session_id=req.session_id,
        message=req.message
    )
    normalized_events = [normalize_agent_payload(event) for event in raw_response]
    return {"agentResponse": normalized_events, "receivedAt": "TODO-UTC-Timestamp"}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...),
                      user_id: str = Form(...),
                      session_id: str = Form(...)):
    try:
        contents = await file.read()
        result = await run_in_threadpool(
            upload_and_query_agent,
            contents=contents,
            filename=file.filename,
            user_id=user_id,
            session_id=session_id
        )
        return {
            "status": "success",
            "filename": file.filename,
            "gs_url": result["gs_url"],
            "agent_response": result["agent_response"]
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/test-jira-connection")
async def test_jira_connection():
    api_url = f"https://{JIRA_DOMAIN}/rest/api/3/project"
    headers = {"Authorization": get_jira_auth_header(), "Accept": "application/json"}
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url, headers=headers)
        response.raise_for_status()
        projects = response.json()
        return {
            "status": "Connection Successful!",
            "projects": [{"name": p.get("name"), "key": p.get("key")} for p in projects]
        }
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.json())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- CORRECTED JIRA ENDPOINT ---

@app.post("/create-jira-test-case")
async def create_jira_test_case(test_case: TestCasePayload):
    api_url = f"https://{JIRA_DOMAIN}/rest/api/3/issue"
    headers = {
        "Authorization": get_jira_auth_header(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    
    issue_payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": test_case.title,
            "description": format_description_for_jira(test_case),
            "issuetype": {"name": JIRA_ISSUE_TYPE_NAME}
        }
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(api_url, headers=headers, json=issue_payload)
        response.raise_for_status()
        created_issue = response.json()
        issue_url = f"https://{JIRA_DOMAIN}/browse/{created_issue['key']}"
        return {
            "status": "Issue created successfully!",
            "issue_key": created_issue['key'],
            "url": issue_url
        }
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.json())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    
@app.post("/new-session")
async def create_new_session(req: NewSessionRequest):
    """Creates a new chat session for a user."""
    try:
        # This state is used only when creating a brand new session
        initial_state = {
            "user_name": "Default User", 
            "history": [],
            "reminders": [] # Add this line
        }
        
        new_session = await session_service.create_session(
            app_name=runner.app_name,
            user_id=req.user_id,
            state=initial_state,
        )
        print(f"Created new session: {new_session.id} for user: {req.user_id}")
        return {"session_id": new_session.id, "title": "New Conversation"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest):
    """Sends a message to an existing session and gets the agent's response."""
    try:
        content = types.Content(role="user", parts=[types.Part(text=req.message)])
        final_response_text = ""
        
        async for event in runner.run_async(
            user_id=req.user_id, session_id=session_id, new_message=content
        ):
            if event.is_final_response() and event.content.parts and hasattr(event.content.parts[0], "text"):
                final_response_text = event.content.parts[0].text.strip()
        
        # --- THIS IS THE NEW LOGIC ---
        # After getting a response, we update the session's state with a new timestamp.
        try:
            session = await session_service.get_session(
                app_name=runner.app_name, user_id=req.user_id, session_id=session_id
            )
            session.state["last_updated"] = datetime.now(timezone.utc).isoformat()
            
            # If the conversation is new, set its title from the first message
            if "title" not in session.state or session.state["title"] == "Untitled Conversation":
                session.state["title"] = req.message[:50] # Use the first 50 chars as a title
                
            await session_service.update_session(session=session)
        except Exception as update_error:
            # Log this error, but don't fail the whole request
            print(f"Warning: Failed to update session timestamp for {session_id}: {update_error}")
        # --- END OF NEW LOGIC ---

        return {"role": "assistant", "text": final_response_text}
    except Exception as e:
        print(f"Error sending message for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{user_id}")
async def list_sessions(user_id: str):
    """Lists all existing sessions for a user."""
    try:
        existing_sessions = await session_service.list_sessions(
            app_name=runner.app_name,
            user_id=user_id,
        )
        
        # Correctly format the sessions
        formatted_sessions = []
        for s in existing_sessions.sessions:
            # The timestamp is now read from the 'state' dictionary
            # We provide a default value if it's not present
            last_updated = s.state.get("last_updated", datetime.now(timezone.utc).isoformat())
            
            formatted_sessions.append({
                "id": s.id, 
                "title": s.state.get("title", "Untitled Conversation"), 
                "updatedAt": last_updated
            })
        
        # Sort sessions by the updatedAt timestamp, newest first
        formatted_sessions.sort(key=lambda x: x['updatedAt'], reverse=True)
            
        return {"sessions": formatted_sessions}
    except Exception as e:
        # It's good practice to log the error on the server
        print(f"Error listing sessions for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, user_id: str): # user_id for security
    """Gets all messages for a specific session."""
    try:
        # 1. First, fetch the session object.
        session = await session_service.get_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

        # 2. Access the `.events` attribute, as you correctly discovered.
        events = session.events
        messages = []
        
        print(f"Fetched {len(events)} events for session {session_id}")
        print(f"Sample event: {events}")

        for event in events:
            # Check if the event has content and parts
            if event.content and event.content.parts:
                part = event.content.parts[0]

                if hasattr(part, "text") and part.text:
                    messages.append({
                        "id": event.id,
                        "role": "user" if event.content.role == "user" else "assistant",
                        "text": part.text.strip(),
                        # The timestamp is a float, convert it to an ISO string
                        "createdAt": datetime.fromtimestamp(event.timestamp, tz=timezone.utc).isoformat()
                    })
        
        return {"messages": messages}
    except Exception as e:
        print(f"Error fetching messages for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))