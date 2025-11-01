import os
import json
import base64
import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
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
from datetime import datetime, timezone, timedelta
from google.genai import types
from google.cloud import storage
from vertexai import rag
from fastapi import BackgroundTasks
import requests

import uuid
from sqlalchemy import text


from models import (
    Base,
    engine,          
    SessionLocal,    
    ConversationMetadata,
    Document,
    JiraConnection,
    RequirementTrace,
    
)

from jira_service import (
    get_valid_connection,
    fetch_jira_projects,
    fetch_jira_requirements,
    create_jira_test_case
)
import secrets
from urllib.parse import urlencode
from fastapi.responses import RedirectResponse

from memory_agent.agent import memory_agent
from workflow_agent.agent import workflow_agent

# --- 1. SETUP ---
load_dotenv()

# GCS Client Setup
storage_client = storage.Client()
BUCKET_NAME = os.getenv("BUCKET_NAME") # Ensure BUCKET_NAME is in your .env for uploads

# Agent/Vertex AI Config
AGENT_RESOURCE_ID = os.getenv("AGENT_RESOURCE_ID") # Keep if using remote agent engines elsewhere
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
GOOGLE_CLOUD_STAGING_BUCKET = os.getenv("GOOGLE_CLOUD_STAGING_BUCKET") # Needed for vertexai.init

# Jira Config
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = "SCRUM"
JIRA_ISSUE_TYPE_NAME = "Task"


# State storage for OAuth (use Redis in production)
oauth_states = {}

# OAuth Config
JIRA_OAUTH_CLIENT_ID = os.getenv("JIRA_OAUTH_CLIENT_ID")
JIRA_OAUTH_CLIENT_SECRET = os.getenv("JIRA_OAUTH_CLIENT_SECRET") 
JIRA_OAUTH_CALLBACK_URL = os.getenv("JIRA_OAUTH_CALLBACK_URL", "http://localhost:8000/api/jira/callback")



RAG_CORPUS_ID = os.getenv("DATA_STORE_ID")
RAG_CORPUS_NAME = f"projects/{GOOGLE_CLOUD_PROJECT}/locations/{GOOGLE_CLOUD_LOCATION}/ragCorpora/{RAG_CORPUS_ID}"

# Vertex AI Init (Check if project/location are loaded)
if GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION and GOOGLE_CLOUD_STAGING_BUCKET:
    vertexai.init(
        project=GOOGLE_CLOUD_PROJECT,
        location=GOOGLE_CLOUD_LOCATION,
        staging_bucket=GOOGLE_CLOUD_STAGING_BUCKET
    )
    print("✅ Vertex AI initialized.")
else:
    print("⚠️ Vertex AI NOT initialized - Missing GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, or GOOGLE_CLOUD_STAGING_BUCKET in .env")

try:
    session_service = DatabaseSessionService(db_url=engine.url)
    
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

try:
    with engine.connect() as conn:
        # Ensure base tables exist
        Base.metadata.create_all(bind=conn)

        # --- Schema synchronization (for older DBs) ---
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS session_id VARCHAR(255);"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;"))

        # --- Extensions and indexes ---
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        conn.execute(text("""
            ALTER TABLE vector_embeddings 
            ADD COLUMN IF NOT EXISTS embedding vector(768);
        """))

        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_doc_content_hash ON documents(content_hash);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_doc_status_user ON documents(status, user_id);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vec_document_id ON vector_embeddings(document_id);"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS gcs_uri VARCHAR(500);"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS rag_file_id VARCHAR(500);"))

        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_doc_session_active
            ON documents(session_id, is_active)
            WHERE status = 'active';
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_vec_embedding_hnsw
            ON vector_embeddings
            USING hnsw (embedding vector_cosine_ops);
        """))
        
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS requirement_traces (
                id SERIAL PRIMARY KEY,
                requirement_id VARCHAR(50) NOT NULL,
                requirement_text TEXT NOT NULL,
                requirement_type VARCHAR(50),
                category VARCHAR(100),
                compliance_standard VARCHAR(50),
                risk_level VARCHAR(20),
                source_section VARCHAR(200),
                regulatory_refs TEXT[],
                source_document_id VARCHAR(255),
                test_case_ids TEXT[],
                jira_issue_keys TEXT[],
                session_id VARCHAR(255) NOT NULL,
                user_id VARCHAR(255) NOT NULL,
                status VARCHAR(50) DEFAULT 'extracted',
                coverage_percentage INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE INDEX IF NOT EXISTS idx_req_session ON requirement_traces(session_id);
            CREATE INDEX IF NOT EXISTS idx_req_id_session ON requirement_traces(requirement_id, session_id);
        """))
        
        conn.execute(text("""
            ALTER TABLE jira_connections 
            ALTER COLUMN access_token TYPE VARCHAR(3000),
            ALTER COLUMN refresh_token TYPE VARCHAR(3000);
        """))
                
        conn.commit()

    print("✅ Database tables and vector extension verified.")
