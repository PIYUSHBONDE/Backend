# # memory_agent/tools.py
# import os
# import json
# import logging
# import traceback
# from typing import Optional, List

# from google.adk.tools.tool_context import ToolContext
# from google.cloud import aiplatform_v1
# from dotenv import load_dotenv

# # Try to import the SQLAlchemy SessionLocal and Document model.
# # Support both absolute and relative import depending on how project is run.
# try:
#     from models import SessionLocal, Document
# except Exception:
#     try:
#         from ..models import SessionLocal, Document  # type: ignore
#     except Exception as e:
#         raise ImportError("Could not import SessionLocal and Document from models.py") from e

# # Logging
# logger = logging.getLogger(__name__)
# if not logger.handlers:
#     handler = logging.StreamHandler()
#     handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
#     logger.addHandler(handler)
# logger.setLevel(logging.INFO)

# # Load env
# dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env")
# if os.path.exists(dotenv_path):
#     load_dotenv(dotenv_path=dotenv_path)
#     logger.info("Loaded .env from %s", dotenv_path)
# else:
#     load_dotenv()
#     logger.info(".env not found at %s; loaded defaults from environment if present.", dotenv_path)

# # Config
# PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
# LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-east4")
# RAG_CORPUS_ID = os.getenv("DATA_STORE_ID")  # we treat this as the ragCorpus id

# # ---------- Helpers ----------

# def _safe_state_to_dict(state_obj) -> dict:
#     """
#     Convert ADK State object (or dict) into a plain dict safely.
#     """
#     try:
#         if state_obj is None:
#             return {}
#         # ADK State object has to_dict() method
#         if hasattr(state_obj, "to_dict"):
#             return state_obj.to_dict() or {}
#         if isinstance(state_obj, dict):
#             return state_obj
#         # Last resort: try JSON serializable attributes
#         if hasattr(state_obj, "__dict__"):
#             return dict(state_obj.__dict__)
#     except Exception:
#         logger.exception("Failed converting state to dict.")
#     return {}

# def _get_session_and_user_from_tool_context(tool_context: ToolContext) -> (Optional[str], Optional[str]):
#     """
#     Robust extraction of session_id and user_id from ToolContext.
#     Supports ADK state objects (State.to_dict()) and various ToolContext shapes.
#     """
#     session_id = None
#     user_id = None

#     # Try direct session or session_id attribute
#     try:
#         session_obj = getattr(tool_context, "session", None)
#         if session_obj:
#             # session may be an object with id attr or a string
#             if isinstance(session_obj, str):
#                 session_id = session_obj
#             else:
#                 session_id = getattr(session_obj, "id", None) or getattr(session_obj, "session_id", None)
#     except Exception:
#         logger.debug("No direct .session on tool_context")

#     if not session_id:
#         session_id = getattr(tool_context, "session_id", None)

#     # Handle state object
#     state_obj = getattr(tool_context, "state", None)
#     state_dict = _safe_state_to_dict(state_obj)
#     if state_dict:
#         # debug print state for local debugging (comment out in prod)
#         try:
#             logger.debug("tool_context.state: %s", json.dumps(state_dict, indent=2))
#         except Exception:
#             logger.debug("tool_context.state (non-serializable)")

#         user_id = state_dict.get("user_id") or state_dict.get("user") or state_dict.get("user_name")
#         if not session_id:
#             session_id = state_dict.get("session_id") or state_dict.get("session")

#     # fallback to direct attributes
#     if not user_id:
#         user_id = getattr(tool_context, "user_id", None) or getattr(tool_context, "user", None)

#     if isinstance(session_id, str):
#         session_id = session_id.strip() or None
#     if isinstance(user_id, str):
#         user_id = user_id.strip() or None

#     return session_id, user_id

# def _build_rag_corpus_name(project: str, location: str, rag_corpus_id: str) -> str:
#     """
#     Build the full RAG corpus resource name.
#     Expected format:
#     projects/{project}/locations/{location}/ragCorpora/{rag_corpus_id}
#     """
#     return f"projects/{project}/locations/{location}/ragCorpora/{rag_corpus_id}"

# # ---------- Primary Tool: query_active_documents (Vertex AI RAG) ----------

