"""Utilities for selecting and editing DOCX templates via OpenAI tools."""

from __future__ import annotations

from typing import Dict, List, Optional, Any

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


def select_docx_template(prompt: str, vector_store_id: Optional[str] = None) -> Dict[str, any]:
    """Match the best DOCX template using the manifest vector store metadata."""
    manifest_vs_id = ensure_manifest_vector_store(vector_store_id)
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
    if best_file_id and best_file_id != "UNKNOWN":
        for t in manifest:
            if t.get("file_id") == best_file_id:
                template_name = t.get("file_name") or t.get("name")
                break

    return {
        "message": "No matching template was found in the manifest." if best_file_id == "UNKNOWN" else f"File {best_file_id} is the best template that matches this prompt: '{prompt}'",
        "file_id": best_file_id,
        "vector_store_id": template_vs_id,
        "template_name": template_name,
        "response": response_dict,
    }


def edit_docx_template(file_id: str, edit_instructions: str, selection_text: Optional[str] = None) -> Dict[str, Any]:
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
    response = client.responses.create(
        model="gpt-5.1",
        input=[
            {
                "role": "user",
                "content": (
                    "Load the DOCX template and apply these instructions. "
                    "If required details are missing, PAUSE and ask the user clarifying questions before proceeding. "
                    "After applying the edits: save the result as edited.docx in the working directory, then attach edited.docx as an output file so output_file_ids is populated. "
                    "List the working directory before finishing to verify edited.docx exists. "
                    f"{selection_block}"
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
    file_ids = getattr(response, "output_file_ids", None) or _extract_output_file_ids(response_dict)
    container_id = _find_container_id(response_dict)
    container_pairs = _extract_container_file_pairs(response_dict)
    container_file_ids = _extract_container_file_ids_from_annotations(response_dict)
    print(f"[edit_docx_template] returned file_ids={file_ids}, container_id={container_id}, container_pairs={container_pairs}, container_file_ids={container_file_ids}")
    if not file_ids:
        raw_output = response_dict.get("output")
        print(f"[edit_docx_template] output entries (no file_ids): {raw_output}")
    output_text = ""
    if isinstance(getattr(response, "output_text", None), list):
        output_text = " ".join(getattr(response, "output_text"))
    elif isinstance(getattr(response, "output_text", None), str):
        output_text = getattr(response, "output_text")
    message = output_text.strip() or f"Edited DOCX for file {file_id}."

    return {
        "message": message,
        "file_ids": file_ids,
        "container_id": container_id,
        "container_file_ids": container_file_ids,
        "response": response_dict,
    }


def select_and_edit_docx(
    prompt: str,
    edit_instructions: Optional[str] = None,
    file_id: Optional[str] = None,
    vector_store_id: Optional[str] = None,
) -> Dict[str, any]:
    """
    Convenience wrapper: pick the best template (via manifest vector store),
    then optionally run edits against it.
    """
    messages: List[str] = []
    selected_file_id = file_id or None
    selection: Optional[Dict[str, any]] = None
    template_vs_id = None
    template_name = None
    if not selected_file_id:
        selection = select_docx_template(prompt, vector_store_id=vector_store_id)
        selected_file_id = selection.get("file_id")
        template_vs_id = selection.get("vector_store_id")
        template_name = selection.get("template_name")

    edit_result: Optional[Dict[str, any]] = None
    if not selected_file_id or selected_file_id == "UNKNOWN":
        if selection and selection.get("message"):
            messages.append(selection.get("message"))
    else:
        selection_msg = (selection or {}).get("message")
        parts: List[str] = []
        if edit_instructions:
            parts.append(str(edit_instructions))
        edit_text = "\n\n".join(parts).strip() or prompt
        print(f"[select_and_edit_docx] editing file_id={selected_file_id}, template_vs={template_vs_id}, selection_msg_present={bool(selection_msg)}, instructions_len={len(edit_text or '')}")
        if selection_msg:
            print(f"[select_and_edit_docx] selection_msg:\n{selection_msg}")
        if edit_text:
            preview = edit_text[:400]
            print(f"[select_and_edit_docx] edit_text (first 400 chars):\n{preview}")
        edit_result = edit_docx_template(selected_file_id, edit_text, selection_text=selection_msg)
        if edit_result.get("message"):
            messages.append(edit_result["message"])

    filled_name = None
    if template_name:
        base = template_name
        if base.lower().endswith(".docx"):
            base = base[:-5]
        filled_name = f"{base} [FILLED].docx"

    return {
        "message": "\n\n".join(messages) if messages else "Selection completed.",
        "file_id": selected_file_id,
        "file_ids": (edit_result or {}).get("file_ids") if edit_result else [],
        "container_id": (edit_result or {}).get("container_id"),
        "container_file_ids": (edit_result or {}).get("container_file_ids"),
        "filled_filename": filled_name or "edited.docx",
        # Keep session vector store for linking; also expose which store contained the template.
        "vector_store_id": vector_store_id,
        "template_vector_store_id": template_vs_id,
        "selection": selection,
        "edit_result": edit_result,
    }