except Exception as e:
    print(f"❌ Failed to initialize database extensions/indexes: {e}")


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
    
class RenamePayload(BaseModel):
    new_title: str

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

# @app.post("/create-jira-test-case")
# async def create_jira_test_case(test_case: TestCasePayload):
#     api_url = f"https://{JIRA_DOMAIN}/rest/api/3/issue"
#     headers = {
#         "Authorization": get_jira_auth_header(),
#         "Accept": "application/json",
#         "Content-Type": "application/json",
#     }
    
#     issue_payload = {
#         "fields": {
#             "project": {"key": JIRA_PROJECT_KEY},
#             "summary": test_case.title,
#             "description": format_description_for_jira(test_case),
#             "issuetype": {"name": JIRA_ISSUE_TYPE_NAME}
#         }
#     }

#     try:
#         async with httpx.AsyncClient() as client:
#             response = await client.post(api_url, headers=headers, json=issue_payload)
#         response.raise_for_status()
#         created_issue = response.json()
#         issue_url = f"https://{JIRA_DOMAIN}/browse/{created_issue['key']}"
#         return {
#             "status": "Issue created successfully!",
#             "issue_key": created_issue['key'],
#             "url": issue_url
#         }
#     except httpx.HTTPStatusError as e:
#         raise HTTPException(status_code=e.response.status_code, detail=e.response.json())
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))
    
    
# main.py

@app.post("/new-session")
async def create_new_session(req: NewSessionRequest):
    """Creates a new chat session and ensures user_id is in ADK state."""
    db = SessionLocal()
    try:
        # Include user_id in the state passed to create_session
        initial_state = {
            "user_name": req.user_id,
            "user_id": req.user_id, # Add user_id here for the tool
            # Add any other initial state needed by your agent/tools
        }
        print(f"Creating session for user {req.user_id} with initial state: {initial_state}")

        # 1. Create the ADK session with the initial state containing user_id
        new_adk_session = await session_service.create_session(
            app_name=runner.app_name,
            user_id=req.user_id,
            state=initial_state, # Pass the state including user_id
        )
        new_session_id = new_adk_session.id
        print(f"ADK session created with ID: {new_session_id}")

        # --- NO NEED TO UPDATE STATE HERE ---
        # The session_id will be available via tool_context later

        # 2. Save conversation metadata (for listing sessions in UI)
        default_title = "New Conversation"
        new_metadata = ConversationMetadata(
            session_id=new_session_id,
            user_id=req.user_id,
            title=default_title,
            updated_at=datetime.now(timezone.utc)
        )
        db.add(new_metadata)
        db.commit()

        print(f"Created new session metadata: {new_session_id} for user: {req.user_id}")
        return {"session_id": new_session_id, "title": default_title}
    except Exception as e:
        db.rollback()
        print(f"❌ Error creating new session: {e}")
        import traceback
        traceback.print_exc() # Print full traceback for debugging
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e}")
    finally:
        db.close()
    
