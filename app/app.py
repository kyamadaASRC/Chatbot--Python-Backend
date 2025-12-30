"""Flask application for chat, template selection/editing, and file helpers."""

import copy
import io
import os
import uuid
import json
import re
import tempfile
import time
import subprocess
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any, TypedDict, Tuple, Union
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
from openpyxl import Workbook

from app.openai_client import client
import openai
from app.Select_Edit_Docx import edit_docx_template, select_docx_template
from app.consultants import _extract_id


CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-5")
RESPONSE_TIMEOUT = int(os.getenv("RESPONSE_TIMEOUT", "300"))
DEBUG_LOG = os.getenv("DEBUG_LOG", "0") == "1"
CONVERSATIONS_ENABLED = os.getenv("CONVERSATIONS_ENABLED", "1") == "1"
GENERAL_CHAT_SYSTEM = """You are a helpful assistant. Keep answers concise unless the user asks for more detail. Stay friendly, avoid tool jargon, and never include download links (tell the user to check the Files list instead).
Tool hand-offs:
- When reading user uploads, call file_search first (session stores are linked) or code_interpreter to inspect/transform files.
- To return documents, call generate_pdf(markdown_text=...) or generate_xlsx(...).
- For DOCX/template work (e.g., lesson plans, forms): if no template is selected yet, proactively call select_docx based on the user request (you don’t need the user to say “use select_docx”); file_info/selection_text holds keywords/anchor points. YOU must ask the user for the anchor-aligned values (header, overview, objectives, materials, assessment, accommodations, etc.). Do not call edit_docx until you have those values; call edit_docx exactly once with complete edit instructions. Do NOT ask questions from within edit_docx. Do NOT use generate_pdf for DOCX/template requests. If you need to inspect the template or a generated DOCX, you may use code_interpreter with the relevant file_id/container_file_id to read headings/content and verify the fill before responding to the user.
- Use the conversation’s default file container; do not switch containers unless the user explicitly supplies a persistent vector_store_id. If a vector_store_id is provided, attach it to every edit_docx call and treat that store as the canonical project space.
- When iterating, pick the latest filled DOCX in the container as the source (prefer “[FILLED]”, then newest timestamp, then highest vN). Do not revert to the original template unless the user says “start fresh” or wants a new base.
- Ask for missing info in small batches, confirm the source filename and intended changes before editing, and call edit_docx exactly once per version. After editing, report the exact output filename returned and tell the user to check the Files list. Never claim a file was generated unless edit_docx returned one.
- Name outputs “{base_name} [FILLED] v{n}”, increment n each edit, and keep prior versions. Use edit_docx only for DOCX/template work (not generate_pdf). For meta/explanations, answer in text only and do not call edit_docx unless the user requests a new or corrected edit.
- If the user is asking a meta/clarifying question about what happened or why something occurred, answer directly in text. Do NOT call select_docx or edit_docx unless the user explicitly asks to run (or re-run) an edit after the explanation.
- Use web_search_preview or code_interpreter when they materially improve the answer.
- Ask for information in small batches (3–5 items), acknowledge what you received, and avoid re-asking answered items. If something is missing, list it succinctly. When errors occur, apologize briefly and suggest the next step (retry, provide missing values, etc.).
Respond using Markdown syntax for code and always wrap code in fenced blocks (```), leaving a blank line before and after each block.
If you cannot access the data, just say so and do not provide terminal commands.
Otherwise, reply normally in raw Markdown."""
DIRECT_TOOL_NAMES = [
    "file_search",
    "generate_pdf",
    "generate_xlsx",
    "web_search_preview",
    "code_interpreter",
    "select_docx",
    "edit_docx",
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
        "name": "select_docx",
        "description": "Pick the best-matching DOCX template from the manifest store (file_info holds keywords/summary for matching). If you call select_docx, you MUST follow with an edit_docx call in the same turn using the returned file_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "User request that describes the needed template.",
                },
                "vector_store_id": {
                    "type": "string",
                    "description": "Optional vector store to scope template search and attach files.",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "type": "function",
        "name": "edit_docx",
        "description": "Apply edit instructions to a selected DOCX template using code interpreter.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The file_id of the DOCX template to edit.",
                },
                "edit_instructions": {
                    "type": "string",
                    "description": "Instructions to apply to the selected template.",
                },
                "selection_text": {
                    "type": "string",
                    "description": "Optional selection summary or context from the selection tool.",
                },
                "vector_store_id": {
                    "type": "string",
                    "description": "Optional vector store to attach generated files to.",
                },
            },
            "required": ["file_id", "edit_instructions"],
        },
    },
]
_TOOL_LOGGED = False

# Lightweight DTOs to keep tool payloads structured.
class SelectionResult(TypedDict, total=False):
    file_id: str
    selection_text: str
    template_name: str
    vector_store_id: str
    message: str
    file_info: str
    response: Dict[str, Any]


