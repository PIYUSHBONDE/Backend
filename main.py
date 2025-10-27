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
from google.cloud import storage
from vertexai import rag
from fastapi import BackgroundTasks


import uuid
from sqlalchemy import text


from models import (
    Base,
    engine,          
    SessionLocal,    
    ConversationMetadata,
    Document,
)

from memory_agent.agent import memory_agent

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
        
@app.get("/")
async def root():
    return {"message": "HealthCase AI Agent API is running"}