@app.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest):
    """Sends a message to an existing session and gets the agent's response."""
    db = SessionLocal()
    try:
        content = types.Content(role="user", parts=[types.Part(text=req.message)])
        final_response_text = ""
        
        async for event in runner.run_async(
            user_id=req.user_id, session_id=session_id, new_message=content
        ):
            if event.is_final_response() and event.content.parts and hasattr(event.content.parts[0], "text"):
                final_response_text = event.content.parts[0].text.strip()
        
        # --- NEW: Update our metadata table ---
        updated_title = None
        session_metadata = db.query(ConversationMetadata).filter(
            ConversationMetadata.session_id == session_id,
            ConversationMetadata.user_id == req.user_id
        ).first()

        if session_metadata:
            session_metadata.updated_at = datetime.now(timezone.utc)
            if session_metadata.title == "New Conversation":
                new_title = req.message[:50]
                session_metadata.title = new_title
                updated_title = new_title
            
            db.commit()
        
        return {"role": "assistant", "text": final_response_text, "updated_title": updated_title}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.get("/sessions/{user_id}")
async def list_sessions(user_id: str):
    """Lists all existing sessions for a user."""
    db = SessionLocal()
    try:
        # Query our new table, ordering by the last updated time
        sessions_metadata = db.query(ConversationMetadata).filter(
            ConversationMetadata.user_id == user_id
        ).order_by(ConversationMetadata.updated_at.desc()).all()
        
        formatted_sessions = [{
            "id": s.session_id, 
            "title": s.title, 
            "updatedAt": s.updated_at.isoformat()
        } for s in sessions_metadata]
            
        return {"sessions": formatted_sessions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
    
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
        
        # print(f"Fetched {len(events)} events for session {session_id}")
        # print(f"Sample event: {events}")

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
    
    
@app.patch("/sessions/{session_id}/title")
async def rename_session_title(session_id: str, payload: RenamePayload, user_id: str):
    """Updates the title of a specific conversation."""
    db = SessionLocal()
    try:
        session_metadata = db.query(ConversationMetadata).filter(
            ConversationMetadata.session_id == session_id,
            ConversationMetadata.user_id == user_id
        ).first()

        if not session_metadata:
            raise HTTPException(status_code=404, detail="Session not found")

        session_metadata.title = payload.new_title
        session_metadata.updated_at = datetime.now(timezone.utc)
        db.commit()

        return {"status": "success", "new_title": payload.new_title}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
        

# ========== RAG DOCUMENT ENDPOINTS ==========

@app.post("/api/rag/upload")
async def upload_document_rag(
    background_tasks: BackgroundTasks, 
    file: UploadFile = File(...),
    user_id: str = Form(...),
    session_id: str = Form(...)  # Frontend is sending this
):
    """
    (Simplified) Uploads a document and links it to a session.
    This version just creates the database record so it appears in the UI.
    """
    if not BUCKET_NAME:
         raise HTTPException(status_code=500, detail="GCS Bucket name not configured.")
     
    db = SessionLocal()
    try:
        # 1. Upload to GCS
        bucket = storage_client.bucket(BUCKET_NAME)
        # Create a unique path, maybe including user/session ID
        blob_name = f"user_{user_id}/session_{session_id}/{uuid.uuid4()}_{file.filename}"
        blob = bucket.blob(blob_name)
        
        contents = await file.read()
        blob.upload_from_string(contents, content_type=file.content_type)
        gcs_uri = f"gs://{BUCKET_NAME}/{blob_name}"
        
        print(f"✅ File uploaded to GCS: {gcs_uri}")
        
        # Create a placeholder document record
        doc_id = str(uuid.uuid4())
        new_doc = Document(
            id=str(doc_id),
            filename=file.filename,
            user_id=user_id,
            session_id=session_id,
            is_active=True,
            status='processing',
            # Simulating some data for the UI
            total_pages=0, 
            chunk_count=0,
            gcs_uri=gcs_uri,
            document_summary="Processing document...",
            rag_file_id=None
        )
        db.add(new_doc)
        db.commit()
        db.refresh(new_doc)

        print(f"✅ (Simplified Upload) Saved doc {new_doc.id} to session {session_id}")
        
        # 3. Import to RAG Engine (async in background)
        background_tasks.add_task(
            import_to_rag_engine,
            doc_id=str(doc_id),
            gcs_uri=gcs_uri,
            user_id=user_id,
            session_id=session_id,
            filename=file.filename
        )

        return {
            "status": "success",
            "document_id": new_doc.id,
            "filename": new_doc.filename,
            "gcs_uri": gcs_uri,
            "message": "Document uploaded and is processing."
        }
        # --- END OF SIMPLIFIED VERSION ---
    
    except Exception as e:
        db.rollback() # Rollback DB changes if GCS upload or DB save fails
        print(f"❌ RAG Upload Error: {e}")
        # Attempt to delete the GCS file if DB save failed
        if blob:
            try:
                print(f"   -> Attempting to clean up GCS blob: {blob.name}")
                blob.delete()
                print(f"   -> GCS blob deleted.")
            except Exception as delete_e:
                print(f"   -> Failed to delete GCS blob {blob.name}: {delete_e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to upload document: {e}")
    finally:
        db.close()
        
def import_to_rag_engine(
    doc_id: str,
    gcs_uri: str,
    user_id: str,
    session_id: str,
    filename: str
):
    """
    Background task to import document to RAG Engine
    """
    db = SessionLocal()
    try:
        print(f"📥 Starting RAG import for {doc_id}")
        
        # Import to RAG Engine with chunking config
        response = rag.import_files(
            corpus_name=RAG_CORPUS_NAME,
            paths=[gcs_uri],
            transformation_config=rag.TransformationConfig(
                chunking_config=rag.ChunkingConfig(
                    chunk_size=512,      # Adjust based on your needs
                    chunk_overlap=100
                )
            ),
            max_embedding_requests_per_min=900
        )
        
        # Get the RAG file ID
        rag_file_id = None
        if response.imported_rag_files_count > 0:
            try:
                # List files and find the one we just imported
                files = list(rag.list_files(corpus_name=RAG_CORPUS_NAME))
                
                # Find by GCS URI or filename (most recently added)
                for file in reversed(files):  
                    if filename in file.display_name:
                        rag_file_id = file.name
                        print(f"✅ Found RAG file: {rag_file_id}")
                        break
                
                # If not found by display name, use the last file
                if not rag_file_id and files:
                    rag_file_id = files[-1].name
                    print(f"⚠️ Using last file as fallback: {rag_file_id}")
                    
            except Exception as list_error:
                print(f"⚠️ Could not list files: {list_error}")
                # Fallback: construct expected ID
                rag_file_id = f"{RAG_CORPUS_NAME}/ragFiles/{doc_id}"
        
        if not rag_file_id:
            raise Exception("Failed to get RAG file ID after import")
        
        print(f"✅ RAG import complete: {rag_file_id}")
        
        # Update document record
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc:
            doc.rag_file_id = rag_file_id
            doc.status = 'active'
            doc.document_summary = f"Document processed successfully. Ready for querying."
            doc.chunk_count = 1  # You can calculate actual chunks if needed
            db.commit()
            print(f"✅ Updated document {doc_id} with RAG file ID")
    
    except Exception as e:
        print(f"❌ RAG import failed for {doc_id}: {e}")
        # Update document status to failed
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc:
            doc.status = 'failed'
            doc.document_summary = f"Failed to process: {str(e)}"
            db.commit()
        import traceback
        traceback.print_exc()
    finally:
        db.close()


@app.get("/api/rag/documents/session/{session_id}")
async def get_session_documents(session_id: str, user_id: str):
    """
    Gets all documents associated with a specific session for a user.
    (This is called by DocumentManager.tsx)
    """
    db = SessionLocal()
    try:
        # Query using SQLAlchemy ORM (safer than raw SQL)
        docs = db.query(
            Document.id,
            Document.filename,
            Document.chunk_count,
            Document.total_pages,
            Document.document_summary,
            Document.upload_date,
            Document.is_active
        ).filter(
            Document.session_id == session_id,
            Document.user_id == user_id,
            Document.status == 'active'
        ).order_by(Document.upload_date.desc()).all()

        active_count = sum(1 for d in docs if d.is_active)

        return {
            "documents": [
                {
                    "id": str(d.id), # Ensure ID is string
                    "filename": d.filename,
                    "chunk_count": d.chunk_count,
                    "total_pages": d.total_pages,
                    "summary": d.document_summary,
                    "uploaded": d.upload_date.isoformat() if d.upload_date else None,
                    "is_active": d.is_active
                }
                for d in docs
            ],
            "total": len(docs),
            "active_count": active_count
        }
    except Exception as e:
        print(f"❌ Get Session Docs Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.patch("/api/rag/documents/{document_id}/toggle")
async def toggle_document_active(
    document_id: str,
    user_id: str = Form(...), # Read user_id from form body
    is_active: bool = Form(...) # Read new status from form body
):
    """
    Toggles a document's active status.
    (This is called by DocumentManager.tsx via api.js)
    """
    db = SessionLocal()
    try:
        # Verify ownership
        doc = db.query(Document).filter(
            Document.id == document_id,
            Document.user_id == user_id,
            Document.status == 'active'
        ).first()

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found or not authorized")

        # Set the new status from the request
        doc.is_active = is_active
        db.commit()

        print(f"✅ Toggled doc {doc.id} for user {user_id} to {is_active}")

        return {
            "status": "success",
            "document_id": doc.id,
            "is_active": doc.is_active
        }
    except Exception as e:
        db.rollback()
        print(f"❌ Toggle Doc Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
        
        
# ============================================================================
# JIRA OAUTH ENDPOINTS
# ============================================================================

@app.get("/api/jira/connect")
async def jira_connect(user_id: str):
    """Initiate OAuth flow."""
    state = secrets.token_urlsafe(32)
    oauth_states[state] = user_id
    
    params = {
        "audience": "api.atlassian.com",
        "client_id": JIRA_OAUTH_CLIENT_ID,
        "scope": (
            "read:jira-work read:jira-user write:jira-work "
            "read:jira-software offline_access"
        ),
        "redirect_uri": JIRA_OAUTH_CALLBACK_URL,
        "state": state,
        "response_type": "code",
        "prompt": "consent"
    }
    
    auth_url = f"https://auth.atlassian.com/authorize?{urlencode(params)}"
    return {"authorization_url": auth_url}


@app.get("/api/jira/callback")
async def jira_callback(code: str, state: str):
    """Handle OAuth callback."""
    user_id = oauth_states.get(state)
    if not user_id:
        raise HTTPException(400, "Invalid state")
    
    # Exchange code for tokens
    token_url = "https://auth.atlassian.com/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": JIRA_OAUTH_CLIENT_ID,
        "client_secret": JIRA_OAUTH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": JIRA_OAUTH_CALLBACK_URL
    }
    
    response = requests.post(token_url, json=payload)
    response.raise_for_status()
    tokens = response.json()
    
    # Get Jira instance
    resources_url = "https://api.atlassian.com/oauth/token/accessible-resources"
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    resources_resp = requests.get(resources_url, headers=headers)
    resources = resources_resp.json()
    
    if not resources:
        raise HTTPException(400, "No Jira instances")
    
    jira_resource = resources[0]
    
    # Save to database
    db = SessionLocal()
    try:
        existing = db.query(JiraConnection).filter(
            JiraConnection.user_id == user_id
        ).first()
        
        if existing:
            existing.access_token = tokens["access_token"]
            existing.refresh_token = tokens.get("refresh_token")
            existing.token_expires_at = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))
            existing.jira_cloud_id = jira_resource["id"]
            existing.jira_base_url = jira_resource["url"]
            existing.is_active = True
        else:
            conn = JiraConnection(
                user_id=user_id,
                jira_cloud_id=jira_resource["id"],
                jira_base_url=jira_resource["url"],
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                token_expires_at=datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))
            )
            db.add(conn)
        
        db.commit()
        del oauth_states[state]
        
        # Redirect to frontend
        return RedirectResponse(url="http://localhost:5173/jira-connected?success=true")
    finally:
        db.close()