# def query_active_documents(question: str, tool_context: ToolContext) -> str:
#     """
#     Query the Vertex AI RAG corpus for relevant content for the current ADK session.
#     Returns a concise context string (or a helpful error message).
#     """
#     logger.info("ADK Tool 'query_active_documents' called. question=%s", (question[:160] + "...") if len(question) > 160 else question)

#     # 1) Extract session and user
#     session_id, user_id = _get_session_and_user_from_tool_context(tool_context)
#     logger.info("Extracted session_id=%s user_id=%s from tool_context", session_id, user_id)

#     # 2) Validate environment
#     missing = []
#     if not session_id:
#         missing.append("session_id (tool_context)")
#     if not user_id:
#         missing.append("user_id (tool_context.state)")
#     if not PROJECT_ID:
#         missing.append("GOOGLE_CLOUD_PROJECT (env)")
#     if not RAG_CORPUS_ID:
#         missing.append("DATA_STORE_ID / RAG_CORPUS_ID (env)")
#     if missing:
#         msg = f"Tool Error: configuration incomplete. Missing: {', '.join(missing)}"
#         logger.error(msg)
#         return "I cannot access session documents right now because the server configuration is incomplete."

#     # 3) Gather active doc URIs from DB
#     db = None
#     try:
#         db = SessionLocal()
#         logger.debug("DB session opened.")

#         # Query documents tied to session + user and active status
#         q = db.query(Document).filter(Document.session_id == session_id, Document.user_id == user_id, Document.status == "active")
#         if hasattr(Document, "is_active"):
#             q = q.filter(Document.is_active == True)

#         # Prefer gcs_uri column when available
#         gcs_available = hasattr(Document, "gcs_uri")
#         if gcs_available:
#             q = q.filter(Document.gcs_uri.isnot(None))
#             rows = q.with_entities(Document.gcs_uri).all()
#             gcs_uris = [r[0] for r in rows if r and r[0]]
#         else:
#             rows = q.with_entities(Document.id).all()
#             gcs_uris = [r[0] for r in rows if r and r[0]]

#         logger.info("Found %d active document identifiers for session %s", len(gcs_uris), session_id)
#         if not gcs_uris:
#             return "I couldn't find any active documents for this session. Please upload or activate a document first."

#         # 4) Build rag corpus name and create client
#         rag_corpus_name = _build_rag_corpus_name(PROJECT_ID, LOCATION, RAG_CORPUS_ID)
#         logger.info("Querying RAG corpus: %s", rag_corpus_name)

#         client = aiplatform_v1.VertexRagServiceClient()
#         # Build RagQuery: we ask for top K similar docs
#         rag_query = aiplatform_v1.RagQuery(similar_doc_count=4)

#         # The QueryRagCorpusRequest takes the corpus name and the query text.
#         # Optionally we can pass `context_documents` or filters later; for now we rely on the corpus index itself.
#         request = aiplatform_v1.QueryRagCorpusRequest(
#             name=rag_corpus_name,
#             query=question,
#             rag_query=rag_query,
#             # optional: use whatever parameters you want (max_output_tokens etc.) if supported
#         )

#         # 5) Execute query
#         logger.debug("Sending QueryRagCorpusRequest to Vertex RAG...")
#         response = client.query_rag_corpus(request=request)
#         # Response contains retrieved_documents (repeated) — handle defensively
#         retrieved = getattr(response, "retrieved_documents", None) or []

#         logger.info("Vertex RAG returned %d retrieved_documents", len(retrieved))

#         # 6) Build context parts from retrieved documents/snippets
#         context_parts: List[str] = []
#         max_parts = 4

#         added = 0
#         for rd in retrieved:
#             if added >= max_parts:
#                 break
#             try:
#                 # Fields can vary depending on client version:
#                 # rd.snippet, rd.document.title, rd.document.gcs_uri, rd.metadata etc.
#                 snippet = getattr(rd, "snippet", None)
#                 if not snippet:
#                     # fallback: maybe rd.content or rd.text
#                     snippet = getattr(rd, "content", None) or getattr(rd, "text", None)

#                 # source identifiers
#                 source_label = None
#                 doc_obj = getattr(rd, "document", None)
#                 if doc_obj:
#                     # try common attrs
#                     source_label = getattr(doc_obj, "title", None) or getattr(doc_obj, "display_name", None) \
#                                    or getattr(doc_obj, "gcs_uri", None) or getattr(doc_obj, "id", None) or getattr(doc_obj, "name", None)
#                 # fallback to metadata map if available
#                 if not source_label:
#                     metadata = getattr(rd, "metadata", None)
#                     if isinstance(metadata, dict):
#                         source_label = metadata.get("title") or metadata.get("source") or metadata.get("gcsUri")
#                 if not source_label:
#                     # last resort: use one of the gcs_uris from the DB
#                     source_label = gcs_uris[added] if added < len(gcs_uris) else "Document"

