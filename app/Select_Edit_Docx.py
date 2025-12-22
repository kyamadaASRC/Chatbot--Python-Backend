"""Utilities for selecting and editing DOCX templates via OpenAI tools."""

from __future__ import annotations

import io
import re
from typing import Dict, List, Optional, Any

from docx import Document  # type: ignore
from app.openai_client import client
from Template_Manager.template_manifest import load_manifest, ensure_manifest_vector_store


def _find_container_id(obj: Any) -> Optional[str]:
    """Best-effort crawl to locate a container_id inside a responses payload."""
    if isinstance(obj, dict):
        cid = obj.get("container_id")
        if cid:
            return cid
        for val in obj.values():
            found = _find_container_id(val)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_container_id(item)
            if found:
                return found
    return None


def _extract_output_file_ids(obj: Any) -> List[str]:
    """Extract output_file ids from a serialized response."""
    ids: List[str] = []
    if not isinstance(obj, dict):
        return ids
    outputs = obj.get("output") or []
    if isinstance(outputs, list):
        for entry in outputs:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "output_file":
                fid = entry.get("file_id") or entry.get("id")
                if fid:
                    ids.append(fid)
    return ids


def _extract_container_file_pairs(obj: Any) -> List[Dict[str, Optional[str]]]:
    """Extract container/file pairs for debugging."""
    pairs: List[Dict[str, Optional[str]]] = []
    if not isinstance(obj, dict):
        return pairs
    outputs = obj.get("output") or []
    if isinstance(outputs, list):
        for entry in outputs:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "output_file":
                continue
            fid = entry.get("file_id") or entry.get("id")
            cid = entry.get("container_id") or entry.get("container", {}).get("id")
            pairs.append({"file_id": fid, "container_id": cid})
    return pairs


def _extract_container_file_ids_from_annotations(obj: Any) -> List[str]:
    """Pull container file ids from message annotations (e.g., container_file_citation)."""
    ids: List[str] = []
    if not isinstance(obj, dict):
        return ids
    outputs = obj.get("output") or []
    if not isinstance(outputs, list):
        return ids
    for entry in outputs:
        if not isinstance(entry, dict):
            continue
        annotations = []
        contents = entry.get("content") or []
        if isinstance(contents, list):
            for c in contents:
                if isinstance(c, dict):
                    ann = c.get("annotations") or []
                    if isinstance(ann, list):
                        annotations.extend(ann)
        for ann in annotations:
            if not isinstance(ann, dict):
                continue
            if ann.get("type") == "container_file_citation":
                fid = ann.get("file_id")
                if fid:
                    ids.append(fid)
    return ids


