# hybrid_rag_service.py — corrected version
"""
Hybrid RAG service (corrected)

Key fixes applied while preserving original functionality:
- Robust Gemini file upload handling (uses data["file"]["uri"])
- Safer SQL execution using sqlalchemy.text() to allow ::jsonb and ::vector casts
- Ensure documents inserted with upload_date, version, and status fields
- More robust JSON parsing of Gemini responses
- Flatten chunk lists early to avoid type confusion
- Basic retry/backoff for external HTTP calls
- Structured logging instead of print()
- Minor defensive checks and clearer error messages
"""

import asyncio
import hashlib
import uuid
import io
import json
import re
import tempfile
import os
import logging
from typing import List, Optional, Dict, Any, Tuple, Union
from sqlalchemy.orm import Session
from sqlalchemy import text
import httpx
import base64
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)


class HybridRAGService:
    """
    Complete RAG service combining:
    - Gemini Vision for multimodal extraction
    - Functional chunking by document structure
    - Version management
    - PostgreSQL + pgvector storage
    - ADK agent integration (outside this file)
    """

    def __init__(self, gemini_api_key: str):
        self.api_key = gemini_api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        # simple retry/backoff configuration
        self._max_retries = 3
        self._backoff_factor = 1.0

    # ========== DOCUMENT UPLOAD & PROCESSING ==========

    async def upload_document(
        self,
        file_content: bytes,
        filename: str,
        user_id: str,
        db: Session
    ) -> Dict[str, Any]:
        """Main entry point for document upload"""
        logger.info("Starting upload for %s", filename)

        # Step 1: Extract content using Gemini Vision
        logger.info("📄 Extracting content from %s...", filename)
        extracted_data = await self._extract_with_gemini_vision(file_content, filename)

        # Step 2: Compute content hash for duplicate/version detection
        content_hash = self._compute_content_hash(extracted_data.get("full_text", ""))

        # Step 3: Check for duplicate or version
        duplicate_check = await self._check_duplicate_or_version(
            content_hash, filename, user_id, db
        )

        if duplicate_check.get("is_duplicate"):
            logger.info("Duplicate detected: %s", duplicate_check.get("duplicate_type"))
            return {
                "status": "duplicate",
                "action_required": True,
                "message": "Document with identical content exists",
                "existing_document": duplicate_check.get("existing"),
                "options": ["replace", "new_version", "keep_both", "cancel"]
            }

        # Step 4: Build hierarchical structure
        logger.info("🏗️ Building document structure...")
        hierarchy = self._build_document_hierarchy(extracted_data.get("elements", []))

        # Step 5: Functional chunking
        logger.info("✂️ Creating functional chunks...")
        chunks = self._chunk_by_function(hierarchy)

        # Step 6: Generate embeddings in batches & store
        logger.info("🧠 Generating embeddings and storing chunks...")
        doc_id = str(uuid.uuid4())
        await self._embed_and_store_chunks(chunks, doc_id, filename, user_id, db)

        # Step 7: Generate document summary
        logger.info("📝 Generating summary...")
        # pass first few chunks (flattened)
        flat_for_summary = []
        for c in chunks:
            if isinstance(c, list):
                flat_for_summary.extend(c)
            else:
                flat_for_summary.append(c)
        summary = await self._generate_document_summary(flat_for_summary[:5])

        # Step 8: Save document metadata (use sqlalchemy.text to allow ::jsonb)
        upload_date = datetime.now(timezone.utc)
        version = 1
        status = "active"
        metadata_json = {
            "has_images": extracted_data.get("has_images", False),
            "has_tables": extracted_data.get("has_tables", False),
            "sections_count": len(hierarchy.get("sections", []))
        }

        insert_sql = text("""
            INSERT INTO documents 
            (id, filename, content_hash, chunk_count, total_pages, 
             document_summary, user_id, metadata_json, upload_date, version, status)
            VALUES (:id, :filename, :hash, :count, :pages, :summary, :user_id, (:meta)::jsonb, :upload_date, :version, :status)
        """)

        db.execute(
            insert_sql,
            {
                "id": doc_id,
                "filename": filename,
                "hash": content_hash,
                "count": sum(1 for _ in (c for c in (chunks if isinstance(chunks, list) else [chunks]) )),
                "pages": extracted_data.get("total_pages", 0),
                "summary": summary,
                "user_id": user_id,
                "meta": json.dumps(metadata_json),
                "upload_date": upload_date,
                "version": version,
                "status": status
            }
        )
        db.commit()

        logger.info("✅ Document processed: %s chunks created", len(flat_for_summary))

        return {
            "status": "success",
            "document_id": doc_id,
            "filename": filename,
            "chunk_count": len(flat_for_summary),
            "summary": summary,
            "metadata": {
                "pages": extracted_data.get("total_pages", 0),
                "has_images": extracted_data.get("has_images", False),
                "has_tables": extracted_data.get("has_tables", False)
            }
        }

    # ========== GEMINI VISION EXTRACTION ==========

    async def _extract_with_gemini_vision(
        self,
        file_content: bytes,
        filename: str
    ) -> Dict[str, Any]:
        """Use Gemini to extract and understand document content"""
        # Save file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name

        try:
            # Upload to Gemini File API
            uploaded_file = await self._upload_to_gemini(tmp_path)

            # Extraction prompt (keeps same behavior)
            extraction_prompt = """
Analyze this document and extract:

1. All text content preserving structure
2. Identify all sections and subsections with their titles
3. For each TABLE found, extract it in markdown format and mark with [TABLE]
4. For each IMAGE/CHART/DIAGRAM found, describe it in detail and mark with [IMAGE]
5. Preserve page numbers where available
6. Identify document type (contract, manual, report, etc.)

Format your response as JSON:
{
  "document_type": "...",
  "total_pages": number,
  "sections": [
    {
      "title": "Section Title",
      "level": 1,
      "page": 1,
      "content": "...",
      "subsections": [...]
    }
  ],
  "tables": [{"page": 1, "markdown": "..."}],
  "images": [{"page": 1, "description": "..."}],
  "full_text": "complete text content"
}
"""

            response = await self._query_gemini_file(uploaded_file, extraction_prompt)

            # Parse Gemini response robustly
            extracted_data: Dict[str, Any]
            if isinstance(response, dict):
                extracted_data = response
            else:
                # Try direct JSON parse first
                try:
                    extracted_data = json.loads(response)
                except Exception:
                    # Try to find the first {...} JSON block
                    json_match = re.search(r'\{.*\}', response, re.DOTALL)
                    if json_match:
                        try:
                            extracted_data = json.loads(json_match.group())
                        except Exception:
                            extracted_data = {"full_text": response, "sections": [], "tables": [], "images": []}
                    else:
                        extracted_data = {"full_text": response, "sections": [], "tables": [], "images": []}

            # Convert to standardized element format
            elements = self._convert_to_elements(extracted_data)

            extracted_data["elements"] = elements
            extracted_data["has_images"] = len(extracted_data.get("images", [])) > 0
            extracted_data["has_tables"] = len(extracted_data.get("tables", [])) > 0
            extracted_data.setdefault("full_text", " ".join([e.get("text", "") for e in elements]))

            return extracted_data

        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    async def _upload_to_gemini(self, file_path: str) -> str:
        """
        Robust Gemini file upload. Tries multiple response shapes and does a follow-up GET
        if the upload returns a resource 'name' without a direct URI.
        """
        url = f"{self.base_url}/files"
        last_exc = None

        def extract_candidates_from_dict(dct):
            """Return list of candidate URI strings from common keys in a dict."""
            cand = []
            if not isinstance(dct, dict):
                return cand

            # common nested file key
            file_obj = dct.get("file") or dct.get("fileMetadata") or dct.get("uploadedFile") or dct.get("fileInfo")
            if isinstance(file_obj, dict):
                for key in ("uri", "gcsUri", "googleStorageUri", "fileUri", "url", "signedUrl", "name", "uploadUri"):
                    v = file_obj.get(key)
                    if isinstance(v, str) and v.strip():
                        cand.append(v.strip())

            # top-level keys
            for key in ("uri", "gcsUri", "googleStorageUri", "fileUri", "url", "signedUrl", "name", "uploadUri"):
                v = dct.get(key)
                if isinstance(v, str) and v.strip():
                    cand.append(v.strip())

            return cand

        def recursive_find_uri(obj):
            """Recursively search nested JSON for a string starting with gs:// or http(s)://"""
            if isinstance(obj, dict):
                for v in obj.values():
                    res = recursive_find_uri(v)
                    if res:
                        return res
            elif isinstance(obj, list):
                for item in obj:
                    res = recursive_find_uri(item)
                    if res:
                        return res
            elif isinstance(obj, str):
                s = obj.strip()
                if s.startswith("gs://") or s.startswith("http://") or s.startswith("https://"):
                    return s
            return None

        for attempt in range(1, self._max_retries + 1):
            try:
                with open(file_path, "rb") as f:
                    files = {"file": (os.path.basename(file_path), f, "application/octet-stream")}
                    async with httpx.AsyncClient(timeout=300.0) as client:
                        resp = await client.post(url, params={"key": self.api_key}, files=files)
                        # always capture raw text for debugging
                        raw_text = resp.text or ""

                        # try to parse JSON safely
                        try:
                            data = resp.json()
                        except Exception:
                            data = None

                # Debug logging - enable DEBUG level to see these at runtime
                logger.debug("Gemini upload response status: %s", resp.status_code)
                logger.debug("Gemini upload response JSON: %s", json.dumps(data) if data is not None else "<no-json>")
                logger.debug("Gemini upload raw text (truncated): %s", (raw_text[:2000] + "...") if raw_text else "<empty>")

                # 1) If JSON present, try extracting candidates from known keys
                if data is not None:
                    cands = extract_candidates_from_dict(data)
                    if cands:
                        # prefer any gs:// first, else first candidate
                        for c in cands:
                            if c.startswith("gs://"):
                                return c
                        return cands[0]

                    # try recursive find for any explicit gs:// or http(s)
                    found = recursive_find_uri(data)
                    if found:
                        return found

                    # if there's a resource name but no direct URI, attempt to fetch it
                    name = None
                    for k in ("name",):
                        v = data.get(k) if isinstance(data, dict) else None
                        if isinstance(v, str) and v.strip():
                            name = v.strip()
                            break

                    if name:
                        # attempt to GET the resource by name to discover its URI
                        # name might be like "projects/.../locations/.../files/..."
                        get_url = f"{self.base_url}/{name}"
                        try:
                            async with httpx.AsyncClient(timeout=60.0) as client:
                                get_resp = await client.get(get_url, params={"key": self.api_key})
                                get_resp.raise_for_status()
                                try:
                                    get_data = get_resp.json()
                                except Exception:
                                    get_data = None

                            logger.debug("Follow-up GET for resource name returned JSON: %s", json.dumps(get_data) if get_data is not None else "<no-json>")
                            if get_data is not None:
                                # try the same candidate extraction on the GET result
                                cands2 = extract_candidates_from_dict(get_data)
                                if cands2:
                                    for c in cands2:
                                        if c.startswith("gs://"):
                                            return c
                                    return cands2[0]
                                found2 = recursive_find_uri(get_data)
                                if found2:
                                    return found2
                        except Exception as e_get:
                            logger.debug("Follow-up GET for resource name failed: %s", str(e_get))
                            # continue to other fallbacks

                # 2) Fallback: regex search raw text for gs:// or http(s)://
                m = re.search(r'(gs://[^\s"\'\\<>]+)', raw_text)
                if not m:
                    m = re.search(r'(https?://[^\s"\'\\<>]+)', raw_text)
                if m:
                    return m.group(1)

                # 3) If status is 200 but we couldn't find a URI, as a last resort
                # return the resource name if present (may be usable by other APIs).
                if data is not None and isinstance(data, dict):
                    if "name" in data and isinstance(data["name"], str):
                        return data["name"]

                # If nothing found, raise to trigger retry logic
                raise RuntimeError(f"Unexpected Gemini file upload response structure; status={resp.status_code}")
            except Exception as e:
                last_exc = e
                logger.warning("Upload attempt %s failed: %s", attempt, str(e))
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_factor * attempt)
                else:
                    logger.error("All upload attempts failed.")
                    # include the last response text in the exception if available for debugging
                    raise last_exc


    async def _query_gemini_file(self, file_uri: str, prompt: str) -> Union[str, Dict[str, Any]]:
        """Query uploaded file with Gemini"""
        url = f"{self.base_url}/models/gemini-1.5-flash:generateContent"

        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"file_data": {"file_uri": file_uri}}
                ]
            }],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 8192
            }
        }

        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(url, params={"key": self.api_key}, json=payload)
                    response.raise_for_status()
                    data = response.json()

                # The earlier code expected data["candidates"][0]["content"]["parts"][0]["text"]
                # We'll safely navigate this structure.
                if "candidates" in data and isinstance(data["candidates"], list) and len(data["candidates"]) > 0:
                    cand = data["candidates"][0]
                    # In some formats the content is in cand["content"]["parts"] or cand["output"]
                    content_text = None
                    if "content" in cand and isinstance(cand["content"], dict):
                        parts = cand["content"].get("parts") or []
                        if parts and isinstance(parts, list) and len(parts) > 0 and isinstance(parts[0], dict):
                            # the parts may be dicts with "text" keys or strings
                            part0 = parts[0]
                            if isinstance(part0, dict) and "text" in part0:
                                content_text = part0["text"]
                            elif isinstance(part0, str):
                                content_text = part0
                    # fallback to looking for text in nested places
                    if content_text is None:
                        # try common alternative locations
                        text_fragments = []
                        def _gather_text(obj):
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    _gather_text(v)
                            elif isinstance(obj, list):
                                for item in obj:
                                    _gather_text(item)
                            elif isinstance(obj, str):
                                text_fragments.append(obj)
                        _gather_text(cand)
                        content_text = "\n".join(text_fragments) if text_fragments else None

                    if content_text is not None:
                        return content_text

                # As a fallback return the raw JSON (caller will try to parse)
                return json.dumps(data)
            except Exception as e:
                last_exc = e
                logger.warning("Query attempt %s failed: %s", attempt, str(e))
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_factor * attempt)
                else:
                    logger.error("All Gemini query attempts failed.")
                    raise last_exc

    def _convert_to_elements(self, extracted_data: Dict) -> List[Dict]:
        """Convert Gemini extraction to standardized element format"""
        elements: List[Dict] = []

        # Process sections
        for section in extracted_data.get("sections", []):
            elements.append({
                "type": "Title",
                "text": section.get("title", ""),
                "metadata": {
                    "level": section.get("level", 1),
                    "page": section.get("page")
                }
            })

            if section.get("content"):
                elements.append({
                    "type": "Text",
                    "text": section.get("content", ""),
                    "metadata": {"page": section.get("page")}
                })

            # Process subsections recursively if present
            for subsection in section.get("subsections", []):
                elements.append({
                    "type": "Title",
                    "text": subsection.get("title", ""),
                    "metadata": {
                        "level": subsection.get("level", 2),
                        "page": subsection.get("page")
                    }
                })
                if subsection.get("content"):
                    elements.append({
                        "type": "Text",
                        "text": subsection.get("content", ""),
                        "metadata": {"page": subsection.get("page")}
                    })

        # Add tables
        for table in extracted_data.get("tables", []):
            elements.append({
                "type": "Table",
                "text": table.get("markdown", "") if isinstance(table, dict) else str(table),
                "metadata": {"page": table.get("page") if isinstance(table, dict) else None}
            })

        # Add images
        for image in extracted_data.get("images", []):
            elements.append({
                "type": "Image",
                "text": f"[IMAGE: {image.get('description', 'Visual content')}]"
                        if isinstance(image, dict) else f"[IMAGE: {str(image)}]",
                "metadata": {"page": image.get("page") if isinstance(image, dict) else None}
            })

        # If no sections/tables/images found but full_text exists, add as single text element
        if not elements and extracted_data.get("full_text"):
            elements.append({
                "type": "Text",
                "text": extracted_data.get("full_text", ""),
                "metadata": {}
            })

        return elements

    # ========== DUPLICATE & VERSION MANAGEMENT ==========

    async def _check_duplicate_or_version(
        self,
        content_hash: str,
        filename: str,
        user_id: str,
        db: Session
    ) -> Dict[str, Any]:
        """Check for duplicates or existing versions"""
        # Check exact content match
        exact_sql = text("""
            SELECT id, filename, version, upload_date, document_summary
            FROM documents
            WHERE content_hash = :hash AND user_id = :user_id AND status = 'active'
            LIMIT 1
        """)
        exact_match = db.execute(exact_sql, {"hash": content_hash, "user_id": user_id}).fetchone()

        if exact_match:
            return {
                "is_duplicate": True,
                "duplicate_type": "exact",
                "existing": {
                    "id": exact_match[0],
                    "filename": exact_match[1],
                    "version": exact_match[2],
                    "upload_date": exact_match[3].isoformat() if exact_match[3] else None,
                    "summary": exact_match[4]
                }
            }

        # Check same filename (potential version)
        same_filename_sql = text("""
            SELECT id, filename, version, content_hash, upload_date
            FROM documents
            WHERE filename = :filename AND user_id = :user_id AND status = 'active'
            ORDER BY version DESC
            LIMIT 1
        """)
        same_filename = db.execute(same_filename_sql, {"filename": filename, "user_id": user_id}).fetchone()

        if same_filename:
            return {
                "is_duplicate": True,
                "duplicate_type": "filename",
                "existing": {
                    "id": same_filename[0],
                    "filename": same_filename[1],
                    "version": same_filename[2],
                    "upload_date": same_filename[4].isoformat() if same_filename[4] else None
                },
                "suggested_action": "new_version"
            }

        return {"is_duplicate": False}

    async def handle_user_choice(
        self,
        action: str,
        new_file_content: bytes,
        new_filename: str,
        existing_doc_id: str,
        user_id: str,
        db: Session
    ) -> Dict[str, Any]:
        """Handle user's choice for duplicate/version"""
        action = action.lower()
        if action == "replace":
            # Delete old document embeddings and mark replaced
            db.execute(text("DELETE FROM vector_embeddings WHERE document_id = :id"), {"id": existing_doc_id})
            db.execute(text("UPDATE documents SET status = 'replaced' WHERE id = :id"), {"id": existing_doc_id})
            db.commit()

            # Upload as new
            return await self.upload_document(new_file_content, new_filename, user_id, db)

        elif action == "new_version":
            # Archive old version
            old_doc = db.execute(text("""
                SELECT filename, version, content_hash 
                FROM documents WHERE id = :id
            """), {"id": existing_doc_id}).fetchone()

            if not old_doc:
                raise RuntimeError("Original document not found for creating new version")

            old_version_num = old_doc[1] or 1
            new_version_number = old_version_num + 1

            # Create version record
            db.execute(text("""
                INSERT INTO document_versions 
                (id, original_document_id, version_number, filename, content_hash, created_by)
                VALUES (:id, :orig_id, :version, :filename, :hash, :user)
            """), {
                "id": str(uuid.uuid4()),
                "orig_id": existing_doc_id,
                "version": old_version_num,
                "filename": old_doc[0],
                "hash": old_doc[2],
                "user": user_id
            })

            # Mark old as archived
            db.execute(text("UPDATE documents SET status = 'archived' WHERE id = :id"), {"id": existing_doc_id})
            db.commit()

            # Upload new version
            result = await self.upload_document(new_file_content, new_filename, user_id, db)

            # Update version number on the newly created document row
            db.execute(text("UPDATE documents SET version = :v, parent_document_id = :parent WHERE id = :id"),
                       {"v": new_version_number, "parent": existing_doc_id, "id": result["document_id"]})
            db.commit()

            result["version"] = new_version_number
            return result

        elif action == "keep_both":
            # Upload as completely new document
            return await self.upload_document(new_file_content, new_filename, user_id, db)

        else:  # cancel or unknown
            return {"status": "cancelled"}

    # ========== FUNCTIONAL CHUNKING ==========

    def _build_document_hierarchy(self, elements: List[Dict]) -> Dict[str, Any]:
        """Build hierarchical structure from elements"""
        hierarchy = {"sections": [], "content": []}
        current_section = None
        current_subsection = None

        for element in elements:
            elem_type = element.get("type")
            text = element.get("text", "")
            metadata = element.get("metadata", {})

            if elem_type == "Title":
                level = metadata.get("level", 1)

                if level == 1:
                    current_section = {
                        "title": text,
                        "level": 1,
                        "page": metadata.get("page"),
                        "subsections": [],
                        "content": []
                    }
                    hierarchy["sections"].append(current_section)
                    current_subsection = None

                elif level == 2 and current_section:
                    current_subsection = {
                        "title": text,
                        "level": 2,
                        "page": metadata.get("page"),
                        "content": []
                    }
                    current_section["subsections"].append(current_subsection)

            elif elem_type in ["Table", "Image"]:
                content_item = {
                    "type": elem_type.lower(),
                    "text": text,
                    "page": metadata.get("page")
                }

                if current_subsection:
                    current_subsection["content"].append(content_item)
                elif current_section:
                    current_section["content"].append(content_item)
                else:
                    hierarchy["content"].append(content_item)

            else:  # Text
                content_item = {
                    "type": "text",
                    "text": text,
                    "page": metadata.get("page")
                }

                if current_subsection:
                    current_subsection["content"].append(content_item)
                elif current_section:
                    current_section["content"].append(content_item)
                else:
                    hierarchy["content"].append(content_item)

        return hierarchy

    def _chunk_by_function(self, hierarchy: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Create functional chunks based on document structure"""
        chunks: List[Union[Dict[str, Any], List[Dict[str, Any]]]] = []

        # Process each section
        for section in hierarchy.get("sections", []):
            section_title = section["title"]

            # Process subsections
            if section.get("subsections"):
                for subsection in section["subsections"]:
                    chunk = self._create_chunk(
                        content=subsection.get("content", []),
                        section=section_title,
                        subsection=subsection.get("title"),
                        page=subsection.get("page")
                    )
                    if chunk:
                        if isinstance(chunk, list):
                            chunks.extend(chunk)
                        else:
                            chunks.append(chunk)

            # Process section-level content
            if section.get("content"):
                chunk = self._create_chunk(
                    content=section.get("content", []),
                    section=section_title,
                    page=section.get("page")
                )
                if chunk:
                    if isinstance(chunk, list):
                        chunks.extend(chunk)
                    else:
                        chunks.append(chunk)

        # Process non-section content
        if hierarchy.get("content"):
            chunk = self._create_chunk(
                content=hierarchy.get("content", []),
                section="Introduction"
            )
            if chunk:
                if isinstance(chunk, list):
                    # insert introduction chunks at beginning
                    for c in reversed(chunk):
                        chunks.insert(0, c)
                else:
                    chunks.insert(0, chunk)

        return chunks

    def _create_chunk(
        self,
        content: List[Dict],
        section: str,
        subsection: str = None,
        page: int = None
    ) -> Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]:
        """Create a single chunk from content items"""
        if not content:
            return None

        # Build chunk text with context
        parts: List[str] = [f"# {section}"]
        if subsection:
            parts.append(f"## {subsection}")
        parts.append("")

        chunk_type = "text"

        for item in content:
            if item.get("type") == "table":
                parts.append(f"\n[TABLE]\n{item.get('text','')}\n")
                chunk_type = "table"
            elif item.get("type") == "image":
                parts.append(f"\n{item.get('text','')}\n")
                if chunk_type == "text":
                    chunk_type = "image"
            else:
                parts.append(item.get("text", ""))

        chunk_text = "\n".join(parts)

        # Split if too large (>2000 tokens ≈ 8000 chars)
        if len(chunk_text) > 8000:
            sub_chunks: List[Dict[str, Any]] = []
            current: List[str] = []
            current_len = 0

            header_base = [f"# {section}"]
            if subsection:
                header_base.append(f"## {subsection}")
            header_str = "\n".join(header_base) + "\n\n"
            current = header_base[:]
            current_len = len("\n".join(current))

            for part in parts[len(header_base):]:
                part_len = len(part)
                if current_len + part_len > 8000 and current:
                    sub_chunks.append({
                        "text": "\n".join(current),
                        "section": section,
                        "subsection": subsection,
                        "page": page,
                        "type": chunk_type
                    })
                    current = header_base[:]
                    current_len = len("\n".join(current))
                current.append(part)
                current_len += part_len

            if current:
                sub_chunks.append({
                    "text": "\n".join(current),
                    "section": section,
                    "subsection": subsection,
                    "page": page,
                    "type": chunk_type
                })

            return sub_chunks

        return {
            "text": chunk_text,
            "section": section,
            "subsection": subsection,
            "page": page,
            "type": chunk_type
        }

    # ========== EMBEDDING & STORAGE ==========

    async def _embed_and_store_chunks(
        self,
        chunks: List[Dict],
        doc_id: str,
        filename: str,
        user_id: str,
        db: Session,
        batch_size: int = 50
    ):
        """Generate embeddings and store in batches"""

        # Flatten chunks if needed (from _create_chunk splitting)
        flat_chunks: List[Dict[str, Any]] = []
        for chunk in chunks:
            if isinstance(chunk, list):
                flat_chunks.extend(chunk)
            else:
                flat_chunks.append(chunk)

        total = len(flat_chunks)

        for i in range(0, total, batch_size):
            batch = flat_chunks[i:i + batch_size]
            texts = [c["text"] for c in batch]

            # Generate embeddings
            embeddings = await self._generate_embeddings_batch(texts)

            # Store
            for j, (chunk, embedding) in enumerate(zip(batch, embeddings)):
                chunk_idx = i + j
                # emb_str must be like "[0.123,0.456,...]"
                emb_str = "[" + ",".join(map(lambda v: format(float(v), ".18g"), embedding)) + "]"

                metadata = {
                    "filename": filename,
                    "user_id": user_id
                }

                insert_sql = text("""
                    INSERT INTO vector_embeddings
                    (document_id, chunk_index, chunk_type, section_title, 
                     subsection_title, text_content, embedding, page_number, metadata_json, created_at)
                    VALUES (:doc_id, :idx, :type, :section, :subsection, :text,
                            (:emb)::vector, :page, (:meta)::jsonb, :created_at)
                """)
                params = {
                    "doc_id": doc_id,
                    "idx": chunk_idx,
                    "type": chunk.get("type", "text"),
                    "section": chunk.get("section"),
                    "subsection": chunk.get("subsection"),
                    "text": chunk["text"],
                    "emb": emb_str,
                    "page": chunk.get("page"),
                    "meta": json.dumps(metadata),
                    "created_at": datetime.now(timezone.utc)
                }

                db.execute(insert_sql, params)

            db.commit()
            logger.info("  Batch %d/%d stored", (i // batch_size) + 1, (total + batch_size - 1) // batch_size)

    async def _generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings using Gemini API"""
        url = f"{self.base_url}/models/text-embedding-004:batchEmbedContents"

        payload = {
            "requests": [
                {"model": "models/text-embedding-004", "content": {"parts": [{"text": t}]}}
                for t in texts
            ]
        }

        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, params={"key": self.api_key}, json=payload)
                    response.raise_for_status()
                    data = response.json()

                # Expecting data["embeddings"] as list of {"values": [...]}
                if "embeddings" in data:
                    out = []
                    for emb in data["embeddings"]:
                        if isinstance(emb, dict) and "values" in emb:
                            out.append(emb["values"])
                        elif isinstance(emb, list):
                            out.append(emb)
                        else:
                            raise RuntimeError("Unexpected embedding format")
                    return out

                # Fallback: try data["data"]
                if "data" in data and isinstance(data["data"], list):
                    return [d.get("embedding") or d.get("values") for d in data["data"]]

                raise RuntimeError("Unexpected embeddings response structure")
            except Exception as e:
                last_exc = e
                logger.warning("Embedding attempt %s failed: %s", attempt, str(e))
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_factor * attempt)
                else:
                    logger.error("All embedding attempts failed.")
                    raise last_exc

    async def _generate_document_summary(self, first_chunks: List[Dict]) -> str:
        """Generate document summary from first few chunks"""
        intro_text = "\n\n".join([c["text"][:1000] for c in first_chunks if c.get("text")])

        url = f"{self.base_url}/models/gemini-1.5-flash:generateContent"
        prompt = f"Summarize this document in 2-3 sentences:\n\n{intro_text}"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 200}
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, params={"key": self.api_key}, json=payload)
            response.raise_for_status()
            data = response.json()

        # Safely extract result text
        try:
            cand = data["candidates"][0]
            if "content" in cand and isinstance(cand["content"], dict):
                parts = cand["content"].get("parts") or []
                if parts and isinstance(parts, list) and "text" in parts[0]:
                    return parts[0]["text"]
            # fallback: stringify
            return json.dumps(data)[:1000]
        except Exception:
            return ""

    # ========== QUERYING ==========

    async def query_documents(
        self,
        question: str,
        user_id: str,
        document_id: Optional[str] = None,
        section: Optional[str] = None,
        top_k: int = 5,
        db: Session = None
    ) -> Dict[str, Any]:
        """Query documents with RAG"""
        if db is None:
            raise ValueError("db session is required")

        # Generate query embedding
        query_emb = await self._generate_embeddings_batch([question])
        emb_str = "[" + ",".join(map(lambda v: format(float(v), ".18g"), query_emb[0])) + "]"

        # Build query with filters
        filters = ["d.user_id = :user_id", "d.status = 'active'"]
        params = {"emb": emb_str, "user_id": user_id, "limit": top_k}

        if document_id:
            filters.append("v.document_id = :doc_id")
            params["doc_id"] = document_id

        if section:
            filters.append("v.section_title ILIKE :section")
            params["section"] = f"%{section}%"

        where_clause = " AND ".join(filters)

        # Search
        search_sql = text(f"""
            SELECT v.document_id, v.chunk_index, v.text_content, v.section_title,
                   v.subsection_title, v.chunk_type, v.page_number, d.filename,
                   v.embedding <=> :emb::vector as distance
            FROM vector_embeddings v
            JOIN documents d ON v.document_id = d.id
            WHERE {where_clause}
            ORDER BY distance
            LIMIT :limit
        """)
        results = db.execute(search_sql, params).fetchall()

        # Extract context
        context = []
        for r in results:
            chunk_type = r[5]
            text_content = r[2] or ""
            if chunk_type == "table":
                context.append(f"[FROM TABLE in {r[3]}]\n{text_content}")
            elif chunk_type == "image":
                context.append(f"[FROM IMAGE in {r[3]}]\n{text_content}")
            else:
                context.append(text_content)

        # Generate answer
        answer = await self._generate_answer(question, context)

        # Format sources
        sources = []
        for r in results:
            sources.append({
                "document_id": r[0],
                "filename": r[7],
                "section": r[3],
                "subsection": r[4],
                "page": r[6],
                "type": r[5],
                "text_preview": (r[2] or "")[:300] + ("..." if (r[2] or "") and len(r[2]) > 300 else "")
            })

        return {
            "answer": answer,
            "sources": sources,
            "query": question
        }

    async def _generate_answer(self, query: str, context: List[str]) -> str:
        """Generate answer using Gemini"""
        url = f"{self.base_url}/models/gemini-1.5-flash:generateContent"

        context_text = "\n\n".join(context)
        prompt = f"""Answer the question based on the provided context.
If the answer is not in the context, say so clearly.
Always cite which section or document part you used.

Context:
{context_text}

Question: {query}

Answer:"""

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048}
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, params={"key": self.api_key}, json=payload)
            response.raise_for_status()
            data = response.json()

        try:
            cand = data["candidates"][0]
            if "content" in cand and isinstance(cand["content"], dict):
                parts = cand["content"].get("parts") or []
                if parts and isinstance(parts, list) and "text" in parts[0]:
                    return parts[0]["text"]
            # fallback: stringify best-effort
            return json.dumps(data)[:2000]
        except Exception:
            return "Unable to generate answer from model response."

    def _compute_content_hash(self, text: str) -> str:
        """Compute content hash for duplicate detection"""
        normalized = " ".join(text.lower().split())
        return hashlib.sha256(normalized.encode()).hexdigest()