@app.get("/api/jira/status")
async def jira_status(user_id: str):
    """Check connection status."""
    conn = get_valid_connection(user_id)
    if conn:
        return {
            "connected": True,
            "jira_url": conn.jira_base_url,
            "expires_at": conn.token_expires_at.isoformat()
        }
    return {"connected": False}


@app.delete("/api/jira/disconnect")
async def jira_disconnect(user_id: str):
    """Disconnect Jira."""
    db = SessionLocal()
    try:
        db.query(JiraConnection).filter(
            JiraConnection.user_id == user_id
        ).update({"is_active": False})
        db.commit()
        return {"status": "disconnected"}
    finally:
        db.close()


# ============================================================================
# JIRA OPERATIONS (REPLACE YOUR HARDCODED ONES)
# ============================================================================

@app.get("/api/jira/projects")
async def get_projects(user_id: str):
    """Get projects (OAuth)."""
    return fetch_jira_projects(user_id)


@app.post("/api/jira/fetch-requirements")
async def fetch_requirements(data: dict):
    """Fetch requirements (OAuth)."""
    return fetch_jira_requirements(data["user_id"], data["project_key"])


@app.post("/api/jira/create-jira-test-case")
async def create_test_case_oauth(data: dict):
    """Create test case (OAuth)."""
    return create_jira_test_case(
        user_id=data["user_id"],
        project_key=data["project_key"],
        test_case=data["test_case"],
        requirement_key=data.get("requirement_key")
    )
    