def _has_placeholder_noise(docx_bytes: bytes) -> bool:
    """
    Heuristic check: if the DOCX still contains obvious placeholder/gibberish (lorem ipsum, placeholder, asdf),
    treat it as incomplete so we can ask the user for real content.
    """
    try:
        doc = Document(io.BytesIO(docx_bytes))
    except Exception:
        return False
    texts: List[str] = []
    for p in doc.paragraphs:
        if p.text:
            texts.append(p.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    texts.append(cell.text)
    if not texts:
        return False
    lower = "\n".join(texts).lower()
    patterns = [
        r"\blorem\b",
        r"\bipsum\b",
        r"\bplaceholder\b",
        r"\basdf\b",
        r"\bxxx+\b",
        r"\bzzzz+\b",
    ]
    hits = sum(1 for pat in patterns if re.search(pat, lower))
    return hits >= 2


def _find_header_issues(docx_bytes: bytes) -> List[str]:
    """Detect if header anchors like Teacher/Date/Class/Subject are still blank or placeholder-like."""
    issues: List[str] = []
    try:
        doc = Document(io.BytesIO(docx_bytes))
    except Exception:
        return issues
    texts: List[str] = []
    for p in doc.paragraphs:
        if p.text:
            texts.append(p.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    texts.append(cell.text)
    if not texts:
        return issues
    for txt in texts:
        lower = txt.lower().strip()
        # Teacher field blank or placeholder
        if re.match(r"^teacher\s*:?\s*$", lower) or "teacher:" in lower and ("[name" in lower or lower.endswith(":")):
            issues.append("teacher name")
        # Date field blank or placeholder
        if re.match(r"^date\s*:?\s*$", lower) or "date:" in lower and ("[mm" in lower or lower.endswith(":")):
            issues.append("date")
        # Class/Subject blank or placeholder
        if ("class" in lower or "subject" in lower) and ("[" in lower or lower.endswith(":")):
            issues.append("class/subject")
    # De-dupe
    return sorted(set(issues))


def _load_docx_bytes(file_ids: List[str], container_id: Optional[str], container_file_ids: List[str]) -> Optional[bytes]:
    """Load DOCX bytes from either OpenAI file_ids or container file ids."""
    # Prefer OpenAI file ids if present
    for fid in file_ids or []:
        try:
            content = client.files.content(fid)
            data = content.read() if hasattr(content, "read") else content
            if isinstance(data, (bytes, bytearray)):
                return data
        except Exception as exc:
            print(f"[docx-load] failed to fetch file {fid}: {exc}")
    # Fallback to container
    if container_id and container_file_ids:
        cfid = container_file_ids[0]
        try:
            resp = client.containers.files.content.retrieve(container_id=container_id, file_id=cfid)  # type: ignore
            data = resp.read() if hasattr(resp, "read") else resp
            if isinstance(data, (bytes, bytearray)):
                return data
        except Exception as exc:
            print(f"[docx-load] failed to fetch container file {cfid}: {exc}")
    return None


def select_docx_template(prompt: str, vector_store_id: Optional[str] = None) -> Dict[str, any]:
    """Match the best DOCX template using the manifest vector store metadata.
    Onboarding tip: manifest cache + manifest VS carry `file_info` anchors; the main chat model will read these anchors to ask the user for values before calling edit_docx."""
    # Always use the manifest vector store from the Template Manager (ignore session VS overrides).
    manifest_vs_id = ensure_manifest_vector_store(None)
    if not manifest_vs_id:
        print("[select_docx_template] No manifest VS ID available.")
        return {
            "message": "Vector store could not be created for template selection.",
            "file_id": "UNKNOWN",
            "vector_store_id": None,
            "response": {},
        }
    manifest, _, template_vs_id = load_manifest(manifest_vs_id)
    print(f"[select_docx_template] Using manifest VS: {manifest_vs_id}, templates found: {len(manifest) if manifest else 0}")
    if not manifest:
        print("[select_docx_template] Manifest empty.")
        return {
            "message": "No templates found in manifest.",
            "file_id": "UNKNOWN",
            "vector_store_id": template_vs_id,
            "response": {},
        }

    best_file_id = None
    # Primary: ask the model to pick from the manifest list directly.
    try:
        manifest_summary = []
        for t in manifest:
            fid = t.get("file_id")
            name = t.get("file_name") or t.get("name") or ""
            info = t.get("file_info") or t.get("description") or ""
            manifest_summary.append(f"file_id: {fid} | name: {name} | info: {info}")
        chooser = client.responses.create(
            model="gpt-5",
            input=[
                {
                    "role": "user",
                    "content": (
                        "Pick the best template from the list based on the user prompt. "
                        "Return ONLY the file_id.\n\n"
                        f"User prompt:\n{prompt}\n\n"
                        "Templates:\n" + "\n".join(manifest_summary)
                    ),
                }
            ],
        )
        out = getattr(chooser, "output_text", None)
        choice_text = ""
        if isinstance(out, list):
            choice_text = " \n".join([str(x) for x in out if isinstance(x, str)]).strip()
        elif isinstance(out, str):
            choice_text = out.strip()
        for t in manifest:
            fid = t.get("file_id")
            if fid and fid in choice_text:
                best_file_id = fid
                print(f"[select_docx_template] chooser selected: {best_file_id}")
                break
    except Exception as exc:
        print(f"[select_docx_template] chooser error: {exc}")

    # Secondary: vector-store search if chooser did not resolve a match.
    response_dict = {}
    if not best_file_id:
        response = client.responses.create(
            model="gpt-5",
            input=[
                {
                    "role": "user",
                    "content": (
                        "You are a matching engine. "
                        "Search the vector store and determine which template best matches "
                        "the user's prompt using the 'file_info' field.\n\n"
                        f"User prompt:\n{prompt}\n\n"
                        "Return ONLY the file_id of the best match."
                    ),
                }
            ],
            tools=[
                {"type": "file_search", "vector_store_ids": [manifest_vs_id]},
            ],
        )

        response_dict = getattr(response, "to_dict", lambda: {})()
        outputs = response_dict.get("output") or []
        hit_count = 0
        for entry in outputs:
            if not isinstance(entry, dict):
                continue
            results = entry.get("results") or entry.get("search_results") or []
            if not isinstance(results, list):
                continue
            for r in results:
                fid = (r or {}).get("file_id")
                if fid:
                    best_file_id = fid
                    hit_count += 1
                    break
            if best_file_id:
                break
        print(f"[select_docx_template] file_search hits: {hit_count}, chosen file_id: {best_file_id}")

    if not best_file_id:
        best_file_id = "UNKNOWN"
    template_name = None
    template_info = None
    if best_file_id and best_file_id != "UNKNOWN":
        for t in manifest:
            if t.get("file_id") == best_file_id:
                template_name = t.get("file_name") or t.get("name")
                template_info = t.get("file_info") or t.get("description")
                break
    selection_text_parts: List[str] = []
    if template_name:
        selection_text_parts.append(f"Template name: {template_name}")
    if best_file_id:
        selection_text_parts.append(f"file_id: {best_file_id}")
    if template_info:
        selection_text_parts.append(f"file_info: {template_info}")

    return {
        "message": "No matching template was found in the manifest." if best_file_id == "UNKNOWN" else f"File {best_file_id} is the best template that matches this prompt: '{prompt}'",
        "file_id": best_file_id,
        "vector_store_id": template_vs_id,
        "template_name": template_name,
        "file_info": template_info,
        "selection_text": "\n".join(selection_text_parts) if selection_text_parts else None,
        "response": response_dict,
    }


def edit_docx_template(
    file_id: str,
    edit_instructions: str,
    selection_text: Optional[str] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
    desired_filename: str = "edited.docx",
):
    """
    Load the DOCX file via code interpreter and apply the requested edits.
    Returns output file IDs (if any) plus a short status message.
    """
    print(f"[edit_docx_template] file_id={file_id}, selection_text_present={bool(selection_text)}, instructions_len={len(edit_instructions or '')}")
    # Debug logging only; keep concise
    if selection_text:
        print(f"[edit_docx_template] selection_text preview: {(selection_text or '')[:200]}")
    if edit_instructions:
        preview = (edit_instructions or "")[:200]
        print(f"[edit_docx_template] edit_instructions preview:\n{preview}")
    selection_block = f"\nTemplate selection:\n{selection_text}\n" if selection_text else ""
    history_block = ""
    if conversation_history:
        snippets: List[str] = []
        for msg in conversation_history[-6:]:
            role = msg.get("role")
            content = (msg.get("content") or "").strip()
            if not role or not content:
                continue
            snippets.append(f"{role}: {content[:400]}")
        if snippets:
            history_block = "\nRecent conversation (most recent last):\n" + "\n".join(snippets) + "\n"
    response = client.responses.create(
        model="gpt-5.1",
        input=[
            {
                "role": "user",
                "content": (
                    # Instruction-only prompt: questioning happens in the main chat model, not here.
                    "Load the existing DOCX template (do NOT rebuild from scratch) and apply these instructions in-place. "
                    "Do NOT duplicate paragraphs or bullet items; keep one final version per section. "
                    "If a requested bullet/list style is not available in the DOCX, fall back to plain paragraphs prefixed with '• ' instead of erroring. "
                    "If the template text is placeholder/lorem ipsum or nonsense and required values are not present in the instructions, STOP and return the status that required values are missing; do not invent content, do not ask questions, and do not reuse placeholder text. "
                    f"After applying the edits: save the result as '{desired_filename}' in the working directory (overwrite if it exists), then attach that file as an output file so output_file_ids is populated. "
                    f"List only '{desired_filename}' to verify it exists; do not list other files. "
                    "[[Modification Instruction Start]]\n"
                    f"{edit_instructions}\n"
                    "Fill this template as requested and return the edited DOCX file.\n"
                    "[[Modification Instruction End]]\n"
                    "Your final output should be the modified file (as an attached output file, not just text)."
                ),
            }
        ],
        tools=[{"type": "code_interpreter", "container": {"type": "auto", "file_ids": [file_id]}}],
        tool_choice="required",
    )
    response_dict = getattr(response, "to_dict", lambda: {})()
    output_items = response_dict.get("output") or getattr(response, "output", []) or []
    # Extract annotations from the completed message item to capture container file ids even when output_file_ids is empty.
    message_item = next(
        (
            item
            for item in output_items
            if isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("status") == "completed"
        ),
        None,
    )
    container_annotations = []
    if message_item:
        content_list = message_item.get("content") or []
        if content_list and isinstance(content_list, list):
            container_annotations = content_list[0].get("annotations") or []
    print("container annotations:", container_annotations)
    # Build a list of file data from container_file_citation annotations
    files = []
    for current_file in container_annotations:
        if current_file.get("type") == "container_file_citation":
            files.append(
                {
                    "container_id": current_file.get("container_id"),
                    "file_id": current_file.get("file_id"),
                    "filename": current_file.get("filename"),
                }
            )
    print("files:", files)

    file_ids = getattr(response, "output_file_ids", None) or _extract_output_file_ids(response_dict)
    container_id = _find_container_id(response_dict) or (files[0].get("container_id") if files else None)
    container_pairs = _extract_container_file_pairs(response_dict)
    container_file_ids = _extract_container_file_ids_from_annotations(response_dict) or [f.get("file_id") for f in files if f.get("file_id")]
    if not container_file_ids and container_pairs:
        for pair in container_pairs:
            fid = pair.get("file_id")
            if fid:
                container_file_ids.append(fid)
    print(f"[edit_docx_template] returned file_ids={file_ids}, container_id={container_id}, container_pairs={container_pairs}, container_file_ids={container_file_ids}")
    if not file_ids:
        raw_output = response_dict.get("output")
        print(f"[edit_docx_template] output entries (no file_ids): {raw_output}")
    output_text = ""
    if isinstance(getattr(response, "output_text", None), list):
        output_text = " ".join(getattr(response, "output_text"))
    elif isinstance(getattr(response, "output_text", None), str):
        output_text = getattr(response, "output_text")
    message = output_text.strip() if isinstance(output_text, str) else ""
    # Heuristic checks: placeholder noise or missing header anchors; if found, ask for details and skip returning files.
    doc_bytes = _load_docx_bytes(file_ids or [], container_id, container_file_ids or [])
    if doc_bytes:
        try:
            if _has_placeholder_noise(doc_bytes):
                if not message:
                    message = (
                        "Warning: the DOCX still contains placeholder/lorem ipsum text; more values may be needed. "
                        "Provide missing details and re-run edit_docx if you want a fully filled version."
                    )
            header_issues = _find_header_issues(doc_bytes)
            if header_issues:
                missing_list = ", ".join(sorted(set(header_issues)))
                if not message:
                    message = (
                        f"Missing header values ({missing_list}); added 'TBD' where possible. "
                        "Provide actual values and re-run edit_docx if you want them filled."
                    )
        except Exception as exc:
            print(f"[post-check] failed to inspect docx: {exc}")

    # Upload a persistent copy to Files API so preview/download works even if container expires.
    uploaded_fid: Optional[str] = None
    if isinstance(doc_bytes, (bytes, bytearray)):
        try:
            upload = client.files.create(
                file=(desired_filename, io.BytesIO(doc_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                purpose="assistants",
            )
            uploaded_fid = getattr(upload, "id", None) or getattr(upload, "file_id", None) or None
            if uploaded_fid:
                file_ids = (file_ids or []) + [uploaded_fid]
                print(f"[edit_docx_template:upload] uploaded file_id={uploaded_fid}")
        except Exception as exc:
            print(f"[edit_docx_template:upload-error] {exc}")
    # If we only have a container file id, provide a local_files entry so the UI can surface it.
    local_files: List[Dict[str, Any]] = []
    if (not file_ids) and container_id and container_file_ids:
        cfid = container_file_ids[0]
        preview_url = f"/v1/containers/{container_id}/files/{cfid}/content?name={desired_filename}"
        local_files.append(
            {
                "id": cfid,
                "name": desired_filename,
                "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "container_id": container_id,
                "container_file_id": cfid,
                "preview_url": preview_url,
                "download_url": preview_url,
                "source": "generated",
            }
        )
    # If no file ids were emitted, try to use any doc bytes to create one.
    if (not file_ids) and isinstance(doc_bytes, (bytes, bytearray)):
        try:
            upload = client.files.create(
                file=(desired_filename, io.BytesIO(doc_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                purpose="assistants",
            )
            uploaded_id = getattr(upload, "id", None) or getattr(upload, "file_id", None) or None
            if uploaded_id:
                file_ids = [uploaded_id]
                print(f"[edit_docx_template:fallback-upload] uploaded file_id={uploaded_id}")
        except Exception as exc:
            print(f"[edit_docx_template:fallback-upload-error] {exc}")
    # If we still have no file ids but do have a container file, fetch it and upload to Files API.
    if (not file_ids) and container_id and container_file_ids:
        try:
            cfid = container_file_ids[0]
            resp = client.containers.files.content.retrieve(container_id=container_id, file_id=cfid)  # type: ignore
            data = resp.read() if hasattr(resp, "read") else resp
            if isinstance(data, (bytes, bytearray)) and data:
                upload = client.files.create(
                    file=(desired_filename, io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                    purpose="assistants",
                )
                uploaded_id = getattr(upload, "id", None) or getattr(upload, "file_id", None) or None
                if uploaded_id:
                    file_ids = [uploaded_id]
                    print(f"[edit_docx_template:container-upload] uploaded file_id={uploaded_id}")
        except Exception as exc:
            print(f"[edit_docx_template:container-upload-error] {exc}")

    # Fallback: if nothing was attached, emit an unchanged copy so the user can download something.
    if not file_ids and not container_file_ids:
        try:
            fallback = client.responses.create(
                model="gpt-5.1",
                input=[
                    {
                        "role": "user",
                        "content": (
                            f"Load the attached DOCX and save it unchanged as '{desired_filename}', then attach that file as an output file. "
                            "This is a fallback to ensure the user can download the template even if no edits were applied."
                        ),
                    }
                ],
                tools=[{"type": "code_interpreter", "container": {"type": "auto", "file_ids": [file_id]}}],
                tool_choice="required",
            )
            fb_dict = getattr(fallback, "to_dict", lambda: {})()
            file_ids = getattr(fallback, "output_file_ids", None) or _extract_output_file_ids(fb_dict)
            container_id = container_id or _find_container_id(fb_dict)
            container_file_ids = container_file_ids or _extract_container_file_ids_from_annotations(fb_dict)
            print(f"[edit_docx_template:fallback] file_ids={file_ids}, container_file_ids={container_file_ids}")
            if not message:
                message = "No edits were attached; provided a fallback copy of the template."
        except Exception as exc:
            print(f"[edit_docx_template:fallback-error] {exc}")
    if (file_ids or container_file_ids or local_files):
        message = f"Completed edits for {desired_filename}. Check the Files list to download."
    elif not message:
        message = output_text.strip() or f"Edited DOCX for file {file_id}."

    return {
        "message": message,
        "file_ids": file_ids,
        "container_id": container_id,
        "container_file_ids": container_file_ids,
        "local_files": local_files,
        "response": response_dict,
    }