#                 if snippet:
#                     snippet_text = str(snippet).strip()
#                     # Trim snippet length to keep responses compact
#                     if len(snippet_text) > 1200:
#                         snippet_text = snippet_text[:1200] + "...[truncated]"
#                     context_parts.append(f"From '{source_label}':\n{snippet_text}")
#                     added += 1
#             except Exception:
#                 logger.exception("Error processing retrieved document entry; skipping it.")
#                 continue

#         if not context_parts:
#             # If we had retrieved docs but couldn't extract snippets, give a friendly message
#             if len(retrieved) > 0:
#                 return "I found documents but couldn't extract useful snippets from them to answer precisely."
#             else:
#                 return "I searched the RAG corpus but didn't find any content relevant to your question."

#         # Combine and limit length
#         context = "\n\n---\n\n".join(context_parts)
#         max_chars = 14000
#         if len(context) > max_chars:
#             context = context[:max_chars] + "\n\n[Context truncated]"

#         logger.debug("Returning assembled context (len=%d)", len(context))
#         return context

#     except Exception as e:
#         logger.error("Unexpected error in query_active_documents: %s", e)
#         traceback.print_exc()
#         return "An error occurred while querying session documents. Details have been logged on the server."
#     finally:
#         if db:
#             try:
#                 db.close()
#             except Exception:
#                 logger.debug("Error closing DB session in finally block", exc_info=True)



# memory_agent/tools.py

from vertexai import rag
from google.adk.tools.tool_context import ToolContext
from google.genai import types
import os
from models import SessionLocal, Document

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-east4")
RAG_CORPUS_ID = os.getenv("DATA_STORE_ID")
RAG_CORPUS_NAME = f"projects/{PROJECT_ID}/locations/{LOCATION}/ragCorpora/{RAG_CORPUS_ID}"

