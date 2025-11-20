"""Flask application that orchestrates consultants, resources, and chat UI helpers."""

import copy
import io
import os
import uuid
import json
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, List, Dict, Any
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
from docx import Document
from openpyxl import Workbook

from app.consultants import (
    run_consultant_response,
    DEFAULT_CONSULTANT_KEY,
    CONSULTANTS,
    list_consultants,
    get_consultant_overview,
    _extract_id,
)
from app.consultant_router import route_consultants, RouterDecision, describe_decision
from app.openai_client import client


CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-5")
GENERAL_CHAT_SYSTEM = """You are a helpful assistant. Keep answers concise unless the user asks for more detail.
When the user requests a document, report, or formatted output, call generate_pdf(markdown_text=your response in raw Markdown) or generate_docx(markdown_text=..., filename=...) depending on the requested format.
For spreadsheets or tabular deliverables, call generate_xlsx(sheets=[{name:..., rows:[[...], ...]}]).
When appropriate, use tools like web_search_preview or code_interpreter to enhance your answers.
You can also call specialized consultants via the call_<consultant> tool names when their expertise fits better than answering yourself
Respond using Markdown syntax for code and always wrap code in fenced blocks (```), leaving a blank line before and after each block.
If you cannot access the data, just say so and do not provide terminal commands.
Otherwise, reply normally in raw Markdown."""
PARALLEL_SUMMARY_SYSTEM = """You orchestrate multiple consultant agents. Summarize their findings into a cohesive answer for the acquisition team.
- Tie recommendations back to the user's question.
- Highlight conflicts or gaps between consultants.
- Mention which consultant provided critical insights.
Respond concisely but cover the major points."""
CONSULTANT_TOOL_PREFIX = "call_"
DIRECT_TOOL_NAMES = [
    "file_search",
    "generate_pdf",
    "generate_docx",
    "generate_xlsx",
    "web_search_preview",
    "code_interpreter",
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
    try:
        # Grab file info from OpenAI so filenames + sizes stay accurate when the UI renders them.
        meta = client.files.retrieve(file_id)
        data = _ensure_dict(meta)
    except Exception:
        data = {}
    return {
        "id": file_id,
        "openai_file_id": file_id,
        "name": data.get("filename") or data.get("display_name") or file_id,
        "size": data.get("bytes"),
        "vector_store_id": None,
        "created_at": data.get("created_at"),
        "source": source,
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


def _process_server_tool_calls(data: Dict[str, Any], vector_store_id: Optional[str]) -> List[Dict[str, Any]]:
    """Look for server-side function calls (DOCX/XLSX) and synthesize files."""
    generated: List[Dict[str, Any]] = []
    outputs = data.get("output") or []
    for entry in outputs:
        entry_type = entry.get("type")
        if entry_type not in ("function_call", "output_tool_call"):
            continue
        name = entry.get("name") or entry.get("function", {}).get("name")
        if not name:
            continue
        args = _coerce_tool_args(entry)
        if name == "generate_docx":
            generated.extend(_handle_generate_docx_tool(args, vector_store_id))
        elif name == "generate_xlsx":
            generated.extend(_handle_generate_xlsx_tool(args, vector_store_id))
    return generated


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


def _build_consultant_tool_specs() -> List[Dict[str, Any]]:
    """Expose each consultant as a callable tool so the general model can delegate work."""
    specs: List[Dict[str, Any]] = []
    for key, meta in CONSULTANTS.items():
        display = meta.get("display_name", key)
        summary = meta.get("summary") or f"Delegate acquisition tasks handled by {display}."
        specs.append(
            {
                "type": "function",
                "name": f"{CONSULTANT_TOOL_PREFIX}{key}",
                "description": summary,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "User request to provide to the consultant.",
                        },
                        "context": {
                            "type": "string",
                            "description": "Additional background or notes for the consultant.",
                        },
                        "vector_store_id": {
                            "type": "string",
                            "description": "Override vector store id for this call (optional).",
                        },
                        "container_id": {
                            "type": "string",
                            "description": "Override code interpreter container id (optional).",
                        },
                    },
                },
            }
        )
    return specs


