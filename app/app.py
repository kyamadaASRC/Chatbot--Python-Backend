"""Flask application for chat, template selection/editing, and file helpers."""

import copy
import io
import os
import uuid
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
from docx import Document
from openpyxl import Workbook

from app.openai_client import client
import openai
from app.Select_Edit_Docx import select_and_edit_docx
from app.consultants import _extract_id


CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-5")
RESPONSE_TIMEOUT = int(os.getenv("RESPONSE_TIMEOUT", "240"))
GENERAL_CHAT_SYSTEM = """You are a helpful assistant. Keep answers concise unless the user asks for more detail.
Tool hand-offs:
- When reading user uploads, call file_search first (session stores are linked) or code_interpreter to inspect/transform files.
- To return documents, call generate_pdf(markdown_text=...) or generate_xlsx(...).
- Use select_and_edit_docx to pick a template from the manifest store and optionally apply edits.
- Use web_search_preview or code_interpreter when they materially improve the answer.
Respond using Markdown syntax for code and always wrap code in fenced blocks (```), leaving a blank line before and after each block.
If you cannot access the data, just say so and do not provide terminal commands.
Otherwise, reply normally in raw Markdown."""
DIRECT_TOOL_NAMES = [
    "file_search",
    "generate_pdf",
    "generate_xlsx",
    "web_search_preview",
    "code_interpreter",
    "select_and_edit_docx",
]

# Tool specs for direct assistant calls (docs + template selection/edit).
DOC_TOOL_SPECS = [
    {
        "type": "function",
        "name": "generate_pdf",
        "description": "Convert markdown text into a downloadable PDF.",
        "parameters": {
            "type": "object",
            "properties": {
                "markdown_text": {
                    "type": "string",
                    "description": "The Markdown content to be converted into a PDF document.",
                },
            },
            "required": ["markdown_text"],
        },
    },
    {
        "type": "function",
        "name": "generate_xlsx",
        "description": "Create an .xlsx workbook from structured row data.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Optional name for the generated .xlsx file.",
                },
                "sheets": {
                    "type": "array",
                    "description": "List of worksheets to include. Each sheet must define a name and rows.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Worksheet name (31 chars max)."},
                            "rows": {
                                "type": "array",
                                "description": "Rows of data; each row is an array of cell values.",
                                "items": {
                                    "type": "array",
                                    "items": {},
                                },
                            },
                        },
                        "required": ["rows"],
                    },
                },
            },
            "required": ["sheets"],
        },
    },
    {
        "type": "function",
        "name": "select_and_edit_docx",
        "description": "Pick the best-matching DOCX template from the manifest store and optionally apply edits.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "User request that describes the needed template.",
                },
                "edit_instructions": {
                    "type": "string",
                    "description": "Optional instructions to apply to the selected template.",
                },
                "file_id": {
                    "type": "string",
                    "description": "Optional file_id to edit directly (skips template selection).",
                },
                "vector_store_id": {
                    "type": "string",
                    "description": "Optional vector store to attach generated files to.",
                },
            },
            "required": ["prompt"],
        },
    },
]
_TOOL_LOGGED = False



def _serialize(obj):
    """Convert pydantic/BaseModel-like objects into plain dicts for logging."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def _ensure_dict(value: Any) -> Dict[str, Any]:
    """Normalize SDK objects into dicts so later code can safely call .get()."""
    if isinstance(value, dict):
        return value
    serialized = _serialize(value)
    return serialized if isinstance(serialized, dict) else {}


def _link_files_to_vector_store(
    file_ids: Optional[List[str]],
    vector_store_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Attach OpenAI file IDs to an existing session vector store."""
    attached: List[Dict[str, Any]] = []
    if not vector_store_id or not file_ids:
        return attached

    for file_id in file_ids:
        if not file_id:
            continue
        # Link each generated file to the caller's vector store so future file_search calls can see it.
        try:
            client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=file_id)
        except Exception as exc:
            print(f"[vector-store] Failed to link file {file_id} to {vector_store_id}: {exc}")
        file_meta: Dict[str, Any] = {}
        try:
            meta = client.files.retrieve(file_id)
            file_meta = _ensure_dict(meta)
        except Exception as exc:
            print(f"[files] Failed to retrieve metadata for {file_id}: {exc}")
        attached.append(
            {
                "id": file_id,
                "openai_file_id": file_id,
                "name": file_meta.get("filename") or file_meta.get("display_name") or file_meta.get("id") or file_id,
                "size": file_meta.get("bytes"),
                "vector_store_id": vector_store_id,
                "created_at": file_meta.get("created_at"),
                "source": "generated",
            }
        )
    return attached