def query_active_documents(question: str, tool_context: ToolContext) -> dict:
    """
    Search through documents uploaded in the current session.
    Use this to find information from requirements documents, specifications,
    or any files the user has uploaded and marked as active.
    
    Args:
        question: The question or query to search for in the documents
    
    Returns:
        A dictionary containing the search results or error message
    """
    
    # Try to extract session context from kwargs
    
    session_id = None
    user_id = None
    
    # Try multiple ways to access session info
    if tool_context:
        try:
            # Try accessing from invocation_context
            if hasattr(tool_context, '_invocation_context'):
                inv_ctx = tool_context._invocation_context
                if inv_ctx and hasattr(inv_ctx, 'session'):
                    session = inv_ctx.session
                    session_id = session.id if hasattr(session, 'id') else None
                    user_id = session.user_id if hasattr(session, 'user_id') else None
            
            # Fallback: try state
            if not user_id and hasattr(tool_context, 'state'):
                state = tool_context.state
                user_id = state.get('user_id') or state.get('user_name')
            
            # Another fallback: direct attributes
            if not session_id and hasattr(tool_context, 'session_id'):
                session_id = tool_context.session_id
            if not user_id and hasattr(tool_context, 'user_id'):
                user_id = tool_context.user_id
                
        except Exception as ctx_error:
            print(f"⚠️ Error extracting context: {ctx_error}")
    
    # print(f"❌ Missing context - session_id: {session_id}, user_id: {user_id}")
    
    if not session_id or not user_id:
        print(f"❌ Missing context - session_id: {session_id}, user_id: {user_id}")
        print(f"   kwargs keys: {list(kwargs.keys())}")
        return {
            "status": "error",
            "message": "Could not determine session or user context. Please ensure you're in an active session."
        }
    
    print(f"🔍 RAG Query - Session: {session_id}, User: {user_id}, Question: {question}")
    
    # Query PostgreSQL for active documents
    db = SessionLocal()
    try:
        active_docs = db.query(Document).filter(
            Document.session_id == session_id,
            Document.user_id == user_id,
            Document.is_active == True,
            Document.status == 'active',
            Document.rag_file_id.isnot(None)
        ).all()
        
        if not active_docs:
            print("⚠️ No active documents found")
            return {
                "status": "no_documents",
                "message": "No active documents found in this session. Please upload and activate documents first."
            }
        
        rag_file_ids = []
        for doc in active_docs:
            # Extract just the file ID from the full resource name
            file_id = doc.rag_file_id.split('/')[-1] if '/' in doc.rag_file_id else doc.rag_file_id
            rag_file_ids.append(file_id)
        
        print(f"✅ Found {len(active_docs)} active documents:")
        for doc, file_id in zip(active_docs, rag_file_ids):
            print(f"   - {doc.filename} (File ID: {file_id})")
        
        # print("Rag Corpus Name:", RAG_CORPUS_NAME)
        rag_resources = [
            rag.RagResource(
                rag_corpus=RAG_CORPUS_NAME,
                rag_file_ids=rag_file_ids  
            )
        ]
        
        # Query RAG Engine
        try:
            response = rag.retrieval_query(
                rag_resources=rag_resources,
                text=question,
                rag_retrieval_config=rag.RagRetrievalConfig(
                    top_k=5,  # ← CORRECT: top_k (not similarity_top_k)
                    filter=rag.utils.resources.Filter(
                        vector_distance_threshold=0.5
                    )
                )
            )
            
            # Format results
            if response.contexts and response.contexts.contexts:
                contexts = []
                for idx, ctx in enumerate(response.contexts.contexts, 1):
                    # Find source document
                    source_doc = None
                    for doc in active_docs:
                        if doc.rag_file_id in str(ctx.source_uri):
                            source_doc = doc
                            break
                    
                    source_name = source_doc.filename if source_doc else "Document"
                    context_text = f"[Source {idx}: {source_name}]\n{ctx.text}\n---"
                    contexts.append(context_text.strip())
                
                result_text = "\n\n".join(contexts)
                print(f"✅ Retrieved {len(contexts)} relevant chunks")
                
                return {
                    "status": "success",
                    "result": result_text,
                    "sources_count": len(contexts)
                }
            else:
                print("⚠️ No relevant content found")
                return {
                    "status": "no_results",
                    "message": f"I searched through {len(active_docs)} active document(s) but couldn't find relevant information for: '{question}'. Please try rephrasing your question or check if the right documents are active."
                }
        
        except Exception as rag_error:
            print(f"❌ RAG query error: {rag_error}")
            import traceback
            traceback.print_exc()
            return {
                "status": "error",
                "message": f"Error querying documents: {str(rag_error)}"
            }
    
    except Exception as db_error:
        print(f"❌ Database error: {db_error}")
        import traceback
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"Error accessing document database: {str(db_error)}"
        }
    finally:
        db.close()


print("✅ RAG Tool 'query_active_documents' defined as function")


# memory_agent/tools.py - ADD THIS NEW TOOL

