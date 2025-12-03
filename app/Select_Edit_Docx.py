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


def select_docx_template(prompt: str, vector_store_id: Optional[str] = None) -> Dict[str, any]:
    """Match the best DOCX template using the manifest vector store metadata."""
    manifest_vs_id = ensure_manifest_vector_store(vector_store_id)
    if not manifest_vs_id:
        return {
            "message": "Vector store could not be created for template selection.",
            "file_id": "UNKNOWN",
            "vector_store_id": None,
            "response": {},
        }
    manifest, _, template_vs_id = load_manifest(manifest_vs_id)
    if not manifest:
        return {
            "message": "No templates found in manifest.",
            "file_id": "UNKNOWN",
            "vector_store_id": template_vs_id,
            "response": {},
        }

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

    # Try to read the tool results directly; fall back to text matching; finally pick the first manifest entry.
    response_dict = getattr(response, "to_dict", lambda: {})()
    best_file_id = None
    outputs = response_dict.get("output") or []
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
                break
        if best_file_id:
            break
    if not best_file_id:
        raw_output = getattr(response, "output_text", None)
        if isinstance(raw_output, list):
            res = " ".join([str(item) for item in raw_output if isinstance(item, str)]).strip()
        elif isinstance(raw_output, str):
            res = raw_output.strip()
        else:
            res = ""
        for template in manifest:
            template_file_id = template.get("file_id")
            if template_file_id and template_file_id in res:
                best_file_id = template_file_id
                break
    if not best_file_id and manifest:
        best_file_id = manifest[0].get("file_id")
    if not best_file_id:
        best_file_id = "UNKNOWN"

    return {
        "message": f"File {best_file_id} best matches this prompt: '{prompt}'",
        "file_id": best_file_id,
        "vector_store_id": template_vs_id,
        "response": response_dict,
    }


def edit_docx_template(file_id: str, prompt: str) -> Dict[str, Any]:
    """
    Load the DOCX file via code interpreter and apply the requested edits.
    Returns output file IDs (if any) plus a short status message.
    """
    response = client.responses.create(
        model="gpt-5.1",
        input=[
            {
                "role": "user",
                "content": (
                    "Load the DOCX file and apply these instructions. "
                    "If required details are missing, PAUSE and ask the user clarifying questions before proceeding. "
                    "If you already have sufficient details, proceed to produce the final modified file now. "
                    "Save the result as edited.docx in the working directory, then attach edited.docx as an output file from this tool call. "
                    "Do NOT return sandbox/local file paths or download links—only attach the file output so output_file_ids is populated. "
                    "Do NOT make up details or add placeholders on your own. "
                    "Only produce the final modified file after you have the necessary details.\n"
                    "[[Modification Instruction Start]]\n"
                    f"{prompt}\n"
                    "[[Modification Instruction End]]\n"
                    "Your final output should be the modified file."
                ),
            }
        ],
        tools=[{"type": "code_interpreter", "container": {"type": "auto", "file_ids": [file_id]}}],
        tool_choice="required",
    )
    file_ids = getattr(response, "output_file_ids", None) or []
    response_dict = getattr(response, "to_dict", lambda: {})()
    container_id = _find_container_id(response_dict)
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
    selected_file_id = file_id
    selection: Optional[Dict[str, any]] = None
    template_vs_id = None
    if not selected_file_id:
        selection = select_docx_template(prompt, vector_store_id=vector_store_id)
        selected_file_id = selection.get("file_id")
        template_vs_id = selection.get("vector_store_id")

    edit_result: Optional[Dict[str, any]] = None
    if selected_file_id and selected_file_id != "UNKNOWN":
        selection_msg = (selection or {}).get("message")
        edit_prompt = edit_instructions or selection_msg or prompt
        edit_result = edit_docx_template(selected_file_id, edit_prompt)
        if edit_result.get("message"):
            messages.append(edit_result["message"])

    return {
        "message": "\n\n".join(messages) if messages else "Selection completed.",
        "file_id": selected_file_id,
        "file_ids": (edit_result or {}).get("file_ids") if edit_result else [],
        "container_id": (edit_result or {}).get("container_id"),
        # Keep session vector store for linking; also expose which store contained the template.
        "vector_store_id": vector_store_id,
        "template_vector_store_id": template_vs_id,
        "selection": selection,
        "edit_result": edit_result,
    }