# main.py - SIMPLE IMPORT ENDPOINT

# Modify the existing /api/jira/import-requirements endpoint

@app.post("/api/jira/import-requirements")
async def import_jira_requirements(background_tasks: BackgroundTasks, data: dict):
    """
    Import selected Jira requirements.
    Saves to database + uploads to RAG corpus.
    """
    session_id = data.get("session_id")
    user_id = data.get("user_id")
    requirements = data.get("requirements", [])
    overwrite = data.get("overwrite", False)  # NEW: Allow overwrite flag
    
    if not all([session_id, user_id, requirements]):
        raise HTTPException(400, "Missing required fields")
    
    db = SessionLocal()
    try:
        imported_count = 0
        updated_count = 0
        
        # 1. Save to database for tracking/UI
        for req_data in requirements:
            existing = db.query(RequirementTrace).filter(
                RequirementTrace.requirement_id == req_data.get("id"),
                RequirementTrace.session_id == session_id
            ).first()
            
            if existing:
                if overwrite:
                    # Update existing requirement
                    existing.requirement_text = req_data.get("text")
                    existing.requirement_type = req_data.get("type", "functional")
                    existing.risk_level = req_data.get("risk_level", "medium")
                    existing.compliance_standard = req_data.get("compliance_standard", "None")
                    existing.updated_at = datetime.now()
                    updated_count += 1
                else:
                    # Skip if not overwriting
                    continue
            else:
                # Create new requirement
                requirement = RequirementTrace(
                    requirement_id=req_data.get("id"),
                    requirement_text=req_data.get("text"),
                    requirement_type=req_data.get("type", "functional"),
                    category="from_jira",
                    compliance_standard=req_data.get("compliance_standard", "None"),
                    risk_level=req_data.get("risk_level", "medium"),
                    source_section=f"Jira: {req_data.get('jira_key')}",
                    regulatory_refs=req_data.get("regulatory_refs", []),
                    jira_issue_keys=[req_data.get("jira_key")],
                    session_id=session_id,
                    user_id=user_id,
                    status='imported'
                )
                db.add(requirement)
                imported_count += 1
        
        db.commit()
        
        # 2. Upload to RAG corpus in background (only if new imports)
        if imported_count > 0:
            background_tasks.add_task(
                upload_requirements_to_rag,
                requirements=requirements,
                session_id=session_id,
                user_id=user_id
            )
        
        message = f"Imported {imported_count} new requirements"
        if updated_count > 0:
            message += f", updated {updated_count} existing requirements"
        
        return {
            "status": "success",
            "imported": imported_count,
            "updated": updated_count,
            "message": message
        }
    
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()