def _fetch_file_metadata(file_id: str, source: str = "generated") -> Dict[str, Any]:
    """Build the metadata object that the UI expects for downloadable files."""
    def _guess_mime(name: str) -> Optional[str]:
        lowered = name.lower()
        if lowered.endswith(".pdf"):
            return "application/pdf"
        if lowered.endswith(".docx"):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if lowered.endswith(".xlsx"):
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if lowered.endswith(".json"):
            return "application/json"
        if lowered.endswith(".txt"):
            return "text/plain"
        return None

    def _cache_file_locally(name: str) -> Optional[str]:
        """Attempt to download from OpenAI and cache for browser preview; return local URL."""
        safe_name = _sanitize_filename(name, Path(name).suffix or ".bin")
        local_dir = Path("artifacts/generated_files")
        local_dir.mkdir(parents=True, exist_ok=True)
        target = local_dir / f"{file_id}_{safe_name}"
        try:
            content = client.files.content(file_id)
            data = content.read() if hasattr(content, "read") else content
            if isinstance(data, str):
                data = data.encode("utf-8")
            if not isinstance(data, (bytes, bytearray)):
                return None
            target.write_bytes(data)
            return f"/local_files/{target.name}"
        except Exception as exc:
            print(f"[local-cache] Failed to cache file {file_id}: {exc}")
            return None

    try:
        # Grab file info from OpenAI so filenames + sizes stay accurate when the UI renders them.
        meta = client.files.retrieve(file_id)
        data = _ensure_dict(meta)
    except Exception:
        data = {}
    name = data.get("filename") or data.get("display_name") or file_id
    mime = data.get("mime_type") or _guess_mime(name)
    preview_url = _cache_file_locally(name)
    return {
        "id": file_id,
        "openai_file_id": file_id,
        "name": name,
        "size": data.get("bytes"),
        "vector_store_id": None,
        "created_at": data.get("created_at"),
        "source": source,
        "mime": mime,
        "preview_url": preview_url,
    }


def _sanitize_filename(name: Optional[str], suffix: str) -> str:
    """Ensure we return filesystem-safe filenames with the proper suffix."""
    base = (name or "").strip() or f"assistant_output{suffix}"
    if not base.lower().endswith(suffix):
        base = f"{base}{suffix}"
    safe = re.sub(r"[^\w.\-]+", "_", base)
    if not safe:
        safe = f"assistant_output{suffix}"
    return safe


def _make_temp_path(suffix: str) -> Path:
    """Create a temporary file path for DOCX/XLSX generation."""
    # Use NamedTemporaryFile so downstream libraries can write directly to disk.
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = Path(temp.name)
    temp.close()
    return temp_path


