import os
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from dotenv import load_dotenv
import vertexai
from vertexai import agent_engines
from agent_api import normalize_agent_payload
from google.cloud import storage
from fastapi.middleware.cors import CORSMiddleware

# --- 1. SETUP ---
load_dotenv()

AGENT_RESOURCE_ID = os.getenv("AGENT_RESOURCE_ID")
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
GOOGLE_CLOUD_STAGING_BUCKET = os.getenv("GOOGLE_CLOUD_STAGING_BUCKET")
BUCKET_NAME = os.getenv("BUCKET_NAME")

vertexai.init(
    project=GOOGLE_CLOUD_PROJECT,
    location=GOOGLE_CLOUD_LOCATION,
    staging_bucket=GOOGLE_CLOUD_STAGING_BUCKET
)

app = FastAPI(title="Vertex AI Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or ["*"] for dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AgentRequest(BaseModel):
    user_id: str
    session_id: str
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