class GeneratedFile(TypedDict, total=False):
    id: str
    openai_file_id: Optional[str]
    container_id: Optional[str]
    container_file_id: Optional[str]
    container_file_ids: Optional[List[str]]
    name: str
    mime: Optional[str]
    preview_url: Optional[str]
    download_url: Optional[str]
    source: str
    vector_store_id: Optional[str]
    created_at: Optional[int]
    size: Optional[int]

# Global Flask app instance for decorators below
app = Flask(__name__)
CORS(app)
if not _TOOL_LOGGED:
    print(f"[direct-tools] Built-in tools: {DIRECT_TOOL_NAMES}")
    _TOOL_LOGGED = True


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


_PREVIEW_CACHE_DIR = Path("artifacts/previews")
_PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_LOCAL_FILES_DIR = Path("artifacts/generated_files")
_LOCAL_FILES_DIR.mkdir(parents=True, exist_ok=True)


def _convert_docx_to_pdf_bytes(docx_bytes: bytes) -> Optional[bytes]:
    """Convert DOCX bytes to PDF bytes using docx2pdf. Returns None on failure."""
    try:
        try:
            import docx2pdf  # type: ignore
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "docx2pdf"])
            import docx2pdf  # type: ignore
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            src = tmpdir_path / "input.docx"
            out = tmpdir_path / "output.pdf"
            src.write_bytes(docx_bytes)
            try:
                docx2pdf.convert(str(src), str(out))  # type: ignore
            except Exception as e:
                print(f"[preview-convert-error] docx2pdf failed: {e}")
            if out.exists():
                return out.read_bytes()
        # Fallback: use libreoffice if docx2pdf failed
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                src = tmpdir_path / "input.docx"
                out_dir = tmpdir_path / "out"
                src.write_bytes(docx_bytes)
                out_dir.mkdir(exist_ok=True)
                subprocess.check_call(
                    ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                out_pdf = out_dir / "input.pdf"
                if out_pdf.exists():
                    return out_pdf.read_bytes()
        except Exception as e:
            print(f"[preview-convert-error] libreoffice failed: {e}")
    except Exception as exc:
        print(f"[preview-convert-error] {exc}")
    return None


def _convert_docx_to_html(docx_bytes: bytes) -> Optional[str]:
    """Convert DOCX bytes to HTML using mammoth. Returns HTML string or None."""
    try:
        try:
            import mammoth  # type: ignore
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "mammoth"])
            import mammoth  # type: ignore
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            src = tmpdir_path / "input.docx"
            src.write_bytes(docx_bytes)
            with open(src, "rb") as docx_file:
                result = mammoth.convert_to_html(docx_file)  # type: ignore
                html = result.value  # type: ignore
                return html
    except Exception as exc:
        print(f"[preview-html-error] {exc}")
    return None


def _sanitize_filename(name: Optional[str], suffix: str) -> str:
    """Ensure we return filesystem-safe filenames with the proper suffix."""
    base = (name or "").strip() or f"assistant_output{suffix}"
    if not base.lower().endswith(suffix):
        base = f"{base}{suffix}"
    safe = re.sub(r"[^\w.\-]+", "_", base)
    if not safe:
        safe = f"assistant_output{suffix}"
    return safe


def _friendly_filled_name(template_name: Optional[str], file_id: Optional[str], fallback_name: Optional[str] = None) -> str:
    """Derive a user-friendly filled filename from the template name (preferred) or a known filename."""
    name_source = template_name or fallback_name
    if name_source:
        base = name_source[:-5] if name_source.lower().endswith(".docx") else name_source
        return f"{base} [FILLED].docx"
    if file_id:
        return f"{file_id} [FILLED].docx"
    return "edited.docx"


def _debug(msg: str) -> None:
    """Optional debug logger controlled via DEBUG_LOG env var."""
    if DEBUG_LOG:
        print(msg)


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


