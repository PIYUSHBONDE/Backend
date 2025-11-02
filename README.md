# AI TestGen — Backend

Comprehensive README for the backend service that hosts the ADK Master Agent, RAG helpers, document ingestion and Jira integration. This service is consumed by the React frontend.

--

## Quick summary
- Stack: Python 3.11, FastAPI, Uvicorn, Google Vertex AI (optional), Firestore/GCS (optional), ADK-based agent orchestration.
- Purpose: Run the ADK Master routing agent, orchestrate testcase generation and enhancement pipelines, provide RAG/document endpoints and Jira integration for export/import.

## Repo layout (important files)
- `main.py` — FastAPI app entrypoint and HTTP routes used by the frontend
- `Master_agent/agent.py` — root MasterAgent (exposes tools like `clear_session_state`, delegates to subagents)
- `Master_agent/subagents/testcase_generator_orchestrator/` — orchestrator and pipeline subagents (generator, reviewer, refiner, collector, requirement analyst)
- `Master_agent/subagents/enhancer/` — enhancement flow and enhancer engine subagent
- `models.py` — data models and persistence helpers
- `requirements.txt` — Python dependencies
- `Dockerfile` — container build for the backend

## Environment variables (common)
- `GOOGLE_CLOUD_PROJECT` — your GCP project id (required if using Vertex/Firestore/GCS)
- `GOOGLE_CLOUD_LOCATION` — GCP region (e.g. `us-central1`)
- `DATA_STORE_ID` — Vertex/document store id (if using Vertex RAG)
- `GOOGLE_APPLICATION_CREDENTIALS` — path to GCP service account JSON (local/dev)
- `PORT` — port Uvicorn listens on (default 8080 in many deployments)
- Jira-specific env vars may be required depending on `jira_service.py` (client id/secret etc.)

## Local setup (Windows / PowerShell)
1. Open folder:
   - cd "d:\Gen AI Hackathon\backend final\Backend"
2. Create virtualenv and install:
   - python -m venv .venv
   - .venv\Scripts\Activate.ps1
   - pip install -r requirements.txt
3. Set env vars (example):
   - $env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\sa.json"
   - $env:GOOGLE_CLOUD_PROJECT = "your-project-id"
   - $env:PORT = 8080
4. Run locally:
   - uvicorn main:app --host 0.0.0.0 --port 8080 --reload

## Docker (build & run)
- Build image:
  - docker build -t ai-testgen-backend .
- Run container (example mounting credentials):
  - docker run -p 8080:8080 -e PORT=8080 -e GOOGLE_CLOUD_PROJECT="<PROJECT>" -v C:\path\to\sa.json:/creds/sa.json -e GOOGLE_APPLICATION_CREDENTIALS=/creds/sa.json ai-testgen-backend

## API endpoints (frontend uses these — high level)
- POST /agent/run — Run the ADK Master agent. Payload: { user_id, session_id, message }
- POST /new-session — Create a new user session. Payload: { user_id }
- GET /sessions/{user_id} — List sessions for a user
- POST /sessions/{session_id}/messages — Add message to session. Payload: { user_id, message }
- GET /sessions/{session_id}/messages — Fetch session messages (payload param user_id)
- RAG endpoints: `/api/rag/upload`, `/api/rag/documents/session/{session_id}`, `/api/rag/documents/{document_id}/toggle` (see frontend `src/api.js` for usage)
- Jira endpoints: `/api/jira/*` — connect, status, fetch projects, import/export testcases

## ADK Master Agent overview (developer notes)
- The MasterAgent (in `Master_agent/agent.py`) is the main router: it exposes a small toolset (for example `clear_session_state`) and delegates generation or enhancement tasks to subagents.
- Two main delegations:
  - Testcase generation orchestrator: `new_testcase_generator` which coordinates generator → reviewer → refiner → collector
  - Enhancer flow: `enhancer_engine_agent` for refining or enriching outputs
- Subagents share utility RAG tools (`rag_query.py`, `list_corpora.py`, `get_corpus_info.py`, `utils.py`) for querying document stores and Vertex AI.
- Session state is commonly stored in `session.state` while longer-term metadata is persisted via models in `models.py` or Firestore/DB.

## Developer quick tips
- To trace a behavior: search for the agent function name (e.g., `new_testcase_generator`, `enhancer_engine_agent`) and open the subagent's `agent.py` and its `tools/` directory.
- For RAG issues, verify Vertex/Firestore credentials and `DATA_STORE_ID` and confirm indexes/collections exist.

## Testing
- Add unit tests next to modules or in a `tests/` folder. Example run (after creating .venv and installing deps):
  - pytest

## Deployment notes
- Cloud Run / Cloud Build examples exist in this repo (`cloudbuild.yaml`, `app.yaml`). Ensure env vars and GCP service account permissions are configured.

## Troubleshooting
- CORS: if frontend cannot reach backend during local dev, confirm CORS middleware in `main.py` or run frontend pointing to the same host.
- Vertex errors: ensure the service account has Vertex AI and Firestore permissions and `GOOGLE_APPLICATION_CREDENTIALS` points to the JSON file.
- Agent debugging: run the master agent locally via the HTTP entrypoint, inspect logs and any `session.state` writes.

## Where to look (key files)
- `main.py`, `models.py`
- `Master_agent/agent.py`
- `Master_agent/subagents/testcase_generator_orchestrator/` and `.../enhancer/`
- `requirements.txt`, `Dockerfile`

---
If you'd like, I can also add a short architecture diagram (PlantUML) and an examples directory with sample cURL requests.