def _upload_generated_file(path: Path, filename: str, mimetype: str = "application/octet-stream") -> Optional[str]:
    """Upload a server-generated artifact to OpenAI Files and clean up the temp file."""
    try:
        with path.open("rb") as handle:
            # Treat the generated doc as if the user uploaded it so vector stores/containers can reuse the same APIs.
            uploaded = client.files.create(
                file=(filename, handle, mimetype),
                purpose="assistants",
            )
        return _extract_id(uploaded) # type: ignore
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _markdown_to_docx(document: Document, markdown_text: str) -> None:  # type: ignore
    """Very small Markdown → paragraph/heading renderer for DOCX generation."""
    for raw_line in (markdown_text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if not stripped:
            document.add_paragraph("")
            continue
        if stripped.startswith("### "):
            document.add_heading(stripped[4:], level=3)
        elif stripped.startswith("## "):
            document.add_heading(stripped[3:], level=2)
        elif stripped.startswith("# "):
            document.add_heading(stripped[2:], level=1)
        elif re.match(r"^\d+\.\s", stripped):
            document.add_paragraph(stripped, style="List Number")
        elif stripped.startswith(("- ", "* ")):
            document.add_paragraph(stripped[2:], style="List Bullet")
        else:
            document.add_paragraph(line)


def _handle_generate_docx_tool(args: Dict[str, Any], vector_store_id: Optional[str]) -> List[Dict[str, Any]]:
    """Fulfill the `generate_docx` tool call and return uploaded file metadata."""
    markdown_text = args.get("markdown_text")
    if not markdown_text:
        return []
    filename = _sanitize_filename(args.get("filename"), ".docx")
    document = Document()
    # Render each Markdown line into basic DOCX structures so consultants can call a single helper.
    _markdown_to_docx(document, markdown_text)
    temp_path = _make_temp_path(".docx")
    document.save(temp_path) # type: ignore
    file_id = _upload_generated_file(temp_path, filename, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if not file_id:
        return []
    if vector_store_id:
        linked = _link_files_to_vector_store([file_id], vector_store_id)
        if linked:
            return linked
    return [_fetch_file_metadata(file_id)]


def _handle_generate_xlsx_tool(args: Dict[str, Any], vector_store_id: Optional[str]) -> List[Dict[str, Any]]:
    """Fulfill the `generate_xlsx` tool call by building sheets with openpyxl."""
    sheets = args.get("sheets")
    rows = args.get("rows")
    if not sheets and rows:
        sheets = [{"name": "Sheet1", "rows": rows}]
    if not isinstance(sheets, list) or not sheets:
        return []
    filename = _sanitize_filename(args.get("filename"), ".xlsx")
    wb = Workbook()
    first_sheet = True
    for sheet_def in sheets:
        if not isinstance(sheet_def, dict):
            continue
        # Create one worksheet per descriptor and stream rows into openpyxl.
        title = (sheet_def.get("name") or "Sheet").strip() or "Sheet"
        sheet_rows = sheet_def.get("rows") or []
        ws = wb.active if first_sheet else wb.create_sheet()
        first_sheet = False
        ws.title = title[:31] # type: ignore
        for row in sheet_rows:
            if isinstance(row, list):
                ws.append(row) # type: ignore
            else:
                ws.append([row]) # type: ignore
    temp_path = _make_temp_path(".xlsx")
    wb.save(temp_path)
    file_id = _upload_generated_file(temp_path, filename, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if not file_id:
        return []
    if vector_store_id:
        linked = _link_files_to_vector_store([file_id], vector_store_id)
        if linked:
            return linked
    return [_fetch_file_metadata(file_id)]


def _coerce_tool_args(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Responses API delivers tool args under different keys; normalize them."""
    args = entry.get("arguments") or entry.get("function", {}).get("arguments") or {}
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            return {}
    if isinstance(args, dict):
        return args
    return {}


def _process_server_tool_calls(data: Dict[str, Any], vector_store_id: Optional[str], container_id: Optional[str] = None) -> Dict[str, Any]:
    """Look for server-side function calls (DOCX/XLSX/selection) and synthesize files."""
    generated: List[Dict[str, Any]] = []
    messages: List[str] = []
    active_vector_store = vector_store_id
    outputs = data.get("output") or []
    container_id = container_id or data.get("container_id") or None
    # Capture any container file ids emitted by code interpreter
    container_file_map: Dict[str, str] = {}
    for entry in outputs:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "output_file":
            continue
        cid = entry.get("container_id") or entry.get("container", {}).get("id")
        cfile = entry.get("file_id") or entry.get("id")
        if cid and cfile:
            container_file_map[cfile] = cid

    for entry in outputs:
        entry_type = entry.get("type")
        if entry_type not in ("function_call", "output_tool_call"):
            continue
        name = entry.get("name") or entry.get("function", {}).get("name")
        if not name:
            continue
        args = _coerce_tool_args(entry)
        if name == "generate_xlsx":
            generated.extend(_handle_generate_xlsx_tool(args, active_vector_store))
        elif name == "select_and_edit_docx":
            prompt = (args.get("prompt") or "").strip()
            if not prompt:
                messages.append("select_and_edit_docx: missing prompt; skipping.")
                continue
            edit_instructions = (args.get("edit_instructions") or args.get("instructions") or "").strip()
            file_id = (args.get("file_id") or "").strip() or None
            target_vs = args.get("vector_store_id") or active_vector_store
            result = select_and_edit_docx(
                prompt=prompt,
                edit_instructions=edit_instructions or None,
                file_id=file_id,
                vector_store_id=target_vs,
            )
            # Prefer any container_id hinted by the tool itself if we didn't see it in outputs
            if not container_id and result.get("container_id"):
                container_id = result["container_id"]
            if result.get("message"):
                messages.append(result["message"])
            result_vs = result.get("vector_store_id") or target_vs
            if result_vs and not active_vector_store:
                active_vector_store = result_vs
            output_file_ids = result.get("file_ids") or []
            if output_file_ids:
                effective_vs = result_vs or target_vs
                if effective_vs:
                    linked = _link_files_to_vector_store(output_file_ids, effective_vs)
                    # annotate with container ids if we have them
                    for rec in linked:
                        cid = container_file_map.get(rec["openai_file_id"])
                        if not cid:
                            cid = container_id
                        if cid:
                            rec["container_id"] = container_id or cid
                            rec["container_file_id"] = rec["openai_file_id"]
                    generated.extend(linked)
                else:
                    for fid in output_file_ids:
                        rec = _fetch_file_metadata(fid)
                        cid = container_file_map.get(fid)
                        if not cid:
                            cid = container_id
                        if cid:
                            rec["container_id"] = container_id or cid
                            rec["container_file_id"] = fid
                        generated.append(rec)
            # Attach locally cached files from base64 outputs, if present
            local_files = result.get("local_files") or []
            for lf in local_files:
                generated.append(lf)
    # If any generated entries match container_file_map, enrich them
    for rec in generated:
        fid = rec.get("openai_file_id") or rec.get("id")
        if fid and fid in container_file_map:
            rec["container_id"] = container_id or container_file_map[fid]
            rec["container_file_id"] = fid
    return {
        "generated_files": generated,
        "messages": messages,
        "vector_store_id": active_vector_store,
    }


def _normalize_history_for_model(history: Optional[List[Dict[str, Any]]], limit: int = 8) -> List[Dict[str, str]]:
    """Trim conversation history to recent user/assistant turns for assistant calls."""
    if not isinstance(history, list) or not history:
        return []
    cleaned: List[Dict[str, str]] = []
    for entry in history[-limit:]:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if role not in ("user", "assistant"):
            continue
        content = entry.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        cleaned.append({"role": role, "content": text[:2000]})
    return cleaned



def create_app() -> Flask:
    """Application factory so tests and gunicorn can import the same Flask instance."""
    app = Flask(__name__)
    CORS(app)  # enable CORS for all routes
    global _TOOL_LOGGED
    if not _TOOL_LOGGED:
        print(f"[direct-tools] Built-in tools: {DIRECT_TOOL_NAMES}")
        _TOOL_LOGGED = True

    @app.route("/")
    def index():
        return render_template("chatbot.html")


    def _should_force_direct(message: str) -> bool:
        """Some tool requests (generate_pdf/docx/xlsx) only exist on the direct chat path."""
        lowered = (message or "").lower()
        triggers = [
            "generate_pdf",
            "generate docx",
            "generate pdf",
            "generate xlsx",
            "create pdf",
            "create docx",
            "create xlsx",
            "make pdf",
            "make docx",
            "make xlsx",
            "export pdf",
            "export docx",
            "export xlsx",
            "download pdf",
            "download docx",
            "download xlsx",
            "save as pdf",
            "save as docx",
            "save as xlsx",
        ]
        if any(trigger in lowered for trigger in triggers):
            return True
        # simple pairwise terms
        return (
            ("pdf" in lowered and ("generate" in lowered or "create" in lowered or "export" in lowered or "download" in lowered or "save" in lowered))
            or ("docx" in lowered and ("generate" in lowered or "create" in lowered or "export" in lowered or "download" in lowered or "save" in lowered))
            or ("xlsx" in lowered and ("generate" in lowered or "create" in lowered or "export" in lowered or "download" in lowered or "save" in lowered))
        )


    def _log_progress(log: Optional[List[Dict[str, Any]]], stage: str, **extra: Any) -> None:
        """Append a timestamped breadcrumb so the frontend can reflect pipeline stages."""
        if log is None:
            return
        entry: Dict[str, Any] = {
            "stage": stage,
            "timestamp": time.time(),
        }
        for key, value in extra.items():
            if value is not None:
                entry[key] = value
        log.append(entry)


    def _extract_text(resp) -> str:
        """Mirror `_extract_text` from consultant router so tool + summary paths stay consistent."""
        text = getattr(resp, "output_text", None)
        if isinstance(text, list):
            text = text[0] if text else ""
        if isinstance(text, str) and text.strip():
            return text.strip()
        for output in getattr(resp, "output", []) or []:
            if isinstance(output, dict):
                contents = output.get("content")
                if isinstance(contents, list):
                    for c in contents:
                        val = c.get("text") if isinstance(c, dict) else None
                        if isinstance(val, str) and val.strip():
                            return val.strip()
                txt = output.get("text")
                if isinstance(txt, str) and txt.strip():
                    return txt.strip()
        choices = getattr(resp, "choices", None)
        if choices:
            msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            val = msg.get("content")
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""



    @app.route("/chat", methods=["POST"])
    def chat():
        data = request.get_json(force=True)
        msg = data.get("message", "").strip()
        project = data.get("project", "demo-project")
        vector_store_id = data.get("vector_store_id")
        container_id = data.get("container_id")
        incoming_tools = data.get("tools")
        history_payload = data.get("history")
        history_messages = _normalize_history_for_model(history_payload)
        if not msg:
            return jsonify({"error": "Missing message"}), 400
        try:
            tools: List[Dict[str, Any]] = []
            if isinstance(incoming_tools, list):
                for tool in incoming_tools:
                    if not isinstance(tool, dict):
                        continue
                    sanitized = dict(tool)
                    if sanitized.get("name") == "generate_docx":
                        continue  # disabled
                    if sanitized.get("type") == "code_interpreter":
                        sanitized["container"] = container_id or {"type": "auto"}
                    tools.append(sanitized)

            if vector_store_id:
                has_file_search = any(isinstance(t, dict) and t.get("type") == "file_search" for t in tools)
                if not has_file_search:
                    tools.insert(0, {"type": "file_search", "vector_store_ids": [vector_store_id]})

            # Ensure doc tools + select/edit are available.
            for spec in DOC_TOOL_SPECS:
                already = any(
                    (t.get("name") == spec.get("name")) or (t.get("type") == spec.get("type"))
                    for t in tools
                )
                if not already:
                    tools.append(copy.deepcopy(spec))

            convo_input: List[Dict[str, str]] = [{"role": "system", "content": GENERAL_CHAT_SYSTEM}]
            convo_input.extend(history_messages)
            convo_input.append({"role": "user", "content": msg})
            # Retry transient connection errors to OpenAI a few times with backoff.
            resp = None
            for attempt in range(3):
                try:
                    resp = client.responses.create(
                        model=CHAT_MODEL,
                        input=convo_input, # type: ignore
                        tools=tools or None, # type: ignore
                        timeout=RESPONSE_TIMEOUT,
                    )
                    break
                except Exception as exc:
                    if isinstance(exc, openai.APITimeoutError):
                        print(f"[openai-timeout] attempt {attempt+1} timed out")
                    # jittered backoff to avoid thundering herd on retries
                    if attempt == 2:
                        raise
                    delay = 2 * (attempt + 1) + (0.5 * (attempt + 1))
                    time.sleep(delay)
            serialized = _ensure_dict(resp)
            text = _extract_text(resp) or ""
            file_ids = getattr(resp, "output_file_ids", None) or []
            generated_files: List[Dict[str, Any]] = []
            tool_results = _process_server_tool_calls(serialized, vector_store_id, container_id=container_id)
            generated_files.extend(tool_results.get("generated_files", []))
            generated_files.extend(_link_files_to_vector_store(file_ids, vector_store_id))
            tool_messages = tool_results.get("messages") or []
            if tool_messages:
                text = f"{text}\n\n" + "\n\n".join(tool_messages) if text else "\n\n".join(tool_messages)
            return jsonify({
                "text": text,
                "mode": "direct",
                "file_ids": file_ids,
                "generated_files": generated_files,
                "output": serialized.get("output"),
                "output_text": serialized.get("output_text"),
                "choices": serialized.get("choices"),
                "response_payload": serialized,
            })
        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/vector_stores", methods=["POST"])
    def create_vector_store():
        payload = request.get_json(force=True) or {}
        name = payload.get("name", "Session Vector Store")
        try:
            vs = client.vector_stores.create(name=name)
            return jsonify(_serialize(vs))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/vector_stores/<vector_store_id>", methods=["DELETE"])
    def delete_vector_store(vector_store_id):
        try:
            res = client.vector_stores.delete(vector_store_id)
            data = _serialize(res) if res else {"id": vector_store_id, "deleted": True}
            return jsonify(data)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/vector_stores/<vector_store_id>/files", methods=["POST"])
    def link_file_to_vector_store(vector_store_id):
        payload = request.get_json(force=True) or {}
        file_id = payload.get("file_id")
        if not file_id:
            return jsonify({"error": "file_id is required"}), 400
        try:
            res = client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=file_id)
            return jsonify(_serialize(res))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/files", methods=["GET"])
    def list_files():
        try:
            files = client.files.list()
            return jsonify(_serialize(files))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/files", methods=["POST"])
    def upload_file():
        if "file" not in request.files:
            return jsonify({"error": "file is required"}), 400
        file = request.files["file"]
        purpose = request.form.get("purpose", "assistants")
        try:
            uploaded = client.files.create(
                file=(file.filename, file.stream, file.mimetype or "application/octet-stream"),
                purpose=purpose, # type: ignore
            )
            return jsonify(_serialize(uploaded))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/files/<file_id>", methods=["DELETE"])
    def delete_file(file_id):
        try:
            res = client.files.delete(file_id)
            return jsonify(_serialize(res))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/files/<file_id>/content", methods=["GET"])
    def get_file_content(file_id):
        try:
            content = client.files.content(file_id)
            if hasattr(content, "read"):
                data = content.read()
            else:
                data = content
            # Try to use the original filename for download hint.
            filename = None
            try:
                meta = client.files.retrieve(file_id)
                meta_dict = _ensure_dict(meta)
                filename = meta_dict.get("filename") or meta_dict.get("display_name")
            except Exception:
                filename = None
            download_name = filename or f"{file_id}.bin"
            return send_file(
                io.BytesIO(data), # type: ignore
                download_name=download_name,
                mimetype="application/octet-stream",
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/containers/<container_id>/files/<file_id>/content", methods=["GET"])
    def get_container_file_content(container_id, file_id):
        try:
            content = client.containers.files.content(container_id=container_id, file_id=file_id) # type: ignore
            data = content.read() if hasattr(content, "read") else content
            if isinstance(data, str):
                data = data.encode("utf-8")
            download_name = request.args.get("name") or f"{file_id}.bin"
            return send_file(
                io.BytesIO(data), # type: ignore
                download_name=download_name,
                mimetype="application/octet-stream",
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/local_files/<path:fname>", methods=["GET"])
    def get_local_file(fname):
        local_path = Path("artifacts/generated_files") / fname
        if not local_path.exists():
            return jsonify({"error": "file not found"}), 404
        return send_file(
            local_path.open("rb"),
            download_name=fname,
            mimetype="application/octet-stream",
        )

    @app.route("/v1/containers", methods=["POST"])
    def create_container_runtime():
        payload = request.get_json(force=True) or {}
        name = payload.get("name") or f"Session Container - {uuid.uuid4()}"
        expires_after = payload.get("expires_after")
        file_ids = payload.get("file_ids")
        kwargs: Dict[str, Any] = {"name": name}
        if isinstance(expires_after, dict):
            kwargs["expires_after"] = expires_after
        if isinstance(file_ids, list) and file_ids:
            kwargs["file_ids"] = file_ids
        try:
            container = client.containers.create(**kwargs) # type: ignore
            return jsonify(_serialize(container))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/containers/<container_id>", methods=["DELETE"])
    def delete_container_runtime(container_id):
        try:
            res = client.containers.delete(container_id) # type: ignore
            data = _serialize(res) if res else {"id": container_id, "deleted": True}
            return jsonify(data)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/containers/<container_id>/files", methods=["POST"])
    def upload_container_file(container_id):
        if not container_id:
            return jsonify({"error": "container_id is required"}), 400
        if request.files:
            file = request.files.get("file")
            if not file:
                return jsonify({"error": "file is required"}), 400
            file_tuple = (file.filename, file.stream, file.mimetype or "application/octet-stream")
            try:
                uploaded = client.containers.files.create( # type: ignore
                    container_id=container_id,
                    file=file_tuple,
                )
                return jsonify(_serialize(uploaded))
            except Exception as exc:
                return jsonify({"error": str(exc)}), 500
        payload = request.get_json(force=True) or {}
        file_id = payload.get("file_id")
        if not file_id:
            return jsonify({"error": "file or file_id is required"}), 400
        try:
            uploaded = client.containers.files.create( # type: ignore
                container_id=container_id,
                file_id=file_id,
            )
            return jsonify(_serialize(uploaded))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/containers/<container_id>/files/<file_id>", methods=["DELETE"])
    def delete_container_file(container_id, file_id):
        if not container_id or not file_id:
            return jsonify({"error": "container_id and file_id are required"}), 400
        try:
            client.containers.files.delete(file_id=file_id, container_id=container_id) # type: ignore
            return jsonify({"id": file_id, "deleted": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/responses", methods=["POST"])
    def create_response():
        payload = request.get_json(force=True) or {}
        try:
            resp = client.responses.create(**payload)
            return jsonify(_serialize(resp))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500


    return app


app = create_app()
CORS(app, resources={r"/*": {"origins": ["http://127.0.0.1:5501"]}})



if __name__ == "__main__":
    app.run(port=5001, debug=True)