def upload_requirements_to_rag(requirements: list, session_id: str, user_id: str):
    """Background task to upload requirements to RAG corpus."""
    try:
        # Format requirements as readable document
        doc_content = "# Imported Requirements from Jira\n\n"
        
        for req in requirements:
            doc_content += f"## {req.get('id')}: {req.get('text')}\n\n"
            doc_content += f"**Jira Key:** {req.get('jira_key')}\n"
            doc_content += f"**Type:** {req.get('type', 'Functional')}\n"
            doc_content += f"**Risk Level:** {req.get('risk_level', 'Medium').upper()}\n"
            doc_content += f"**Compliance:** {req.get('compliance_standard', 'None')}\n"
            
            if req.get('description'):
                doc_content += f"\n**Description:**\n{req.get('description')}\n"
            
            doc_content += "\n---\n\n"
        
        # Upload to GCS
        bucket = storage_client.bucket(BUCKET_NAME)
        blob_name = f"user_{user_id}/session_{session_id}/jira_requirements_{uuid.uuid4()}.txt"
        blob = bucket.blob(blob_name)
        blob.upload_from_string(doc_content, content_type="text/plain")
        gcs_uri = f"gs://{BUCKET_NAME}/{blob_name}"
        
        # Import to RAG corpus
        rag.import_files(
            corpus_name=RAG_CORPUS_NAME,
            paths=[gcs_uri],
            transformation_config=rag.TransformationConfig(
                chunking_config=rag.ChunkingConfig(
                    chunk_size=512,
                    chunk_overlap=100
                )
            )
        )
        
        print(f"✅ Uploaded {len(requirements)} requirements to RAG corpus")
        
    except Exception as e:
        print(f"❌ Failed to upload requirements to RAG: {e}")
        import traceback
        traceback.print_exc()