CONSULTANT_TOOL_SPECS = _build_consultant_tool_specs()  # Cache tool schemas once so every request reuses the same payload.


def _extract_consultant_tool_calls(outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find consultant tool invocations embedded in a Responses payload."""
    calls: List[Dict[str, Any]] = []
    for entry in outputs:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type not in ("function_call", "output_tool_call"):
            continue
        name = entry.get("name") or entry.get("function", {}).get("name")
        if not name or not name.startswith(CONSULTANT_TOOL_PREFIX):
            continue
        consultant_key = name[len(CONSULTANT_TOOL_PREFIX) :]
        if consultant_key not in CONSULTANTS:
            continue
        calls.append(
            {
                "tool_name": name,
                "consultant_key": consultant_key,
                "arguments": _coerce_tool_args(entry),
            }
        )
    return calls


def create_app() -> Flask:
    """Application factory so tests and gunicorn can import the same Flask instance."""
    app = Flask(__name__)
    CORS(app)  # enable CORS for all routes
    global _TOOL_LOGGED
    # These tool names are exposed to Responses so consultant personas can be invoked via tool calls.
    consultant_tools = [f"{CONSULTANT_TOOL_PREFIX}{key}" for key in CONSULTANTS]
    if not _TOOL_LOGGED:
        print(f"[consultant-tools] Registered tool calls: {consultant_tools}")
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
        ]
        if any(trigger in lowered for trigger in triggers):
            return True
        # simple pairwise terms
        return ("pdf" in lowered and ("generate" in lowered or "create" in lowered or "export" in lowered)) or (
            "docx" in lowered and ("generate" in lowered or "create" in lowered or "export" in lowered)
        ) or ("xlsx" in lowered and ("generate" in lowered or "create" in lowered or "export" in lowered))


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


    def _invoke_consultant_run(
        message: str,
        project: str,
        vector_store_id: Optional[str],
        container_id: Optional[str],
        consultant_key: str,
    ) -> Dict[str, Any]:
        """Execute a single consultant and normalize its response for downstream consumers."""
        started = time.time()
        result = run_consultant_response(
            message,
            vector_store_id=vector_store_id,
            container_id=container_id,
            consultant_key=consultant_key,
        )
        text = (result.get("text") or "").strip() or "The consultant returned no notes."
        file_ids = result.get("file_ids") or []
        response_payload = result.get("response_payload") or {}
        generated_files = _process_server_tool_calls(response_payload, vector_store_id)
        generated_files.extend(_link_files_to_vector_store(file_ids, vector_store_id))
        meta = CONSULTANTS.get(consultant_key, {})
        return {
            "question": message,
            "project": project,
            "consultant": result.get("consultant", consultant_key),
            "display_name": result.get("display_name") or meta.get("display_name", consultant_key),
            "text": text,
            "model": result.get("model"),
            "file_ids": file_ids,
            "generated_files": generated_files,
            "resources": meta.get("local_files", []),
            "response_payload": response_payload,
            "duration_ms": int((time.time() - started) * 1000),
        }


    def _consultant_tool_call(
        message: str,
        project: str,
        vector_store_id: Optional[str],
        container_id: Optional[str],
        consultant_key: str,
        router_decision: Optional[RouterDecision] = None,
        progress_log: Optional[List[Dict[str, Any]]] = None,
    ):
        """Legacy call path that wraps `_invoke_consultant_run` in the tool-call shim."""
        tool_name = f"{CONSULTANT_TOOL_PREFIX}{consultant_key}"
        run_payload = _invoke_consultant_run(
            message,
            project,
            vector_store_id,
            container_id,
            consultant_key,
        )
        _log_progress(
            progress_log,
            "consultant",
            consultant=run_payload["consultant"],
            display_name=run_payload["display_name"],
            duration_ms=run_payload.get("duration_ms"),
        )
        print(f"[consultant-tool] Executed {tool_name} ({run_payload['display_name']}) with model {run_payload.get('model')}")
        tool_args = {
            "question": message,
            "project": project,
            "vector_store_id": vector_store_id,
            "container_id": container_id,
            "consultant_key": consultant_key,
        }
        call_stub = {
            "type": "function_call",
            "name": tool_name,
            "arguments": tool_args,
        }
        response_stub = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": run_payload["text"]}],
        }
        return {
            "text": run_payload["text"],
            "mode": "consultant",
            "tool_name": tool_name,
            "consultant": run_payload["consultant"],
            "consultant_display": run_payload["display_name"],
            "model": run_payload.get("model"),
            "file_ids": run_payload.get("file_ids"),
            "generated_files": run_payload.get("generated_files"),
            "resources": run_payload.get("resources"),
            "router_decision": router_decision.to_dict() if router_decision else None,
            "progress_log": progress_log,
            "output": [call_stub, response_stub],
        }


    def _summarize_consultant_results(
        message: str,
        consultant_runs: List[Dict[str, Any]],
        summary_prompt: Optional[str],
        vector_store_id: Optional[str],
    ) -> Dict[str, Any]:
        """Ask the general chat model to merge multiple consultant notes into one answer."""
        summary_system = summary_prompt or PARALLEL_SUMMARY_SYSTEM
        lines = [f"User question:\n{message.strip()}"]
        for run in consultant_runs:
            lines.append(
                f"\nConsultant: {run['display_name']} ({run['consultant']})\nNotes:\n{run.get('text') or 'No notes provided.'}"
            )
        compiled = "\n".join(lines)
        started = time.time()
        resp = client.responses.create(
            model=CHAT_MODEL,
            input=[
                {"role": "system", "content": summary_system},
                {"role": "user", "content": compiled},
            ],
        )
        duration_ms = int((time.time() - started) * 1000)
        serialized = _ensure_dict(resp)
        text = _extract_text(resp) or "Summary was not generated."
        file_ids = getattr(resp, "output_file_ids", None) or []
        generated_files = _process_server_tool_calls(serialized, vector_store_id)
        generated_files.extend(_link_files_to_vector_store(file_ids, vector_store_id))
        return {
            "text": text,
            "file_ids": file_ids,
            "generated_files": generated_files,
            "response": serialized,
            "duration_ms": duration_ms,
        }


    def _run_parallel_consultants(
        message: str,
        project: str,
        vector_store_id: Optional[str],
        container_id: Optional[str],
        consultant_keys: List[str],
        router_decision: Optional[RouterDecision],
        progress_log: Optional[List[Dict[str, Any]]],
    ):
        """Execute multiple consultants concurrently and merge their answers."""
        if not consultant_keys:
            raise ValueError("At least one consultant key is required for parallel execution.")

        runs: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        aggregated_files: List[Dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=min(len(consultant_keys), 4)) as executor:
            # Fan out each consultant call and keep track of which future maps to which key.
            future_map = {
                executor.submit(
                    _invoke_consultant_run,
                    message,
                    project,
                    vector_store_id,
                    container_id,
                    key,
                ): key
                for key in consultant_keys
            }
            for future in as_completed(future_map):
                key = future_map[future]
                try:
                    run_payload = future.result()
                    runs.append(run_payload)
                    aggregated_files.extend(run_payload.get("generated_files") or [])
                    _log_progress(
                        progress_log,
                        "consultant",
                        consultant=run_payload["consultant"],
                        display_name=run_payload["display_name"],
                        duration_ms=run_payload.get("duration_ms"),
                    )
                    print(f"[consultant-tool] Parallel run completed for {run_payload['display_name']} ({run_payload['consultant']})")
                except Exception as exc:  # pragma: no cover - diagnostic
                    error_text = str(exc)
                    failures.append({"consultant": key, "error": error_text})
                    _log_progress(
                        progress_log,
                        "consultant_error",
                        consultant=key,
                        error=error_text,
                    )

        if not runs:
            raise RuntimeError("All consultant calls failed.")

        summary = _summarize_consultant_results(
            message,
            runs,
            router_decision.summary_prompt if router_decision else None,
            vector_store_id,
        )
        aggregated_files.extend(summary.get("generated_files") or [])
        _log_progress(
            progress_log,
            "summary",
            duration_ms=summary.get("duration_ms"),
        )
        consultant_notes = {run["consultant"]: run.get("text", "") for run in runs}
        return {
            "mode": "parallel",
            "text": summary.get("text"),
            "summary": summary.get("text"),
            "consultant_notes": consultant_notes,
            "consultants": [
                {
                    "consultant": run["consultant"],
                    "display_name": run["display_name"],
                    "text": run.get("text"),
                    "model": run.get("model"),
                    "file_ids": run.get("file_ids"),
                    "generated_files": run.get("generated_files"),
                    "resources": run.get("resources"),
                }
                for run in runs
            ],
            "file_ids": summary.get("file_ids"),
            "generated_files": aggregated_files,
            "router_decision": router_decision.to_dict() if router_decision else None,
            "progress_log": progress_log,
            "failures": failures,
            "summary_response": summary.get("response"),
        }

    @app.route("/chat", methods=["POST"])
    def chat():
        data = request.get_json(force=True)
        msg = data.get("message", "").strip()
        project = data.get("project", "demo-project")
        vector_store_id = data.get("vector_store_id")
        container_id = data.get("container_id")
        requested_consultant = data.get("consultant_key")
        incoming_tools = data.get("tools")
        router_payload = data.get("router_decision")
        history_payload = data.get("history")
        # The general assistant needs the recent transcript so pronouns like "this" resolve before tool calls fire.
        history_messages = _normalize_history_for_model(history_payload)
        if not msg:
            return jsonify({"error": "Missing message"}), 400

        # Track router + execution steps so the client can show progress indicators.
        router_decision = None
        progress_log: List[Dict[str, Any]] = []
        consultant_key = None
        forced_direct = False
        if requested_consultant:
            if requested_consultant not in CONSULTANTS:
                return jsonify({"error": f"Unknown consultant '{requested_consultant}'"}), 400
            consultant_key = requested_consultant
        else:
            # Accept a client-provided router hint if the UI already ran one, otherwise run the router locally.
            forced_direct = _should_force_direct(msg)
            if forced_direct:
                router_decision = RouterDecision(mode="direct", reason="forced_direct_tool_request")
                _log_progress(progress_log, "forced_direct", trigger="tool_request")
            else:
                router_decision = RouterDecision.from_dict(router_payload)
                if not router_decision:
                    router_decision = route_consultants(msg, history=history_messages)
            _log_progress(progress_log, "router", mode=router_decision.mode)
            if router_decision.mode == "single":
                consultant_key = router_decision.primary or DEFAULT_CONSULTANT_KEY
            elif router_decision.mode == "parallel":
                # When the router suggests multiple consultants, short-circuit and execute that workflow immediately.
                keys = router_decision.selected_consultants()
                try:
                    payload = _run_parallel_consultants(
                        msg,
                        project,
                        vector_store_id,
                        container_id,
                        keys,
                        router_decision,
                        progress_log,
                    )
                    return jsonify(payload)
                except Exception as exc:
                    return jsonify({"error": str(exc)}), 500

        # When a specific consultant is selected (router or explicit), run that persona immediately.
        if consultant_key:
            try:
                if router_decision and router_decision.mode == "single":
                    _log_progress(
                        progress_log,
                        "consultant_selected",
                        consultant=consultant_key,
                        reason=router_decision.reason,
                    )
                elif requested_consultant:
                    _log_progress(
                        progress_log,
                        "consultant_selected",
                        consultant=consultant_key,
                        reason="user_override",
                    )
                payload = _consultant_tool_call(
                    msg,
                    project,
                    vector_store_id,
                    container_id,
                    consultant_key,
                    router_decision=router_decision,
                    progress_log=progress_log,
                )
                return jsonify(payload)
            except Exception as exc:
                return jsonify({"error": str(exc)}), 500

        try:
            # No consultant selected, so fall back to the general chat model with whatever tools were requested.
            tools: List[Dict[str, Any]] = []
            if isinstance(incoming_tools, list):
                for tool in incoming_tools:
                    if not isinstance(tool, dict):
                        continue
                    sanitized = dict(tool)
                    # Ensure the session's container ID is passed through so code interpreter calls stay sticky.
                    if sanitized.get("type") == "code_interpreter":
                        if container_id:
                            sanitized["container"] = container_id
                        else:
                            sanitized["container"] = {"type": "auto"}
                    tools.append(sanitized)

            if vector_store_id:
                # Prepend file_search so the assistant can reference session uploads even in direct mode.
                has_file_search = any(
                    isinstance(tool, dict) and tool.get("type") == "file_search"
                    for tool in tools
                )
                if not has_file_search:
                    tools.insert(0, {"type": "file_search", "vector_store_ids": [vector_store_id]})

            # Surface every consultant as a callable function tool so the general model can delegate mid-conversation.
            for spec in CONSULTANT_TOOL_SPECS:
                tools.append(copy.deepcopy(spec))

            convo_input: List[Dict[str, str]] = [
                {"role": "system", "content": GENERAL_CHAT_SYSTEM},
            ]
            convo_input.extend(history_messages)
            convo_input.append({"role": "user", "content": msg})
            resp = client.responses.create(
                model=CHAT_MODEL,
                input=convo_input, #type: ignore
                tools=tools or None, # type: ignore
            )
            serialized = _ensure_dict(resp)
            consultant_calls = _extract_consultant_tool_calls(serialized.get("output") or [])
            if consultant_calls:
                # Execute only the first consultant tool request; follow-up calls will be handled by the next round trip.
                primary = consultant_calls[0]
                if len(consultant_calls) > 1:
                    print(f"[consultant-tools] Multiple consultant tool calls detected; executing {primary['tool_name']} first.")
                args = primary.get("arguments") or {}
                delegated_question = args.get("question") or msg
                context = args.get("context")
                if context:
                    context = context.strip()
                if delegated_question:
                    delegated_question = delegated_question.strip()
                if delegated_question and context:
                    delegated_question = f"{delegated_question}\n\nAdditional context:\n{context}"
                elif context and not delegated_question:
                    delegated_question = context
                delegated_question = delegated_question or msg
                delegated_vector_store = args.get("vector_store_id") or vector_store_id
                delegated_container = args.get("container_id") or container_id
                try:
                    _log_progress(
                        progress_log,
                        "consultant_tool_delegate",
                        tool=primary["tool_name"],
                        consultant=primary["consultant_key"],
                    )
                    payload = _consultant_tool_call(
                        delegated_question,
                        project,
                        delegated_vector_store,
                        delegated_container,
                        primary["consultant_key"],
                        router_decision=router_decision,
                        progress_log=progress_log,
                    )
                    payload["triggered_tool"] = primary["tool_name"]
                    payload["triggered_by_general_model"] = True
                    return jsonify(payload)
                except Exception as exc:
                    return jsonify({"error": str(exc)}), 500
            text = _extract_text(resp) or ""
            file_ids = getattr(resp, "output_file_ids", None) or []
            generated_files = []
            generated_files.extend(_process_server_tool_calls(serialized, vector_store_id))
            generated_files.extend(_link_files_to_vector_store(file_ids, vector_store_id))
            _log_progress(progress_log, "direct", model=CHAT_MODEL, delegated=False)
            return jsonify({
                "text": text,
                "mode": "direct",
                "file_ids": file_ids,
                "generated_files": generated_files,
                "output": serialized.get("output"),
                "output_text": serialized.get("output_text"),
                "choices": serialized.get("choices"),
                "router_decision": router_decision.to_dict() if router_decision else None,
                "progress_log": progress_log,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/consultants", methods=["GET"])
    def list_consultants_route():
        return jsonify({
            "consultants": list_consultants(),
            "overview": get_consultant_overview(),
        })

    @app.route("/v1/router/preview", methods=["POST"])
    def router_preview():
        payload = request.get_json(force=True) or {}
        message = (payload.get("message") or "").strip()
        if not message:
            return jsonify({"error": "message is required"}), 400
        history = payload.get("history")
        decision = route_consultants(message, history=history)
        return jsonify(describe_decision(decision))

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
            return send_file(
                io.BytesIO(data), # type: ignore
                download_name=f"{file_id}.bin",
                mimetype="application/octet-stream",
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

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