def extract_requirements(document_focus: str = "all", tool_context: ToolContext) -> dict:
    """
    Extract structured requirements from uploaded documents.
    Automatically assigns requirement IDs and classifies by type and risk.
    
    Args:
        document_focus: Which documents to analyze ("all" or specific filename)
    
    Returns:
        Structured list of requirements with IDs, text, classification, and risk
    """
    
    # Get session context
    session_id = None
    user_id = None
    
    if tool_context:
        try:
            if hasattr(tool_context, '_invocation_context'):
                inv_ctx = tool_context._invocation_context
                if inv_ctx and hasattr(inv_ctx, 'session'):
                    session = inv_ctx.session
                    session_id = session.id if hasattr(session, 'id') else None
                    user_id = session.user_id if hasattr(session, 'user_id') else None
            
            if not user_id and hasattr(tool_context, 'state'):
                state = tool_context.state
                user_id = state.get('user_id') or state.get('user_name')
        except:
            pass
    
    if not session_id:
        return {"status": "error", "message": "Session context not available"}
    
    print(f"📋 Extracting requirements - Session: {session_id}")
    
    # Get RAG corpus name
    try:
        RAG_CORPUS_ID = os.getenv("DATA_STORE_ID")
        GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
        GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
        RAG_CORPUS_NAME = f"projects/{GOOGLE_CLOUD_PROJECT}/locations/{GOOGLE_CLOUD_LOCATION}/ragCorpora/{RAG_CORPUS_ID}"
    except:
        return {"status": "error", "message": "RAG configuration missing"}
    
    # Query active documents
    db = SessionLocal()
    try:
        active_docs = db.query(Document).filter(
            Document.session_id == session_id,
            Document.user_id == user_id,
            Document.is_active == True,
            Document.status == 'active',
            Document.rag_file_id.isnot(None)
        ).all()
        
        if not active_docs:
            return {"status": "no_documents", "message": "No active documents to extract from"}
        
        # Build RAG resources
        rag_file_ids = [doc.rag_file_id.split('/')[-1] for doc in active_docs]
        rag_resources = [rag.RagResource(rag_corpus=RAG_CORPUS_NAME, rag_file_ids=rag_file_ids)]
        
        # Query for requirement-like content
        extraction_query = """
        Extract all software requirements, functional requirements, security requirements, 
        and compliance requirements from these documents. Include requirement text, 
        section numbers if available, and any regulatory references.
        """
        
        response = rag.retrieval_query(
            rag_resources=rag_resources,
            text=extraction_query,
            rag_retrieval_config=rag.RagRetrievalConfig(
                top_k=20,  # Get more chunks for comprehensive extraction
                filter=rag.Filter(vector_distance_threshold=0.3)
            )
        )
        
        if not response.contexts or not response.contexts.contexts:
            return {"status": "no_results", "message": "No requirements found in documents"}
        
        # Combine all retrieved context
        combined_context = "\n\n".join([ctx.text for ctx in response.contexts.contexts])
        
        # Use Gemini to structure the requirements
        from vertexai.generative_models import GenerativeModel
        
        model = GenerativeModel("gemini-2.0-flash")
        
        structure_prompt = f"""
        You are a healthcare QA requirements analyst. Extract and structure ALL requirements from this text.
        
        Document Content:
        {combined_context}
        
        For EACH requirement, return JSON in this EXACT format:
        {{
            "requirements": [
                {{
                    "id": "REQ-001",
                    "text": "The system shall...",
                    "type": "functional|security|performance|compliance",
                    "category": "authentication|data_security|user_interface|etc",
                    "compliance_standard": "FDA|IEC 62304|HIPAA|ISO 13485|None",
                    "risk_level": "high|medium|low",
                    "source_section": "Section 3.2.1",
                    "regulatory_refs": ["21 CFR Part 11", "IEC 62304 Class B"]
                }}
            ]
        }}
        
        IMPORTANT:
        - Assign sequential IDs starting REQ-001
        - High risk if involves patient safety, data security, or critical functions
        - Include ALL requirements found, not summaries
        - Be specific about compliance standards mentioned
        """
        
        extraction_response = model.generate_content(
            structure_prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        
        requirements_data = json.loads(extraction_response.text)
        requirements = requirements_data.get('requirements', [])
        
        print(f"✅ Extracted {len(requirements)} requirements")
        
        # Store in database for traceability
        from models import RequirementTrace  # You'll need to create this
        
        for req in requirements:
            trace = RequirementTrace(
                requirement_id=req['id'],
                requirement_text=req['text'],
                requirement_type=req['type'],
                category=req.get('category'),
                compliance_standard=req.get('compliance_standard', 'None'),
                risk_level=req['risk_level'],
                source_section=req.get('source_section'),
                regulatory_refs=req.get('regulatory_refs', []),
                source_document_id=active_docs[0].id,  # Link to first doc
                session_id=session_id,
                user_id=user_id,
                status='extracted',  # Not yet covered by tests
                test_case_ids=[]
            )
            
            # Check if already exists
            existing = db.query(RequirementTrace).filter(
                RequirementTrace.requirement_id == req['id'],
                RequirementTrace.session_id == session_id
            ).first()
            
            if not existing:
                db.add(trace)
        
        db.commit()
        
        return {
            "status": "success",
            "requirements": requirements,
            "total": len(requirements),
            "by_risk": {
                "high": len([r for r in requirements if r['risk_level'] == 'high']),
                "medium": len([r for r in requirements if r['risk_level'] == 'medium']),
                "low": len([r for r in requirements if r['risk_level'] == 'low'])
            },
            "by_compliance": {
                std: len([r for r in requirements if r['compliance_standard'] == std])
                for std in set(r['compliance_standard'] for r in requirements)
            }
        }
        
    except Exception as e:
        print(f"❌ Extraction error: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


print("✅ Requirement extraction tool defined")