# main.py - ADD THIS ENDPOINT

@app.get("/api/requirements/session/{session_id}")
async def get_session_requirements_ui(session_id: str, user_id: str):
    """Get all requirements for session for UI display."""
    db = SessionLocal()
    try:
        from models import RequirementTrace
        
        requirements = db.query(RequirementTrace).filter(
            RequirementTrace.session_id == session_id,
            RequirementTrace.user_id == user_id
        ).all()
        
        return {
            "requirements": [
                {
                    "id": str(req.id),  # Use the database ID, not requirement_id
                    "requirement_id": req.requirement_id,  # Keep for reference
                    "text": req.requirement_text,
                    "type": req.requirement_type,
                    "risk_level": req.risk_level,
                    "compliance_standard": req.compliance_standard,
                    "jira_key": req.jira_issue_keys[0] if req.jira_issue_keys else None,
                    "status": req.status,
                    "test_case_count": len(req.test_case_ids) if req.test_case_ids else 0,
                }
                for req in requirements
            ],
            "total": len(requirements)
        }
    finally:
        db.close()


# Add after the existing /api/requirements/session/{session_id} endpoint

@app.delete("/api/requirements/{requirement_id}")
async def delete_requirement(
    requirement_id: str,
    user_id: str = Query(..., description="User ID must be provided"),
    session_id: str = Query(..., description="Session ID must be provided"),
):
    """Delete a single requirement and clean up associated data."""
    db = SessionLocal()
    try:
        # FIXED: Query by 'id' column, not 'requirement_id'
        requirement = db.query(RequirementTrace).filter(
            RequirementTrace.id == requirement_id,  # Changed this line
            RequirementTrace.user_id == user_id,
            RequirementTrace.session_id == session_id
        ).first()
        
        if not requirement:
            print(f"❌ Requirement not found: id={requirement_id}, user={user_id}, session={session_id}")
            raise HTTPException(status_code=404, detail="Requirement not found")
        
        # Delete the requirement from database
        req_text = requirement.requirement_text[:50]  # For logging
        db.delete(requirement)
        db.commit()
        
        print(f"✅ Deleted requirement '{req_text}...' (id={requirement_id}) for user {user_id}")
        
        return {
            "status": "success",
            "message": f"Requirement deleted successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Delete requirement error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()



@app.post("/api/jira/check-duplicate-requirements")
async def check_duplicate_requirements(data: dict):
    """Check if requirements already exist before import."""
    session_id = data.get("session_id")
    user_id = data.get("user_id")
    requirement_ids = data.get("requirement_ids", [])
    
    if not all([session_id, user_id, requirement_ids]):
        raise HTTPException(400, "Missing required fields")
    
    db = SessionLocal()
    try:
        # Check which requirements already exist
        existing = db.query(RequirementTrace).filter(
            RequirementTrace.session_id == session_id,
            RequirementTrace.user_id == user_id,
            RequirementTrace.requirement_id.in_(requirement_ids)
        ).all()
        
        existing_ids = [req.requirement_id for req in existing]
        
        return {
            "has_duplicates": len(existing_ids) > 0,
            "existing_requirement_ids": existing_ids,
            "count": len(existing_ids)
        }
    
    finally:
        db.close()


        
@app.get("/")
async def root():
    return {"message": "HealthCase AI Agent API is running"}