def _persist_container_file(
    container_id: Optional[str],
    container_file_id: Optional[str],
    filename: str,
    vector_store_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Download a container file, upload to Files API, and optionally link to a vector store."""
    if not container_id or not container_file_id:
        return None
    try:
        resp = client.containers.files.content.retrieve(container_id=container_id, file_id=container_file_id)  # type: ignore
        data = resp.read() if hasattr(resp, "read") else bytes(resp)
        if not data:
            return None
        # Cache DOCX locally for download
        safe_name = _sanitize_filename(filename, ".docx")
        local_docx = _LOCAL_FILES_DIR / safe_name
        try:
            local_docx.write_bytes(data)
        except Exception as exc:
            print(f"[persist-container] failed to cache docx locally: {exc}")
        download_url = f"/local_files/{local_docx.name}" if local_docx.exists() else None
        # Cache HTML preview via mammoth
        preview_url = None
        html = _convert_docx_to_html(data) if isinstance(data, (bytes, bytearray)) else None
        if html:
            safe_html = _sanitize_filename(filename, ".html")
            local_html = _PREVIEW_CACHE_DIR / safe_html
            try:
                local_html.write_text(html)
                preview_url = f"/local_previews/{local_html.name}"
            except Exception as exc:
                print(f"[persist-container] failed to cache html preview: {exc}")
        upload = client.files.create(  # type: ignore
            file=(filename, io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            purpose="assistants",
        )
        uploaded_id = _extract_id(upload)  # type: ignore
        rec = _fetch_file_metadata(uploaded_id)
        rec["name"] = filename
        rec["container_id"] = container_id
        rec["container_file_id"] = container_file_id
        rec["preview_url"] = preview_url
        rec["download_url"] = download_url
        if vector_store_id:
            try:
                _link_files_to_vector_store([uploaded_id], vector_store_id)
                rec["vector_store_id"] = vector_store_id
            except Exception as exc:
                print(f"[persist-container] failed to link to vector store: {exc}")
        return rec
    except Exception as exc:
        print(f"[persist-container] failed to persist container file: {exc}")
        return None


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


def _process_server_tool_calls(
    data: Dict[str, Any],
    vector_store_id: Optional[str],
    container_id: Optional[str] = None,
    history_messages: Optional[List[Dict[str, str]]] = None,
    selected_file_id: Optional[str] = None,
    progress_log: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Look for server-side function calls (DOCX/XLSX/selection) and synthesize files.
    Onboarding tip: this is the single place where we execute assistant tool calls server-side,
    enrich with vector store links, and carry selection context forward."""
    generated: List[Dict[str, Any]] = []
    messages: List[str] = []
    selection_results: List[Dict[str, Any]] = []
    last_selection: Dict[str, Any] = {}
    last_selection_prompt: Optional[str] = None
    edit_executed = False
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

    def _append_edit_outputs(
        result: Dict[str, Any],
        filled_name: str,
        target_vs: Optional[str],
        include_message: bool = True,
    ) -> None:
        """Attach edit results (OpenAI file ids or container files) to the response payload."""
        nonlocal container_id, active_vector_store, generated
        if not result:
            return
        _debug(
            f"[append_edit_outputs] filled_name='{filled_name}' "
            f"file_ids={result.get('file_ids')} "
            f"container_file_ids={result.get('container_file_ids')} "
            f"local_files_count={len(result.get('local_files') or [])} "
            f"vector_store_id={result.get('vector_store_id')} "
            f"container_id={result.get('container_id')}"
        )
        if include_message and result.get("message"):
            messages.append(result["message"])
        if not container_id and result.get("container_id"):
            container_id = result["container_id"]
        result_vs = result.get("vector_store_id") or target_vs
        if result_vs and not active_vector_store:
            active_vector_store = result_vs
        output_file_ids = result.get("file_ids") or []
        container_file_ids = result.get("container_file_ids") or []
        # Prefer container file ids when present so downloads use the container endpoint rather than the Files API.
        if container_file_ids:
            cid_result = result.get("container_id") or container_id
            for cfid in container_file_ids:
                rec = {
                    "id": cfid,
                    "openai_file_id": None,
                    "container_id": cid_result,
                    "container_file_id": cfid,
                    "name": filled_name,
                    "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "source": "generated",
                    "vector_store_id": result_vs or target_vs,
                }
                if cid_result and cfid:
                    # Use content endpoint so DOCX renders via mammoth; preview.pdf may 404 if container PDF conversion fails.
                    rec["preview_url"] = f"/v1/containers/{cid_result}/files/{cfid}/content?name={filled_name}"
                    rec["download_url"] = rec["preview_url"]
                generated.append(rec)
        if output_file_ids:
            # If we already have container files, just link OpenAI file ids to the vector store without adding a second sidebar entry.
            if container_file_ids:
                effective_vs = result_vs or target_vs
                if effective_vs:
                    try:
                        _link_files_to_vector_store(output_file_ids, effective_vs)
                    except Exception as exc:
                        print(f"[append_edit_outputs] failed to link file ids to VS: {exc}")
                # Skip adding OpenAI file entries to avoid duplicate sidebar items.
                output_file_ids = []
            effective_vs = result_vs or target_vs
            if effective_vs:
                linked = _link_files_to_vector_store(output_file_ids, effective_vs)
                # annotate with container ids if we have them
                for rec in linked:
                    if filled_name:
                        rec["name"] = filled_name
                    if rec.get("mime") is None:
                        rec["mime"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    cid = container_file_map.get(rec.get("openai_file_id"))
                    # Only attach container metadata when we actually know a container file id mapping.
                    if cid:
                        rec["container_id"] = container_id or cid
                        rec["container_file_id"] = rec.get("openai_file_id")
                generated.extend(linked)
            else:
                for fid in output_file_ids:
                    rec = _fetch_file_metadata(fid)
                    if filled_name:
                        rec["name"] = filled_name
                    if rec.get("mime") is None:
                        rec["mime"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    cid = container_file_map.get(fid)
                    if cid:
                        rec["container_id"] = container_id or cid
                        rec["container_file_id"] = container_file_map.get(fid)
                    generated.append(rec)
        # Only include container-only entries if we did NOT get OpenAI file ids; avoids duplicate sidebar entries.
        elif container_file_ids:
            # Create container-only entries so the UI can download even without OpenAI file ids.
            cid_result = result.get("container_id") or container_id
            for cfid in container_file_ids:
                # Attempt to persist to Files API so it survives container expiry.
                persisted = _persist_container_file(cid_result, cfid, filled_name, result_vs or target_vs)
                if persisted:
                    persisted["container_id"] = cid_result
                    persisted["container_file_id"] = cfid
                    generated.append(persisted)
                    continue
                preview_url = None
                if cid_result and cfid:
                    preview_url = f"/v1/containers/{cid_result}/files/{cfid}/preview.pdf?name={filled_name}"
                rec = {
                    "id": cfid,
                    "openai_file_id": None,
                    "container_id": cid_result,
                    "container_file_id": cfid,
                    "container_file_ids": container_file_ids,
                    "name": filled_name,
                    "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "preview_url": preview_url,
                    "source": "generated",
                    "vector_store_id": result_vs or target_vs,
                }
                generated.append(rec)
        # Attach locally cached files from base64 outputs, if present
        local_files = result.get("local_files") or []
        for lf in local_files:
            generated.append(lf)
        _debug(
            f"[append_edit_outputs] generated_entries_now={len(generated)} "
            f"container_file_map_keys={list(container_file_map.keys())}"
        )

    # Helpers for per-tool handling to keep the main loop readable.
    def _handle_select(args: Dict[str, Any]) -> Optional[SelectionResult]:
        nonlocal active_vector_store, selected_file_id, last_selection, last_selection_prompt
        _log_progress(progress_log, "tool:select_docx")
        prompt = (args.get("prompt") or "").strip()
        target_vs = args.get("vector_store_id") or active_vector_store
        if not prompt:
            messages.append("select_docx: missing prompt; skipping.")
            return None
        result = select_docx_template(prompt, vector_store_id=target_vs)
        selection_results.append(result)  # type: ignore[arg-type]
        if result.get("vector_store_id") and not active_vector_store:
            active_vector_store = result["vector_store_id"]
        last_selection = result or {}
        if result.get("file_id"):
            selected_file_id = result.get("file_id")
        last_selection_prompt = prompt
        return result  # type: ignore[return-value]

    def _handle_edit(args: Dict[str, Any]) -> None:
        nonlocal edit_executed, selected_file_id, active_vector_store
        _log_progress(progress_log, "tool:edit_docx")
        file_id = (
            (args.get("file_id") or "").strip()
            or (last_selection.get("file_id") or "").strip()
            or (selected_file_id or "").strip()
        )
        edit_instructions = (args.get("edit_instructions") or args.get("instructions") or "").strip()
        if not file_id:
            messages.append("edit_docx: missing file_id; skipping.")
            return
        if not edit_instructions:
            messages.append("edit_docx: missing edit_instructions; skipping.")
            return
        selection_text_local = (args.get("selection_text") or "").strip() or last_selection.get("selection_text") or last_selection.get("message")
        target_vs = args.get("vector_store_id") or active_vector_store
        template_name = (
            last_selection.get("template_name")
            or args.get("template_name")
            or None
        )
        if not template_name and selection_text_local:
            # Try to extract template name from selection_text lines like "Template name: XYZ.docx"
            for line in (selection_text_local or "").splitlines():
                if "template name" in line.lower() and ":" in line:
                    cand = line.split(":", 1)[1].strip()
                    if cand:
                        template_name = cand
                        break
        meta_name = None
        if not template_name:
            try:
                meta = client.files.retrieve(file_id)
                meta_dict = _ensure_dict(meta)
                meta_name = meta_dict.get("filename") or meta_dict.get("display_name")
            except Exception:
                meta_name = None
        filled_name = _friendly_filled_name(template_name, file_id, meta_name)
        # Lean mode for very large edit instructions: drop optional conversation history to reduce payload size.
        convo_for_tool = history_messages
        if len(edit_instructions or "") > 12000:
            convo_for_tool = None
        _debug(
            f"[edit_docx:prepared] file_id={file_id} template_name={template_name} "
            f"filled_name={filled_name} instructions_len={len(edit_instructions or '')}"
        )
        if DEBUG_LOG and edit_instructions:
            preview_len = 800
            _debug(f"[edit_docx:instructions_preview]\n{edit_instructions[:preview_len]}")
        result = edit_docx_template(
            file_id=file_id,
            edit_instructions=edit_instructions,
            selection_text=selection_text_local,
            conversation_history=convo_for_tool,
            desired_filename=filled_name,
        )
        _debug(
            "[edit_docx] result keys="
            f"{list(result.keys()) if isinstance(result, dict) else result}; "
            f"file_ids={result.get('file_ids') if isinstance(result, dict) else None}; "
            f"container_file_ids={result.get('container_file_ids') if isinstance(result, dict) else None}; "
            f"local_files={len(result.get('local_files') or []) if isinstance(result, dict) else 0}"
        )
        edit_executed = True
        _append_edit_outputs(result, filled_name, target_vs)
        if not ((result.get("file_ids") or result.get("container_file_ids") or result.get("local_files"))):
            messages.append("edit_docx returned no files; please provide required values or retry the edit.")

    for entry in outputs:
        entry_type = entry.get("type")
        if entry_type not in ("function_call", "output_tool_call"):
            continue
        name = entry.get("name") or entry.get("function", {}).get("name")
        if not name:
            continue
        args = _coerce_tool_args(entry)
        if name == "generate_xlsx":
            _log_progress(progress_log, "tool:generate_xlsx")
            generated.extend(_handle_generate_xlsx_tool(args, active_vector_store))
        elif name == "select_docx":
            _handle_select(args)
        elif name == "edit_docx":
            _handle_edit(args)
    # If any generated entries match container_file_map, enrich them
    for rec in generated:
        fid = rec.get("openai_file_id") or rec.get("id")
        if fid and fid in container_file_map:
            rec["container_id"] = container_id or container_file_map[fid]
            rec["container_file_id"] = fid
    _debug(
        f"[process_server_tool_calls] total_generated={len(generated)} "
        f"messages={len(messages)} selection_results={len(selection_results)}"
    )
    return {
        "generated_files": generated,
        "messages": messages,
        "vector_store_id": active_vector_store,
        "selection_results": selection_results,
    }


def _normalize_history_for_model(history: Optional[List[Dict[str, Any]]], recent_limit: int = 6, summary_limit: int = 4000) -> List[Dict[str, str]]:
    """
    Provide a rolling context: a compact system summary of older turns plus the most recent turns verbatim.
    Keeps precision of recent messages while avoiding unbounded payload growth.
    """
    if not isinstance(history, list) or not history:
        return []
    recent: List[Dict[str, str]] = []
    older: List[Dict[str, str]] = []
    for entry in history:
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
        older.append({"role": role, "content": text})
    # Split older/recent
    if len(older) > recent_limit:
        recent = older[-recent_limit:]
        older = older[:-recent_limit]
    else:
        recent = older
        older = []
    summary_lines: List[str] = []
    total_len = 0
    for item in older:
        snippet = item["content"][:800]
        line = f"{item['role']}: {snippet}"
        if total_len + len(line) > summary_limit:
            break
        summary_lines.append(line)
        total_len += len(line)
    summary_entry: List[Dict[str, str]] = []
    if summary_lines:
        summary_entry.append(
            {
                "role": "system",
                "content": "Earlier context:\n" + "\n".join(summary_lines),
            }
        )
    # Clamp recent message length but keep full turn structure.
    cleaned_recent: List[Dict[str, str]] = []
    for item in recent:
        cleaned_recent.append({"role": item["role"], "content": item["content"][:2000]})
    return summary_entry + cleaned_recent


def _sanitize_assistant_text(text: Optional[str]) -> Optional[str]:
    """Remove sandbox paths and working-dir dumps from assistant text; keep user-facing summary clean."""
    if not isinstance(text, str):
        return text
    sanitized = text.replace("sandbox:/mnt/data/", "")
    lines = sanitized.splitlines()
    out_lines: List[str] = []
    skip_block = False
    for line in lines:
        lower = line.lower()
        if "working directory contents" in lower:
            skip_block = True
            continue
        if skip_block:
            if not line.strip():
                skip_block = False
            continue
        if "download the edited lesson plan" in lower:
            # Drop this boilerplate; Files list already shows artifacts.
            continue
        if "sandbox:/mnt/data" in line:
            # Drop direct sandbox file links; files list handles downloads.
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip()


def _strip_markdown_links(text: Optional[str]) -> Optional[str]:
    """Remove markdown links to avoid dead-end downloads; keep link text only."""
    if not isinstance(text, str):
        return text
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)


def _strip_pdf_claims(text: Optional[str], files: List[Dict[str, Any]]) -> Optional[str]:
    """If no PDF was generated, remove misleading PDF mentions from assistant text."""
    if not isinstance(text, str):
        return text
    pdf_present = any(
        isinstance(f, dict)
        and (
            str(f.get("mime", "")).lower().endswith("pdf")
            or str(f.get("name", "")).lower().endswith(".pdf")
        )
        for f in files
    )
    if pdf_present or "pdf" not in text.lower():
        return text
    lines = []
    for line in text.splitlines():
        lower = line.lower()
        if "pdf" in lower and ("generated" in lower or "see link" in lower):
            continue
        lines.append(line)
    return "\n".join(lines).strip()



def create_app() -> Flask:
    """Application factory so tests and gunicorn can import the same Flask instance."""
    return app

@app.route("/")
def index():
    return render_template("chatbot.html")


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
    def _coerce_text(val: Any) -> str:
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            inner = val.get("value") or val.get("text")
            if isinstance(inner, str):
                return inner
        if isinstance(val, list):
            parts = [_coerce_text(v) for v in val]
            return "\n".join([p for p in parts if p])
        return ""

    raw_text = getattr(resp, "output_text", None)
    coerced_first = _coerce_text(raw_text)
    if coerced_first.strip():
        return coerced_first.strip()
    text = raw_text
    if isinstance(text, list):
        text = text[0] if text else ""
    for output in getattr(resp, "output", []) or []:
        if isinstance(output, dict):
            contents = output.get("content")
            if isinstance(contents, list):
                for c in contents:
                    val = c.get("text") if isinstance(c, dict) else None
                    coerced = _coerce_text(val)
                    if coerced.strip():
                        return coerced.strip()
            txt = output.get("text")
            coerced_txt = _coerce_text(txt)
            if coerced_txt.strip():
                return coerced_txt.strip()
    choices = getattr(resp, "choices", None)
    if choices:
        msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        val = msg.get("content")
        coerced = _coerce_text(val)
        if coerced.strip():
            return coerced.strip()
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
    selected_file_id = data.get("selected_file_id") or None
    selection_text = data.get("selection_text") or None
    conversation_id = data.get("conversation_id") or None
    conversation_mode = "conversations_api" if CONVERSATIONS_ENABLED else "legacy_history"
    conversation_error: Optional[str] = None
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

        for spec in DOC_TOOL_SPECS:
            already = any(
                (t.get("name") == spec.get("name")) or (t.get("type") == spec.get("type"))
                for t in tools
            )
            if not already:
                tools.append(copy.deepcopy(spec))

        def _build_convo_input(include_history: bool) -> List[Dict[str, str]]:
            convo_input: List[Dict[str, str]] = [{"role": "system", "content": GENERAL_CHAT_SYSTEM}]
            if selected_file_id:
                convo_input.append(
                    {
                        "role": "system",
                        "content": (
                            f"A DOCX template is already selected: file_id={selected_file_id}. "
                            "Normally, do NOT call select_docx again; continue with this template. "
                            "However, if the user asks for a different template or the current one is incorrect, you may re-run select_docx to choose a better match. "
                            "Ask concise follow-up questions, grounded in the template anchors, until you have the values needed, then call edit_docx exactly once with the full instructions. "
                            "If the user is asking why a prior edit behaved a certain way or wants an explanation, respond in text only—do NOT call edit_docx unless they explicitly request a new or corrected edit after you answer. "
                            f"Template anchors/context: {selection_text or 'not provided; rely on file_info from selection and user replies.'}"
                        ),
                    }
                )
            if include_history:
                convo_input.extend(history_messages)
            convo_input.append({"role": "user", "content": msg})
            return convo_input

        convo_input: List[Dict[str, str]] = _build_convo_input(False)
        resp = None
        last_exc: Optional[Exception] = None
        progress_log: List[Dict[str, Any]] = []

        if conversation_mode == "conversations_api" and not conversation_id:
            try:
                convo = client.conversations.create()  # type: ignore
                conversation_id = _extract_id(convo)  # type: ignore[arg-type]
            except Exception as exc:
                conversation_error = str(exc)
                conversation_mode = "legacy_history"
                _log_progress(progress_log, "conversation_legacy_fallback", error=conversation_error)

        if conversation_mode == "conversations_api" and conversation_id:
            _log_progress(progress_log, "conversation_api", conversation_id=conversation_id)
            try:
                resp = client.responses.create(
                    model=CHAT_MODEL,
                    conversation=conversation_id,
                    input=convo_input,  # type: ignore
                    tools=tools or None,  # type: ignore
                    timeout=RESPONSE_TIMEOUT,
                )
            except Exception as exc:
                conversation_error = str(exc)
                conversation_mode = "legacy_history"
                last_exc = exc
                _log_progress(progress_log, "conversation_legacy_fallback", error=conversation_error)

        if resp is None and conversation_mode == "conversations_api":
            conversation_mode = "legacy_history"
            _log_progress(progress_log, "conversation_legacy_fallback", error=conversation_error)

        if resp is None:
            convo_input = _build_convo_input(True)
            for attempt in range(3):
                try:
                    resp = client.responses.create(
                        model=CHAT_MODEL,
                        input=convo_input,  # type: ignore
                        tools=tools or None,  # type: ignore
                        timeout=RESPONSE_TIMEOUT,
                    )
                    break
                except openai.APITimeoutError as exc:
                    last_exc = exc
                    print(f"[openai-timeout] attempt {attempt+1} timed out")
                except openai.APIConnectionError as exc:
                    last_exc = exc
                    print(f"[openai-connection-error] attempt {attempt+1}: {exc}")
                except Exception as exc:
                    last_exc = exc
                    print(f"[openai-error] attempt {attempt+1}: {exc}")
                if resp is None and attempt < 2:
                    delay = 2 * (attempt + 1) + (0.5 * (attempt + 1))
                    time.sleep(delay)
        if resp is None:
            msg = "Connection to OpenAI failed. Please try again in a moment."
            detail = str(last_exc) if last_exc else None
            return jsonify({"error": msg, "detail": detail}), 502
        serialized = _ensure_dict(resp)
        text = _strip_markdown_links(_sanitize_assistant_text(_extract_text(resp) or ""))
        file_ids = getattr(resp, "output_file_ids", None) or []
        generated_files: List[Dict[str, Any]] = []
        selected_file_id = data.get("selected_file_id") or None
        selection_text = data.get("selection_text") or None
        tool_results = _process_server_tool_calls(
            serialized,
            vector_store_id,
            container_id=container_id,
            history_messages=history_messages,
            selected_file_id=selected_file_id,
            progress_log=progress_log,
        )
        if tool_results.get("vector_store_id"):
            vector_store_id = tool_results["vector_store_id"]
        generated_files.extend(tool_results.get("generated_files", []))
        generated_files.extend(_link_files_to_vector_store(file_ids, vector_store_id))
        selection_results = tool_results.get("selection_results") or []
        if selection_results:
            last_sel = selection_results[-1] if isinstance(selection_results, list) else None
            if isinstance(last_sel, dict):
                # Thread the last selection forward so the next turn has context without re-calling select_docx.
                # Frontend should echo these back on the next /chat to keep the main model aware of the chosen template.
                if not selected_file_id:
                    selected_file_id = last_sel.get("file_id") or selected_file_id
                if not selection_text:
                    selection_text = (
                        last_sel.get("selection_text")
                        or last_sel.get("message")
                        or last_sel.get("file_info")
                    )
        deduped: List[Dict[str, Any]] = []
        seen_keys = set()
        for rec in generated_files:
            if not isinstance(rec, dict):
                continue
            key = (
                rec.get("openai_file_id") or rec.get("id") or rec.get("download_url"),
                rec.get("container_file_id"),
                rec.get("download_url"),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(rec)
        generated_files = deduped
        _debug(f"[chat] generated_files before response={generated_files}")
        tool_messages = tool_results.get("messages") or []
        if tool_messages:
            sanitized_msgs: List[str] = []
            for msg_text in tool_messages:
                if not isinstance(msg_text, str):
                    continue
                cleaned = _strip_markdown_links(_sanitize_assistant_text(msg_text) or "")
                cleaned = _strip_pdf_claims(cleaned, generated_files) or cleaned
                if cleaned.strip():
                    sanitized_msgs.append(cleaned.strip())
            if sanitized_msgs:
                text = f"{text}\\n\\n" + "\\n\\n".join(sanitized_msgs) if text else "\\n\\n".join(sanitized_msgs)
        # If no assistant text and we have a fresh selection, surface a structured prompt to collect details
        # based on the selected template's anchors (selection_text/file_info).
        if not text and selection_results:
            last_sel = selection_results[-1] if isinstance(selection_results, list) else None
            if isinstance(last_sel, dict):
                anchors = last_sel.get("selection_text") or last_sel.get("file_info") or ""
                template_name = last_sel.get("template_name") or last_sel.get("file_id") or "this template"
                # Trim anchors to keep the prompt concise
                anchors_snippet = anchors.strip()[:1200] if isinstance(anchors, str) else ""
                text = (
                    f"I have the template '{template_name}'. Please provide the values for its fillable fields/anchors so I can fill it in-place:\n\n"
                    f"{anchors_snippet}\n\n"
                    "Reply with the requested values and I’ll run edit_docx once to apply them. If you prefer a different template, tell me and I can re-run select_docx."
                )
        text = _strip_pdf_claims(text, generated_files)
        return jsonify({
            "text": text,
            "mode": "direct",
            "file_ids": file_ids,
            "conversation_id": conversation_id,
            "conversation_mode": conversation_mode,
            "conversation_error": conversation_error,
            "generated_files": generated_files,
            "selection_results": selection_results,
            "selected_file_id": selected_file_id,
            "selection_text": selection_text,
            "output": serialized.get("output"),
            "output_text": serialized.get("output_text"),
            "choices": serialized.get("choices"),
            "response_payload": serialized,
            "progress_log": progress_log,
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


@app.route("/v1/conversations", methods=["POST"])
def create_conversation_api():
    """Provision a new Conversations API thread so the frontend can persist chat state server-side."""
    payload = request.get_json(force=True) or {}
    name = payload.get("name") or f"Session Conversation - {uuid.uuid4()}"
    try:
        convo = client.conversations.create()  # type: ignore
        return jsonify(_serialize(convo))
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
            purpose=purpose,  # type: ignore
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
        filename = None
        try:
            meta = client.files.retrieve(file_id)
            meta_dict = _ensure_dict(meta)
            filename = meta_dict.get("filename") or meta_dict.get("display_name")
        except Exception:
            filename = None
        download_name = filename or f"{file_id}.bin"
        return send_file(
            io.BytesIO(data),  # type: ignore
            download_name=download_name,
            mimetype="application/octet-stream",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/v1/containers/<container_id>/files/<file_id>/content", methods=["GET"])
def get_container_file_content(container_id, file_id):
    try:
        resp = client.containers.files.content.retrieve(container_id=container_id, file_id=file_id)  # type: ignore
        data = resp.read() if hasattr(resp, "read") else bytes(resp)
        if isinstance(data, str):
            data = data.encode("utf-8")
        download_name = request.args.get("name") or f"{file_id}.bin"
        return send_file(
            io.BytesIO(data),  # type: ignore
            download_name=download_name,
            mimetype="application/octet-stream",
        )
    except Exception as exc:
        try:
            print(f"[container-download-error] {exc}")
        except Exception:
            pass
        return jsonify({"error": str(exc)}), 500


@app.route("/v1/containers/<container_id>/files/<file_id>/preview.pdf", methods=["GET"])
def get_container_file_preview(container_id, file_id):
    try:
        resp = client.containers.files.content.retrieve(container_id=container_id, file_id=file_id)  # type: ignore
        docx_bytes = resp.read() if hasattr(resp, "read") else bytes(resp)
        html = _convert_docx_to_html(docx_bytes) if isinstance(docx_bytes, (bytes, bytearray)) else None
        if html:
            return html, 200, {"Content-Type": "text/html"}
        cached = _PREVIEW_CACHE_DIR / f"{file_id}.pdf"
        if cached.exists():
            return send_file(
                cached.open("rb"),
                download_name="preview.pdf",
                mimetype="application/pdf",
            )
        pdf_bytes = _convert_docx_to_pdf_bytes(docx_bytes) if isinstance(docx_bytes, (bytes, bytearray)) else None
        if pdf_bytes:
            cached.write_bytes(pdf_bytes)
            return send_file(
                io.BytesIO(pdf_bytes),  # type: ignore
                download_name="preview.pdf",
                mimetype="application/pdf",
            )
        download_url = f"/v1/containers/{container_id}/files/{file_id}/content?name={request.args.get('name') or 'download.docx'}"
        html = f"""<html><body style="font-family:sans-serif;padding:16px;">
            <p>Preview conversion failed. You can download the DOCX instead:</p>
            <p><a href="{download_url}" target="_blank" rel="noopener">Download DOCX</a></p>
            </body></html>"""
        return html, 200, {"Content-Type": "text/html"}
    except Exception as exc:
        print(f"[container-preview-error] {exc}")
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


@app.route("/local_previews/<path:fname>", methods=["GET"])
def get_local_preview(fname):
    local_path = _PREVIEW_CACHE_DIR / fname
    if not local_path.exists():
        return jsonify({"error": "file not found"}), 404
    return send_file(
        local_path.open("rb"),
        download_name=fname,
        mimetype="text/html",
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
        container = client.containers.create(**kwargs)  # type: ignore
        return jsonify(_serialize(container))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/v1/containers/<container_id>", methods=["DELETE"])
def delete_container_runtime(container_id):
    try:
        res = client.containers.delete(container_id)  # type: ignore
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
            uploaded = client.containers.files.create(  # type: ignore
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
        uploaded = client.containers.files.create(  # type: ignore
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
        client.containers.files.delete(file_id=file_id, container_id=container_id)  # type: ignore
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


if __name__ == "__main__":
    app = create_app()
    app.run(port=5001, debug=True)
