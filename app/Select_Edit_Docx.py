"""Utilities for selecting and editing DOCX templates via OpenAI tools."""

from __future__ import annotations

from typing import Dict, List, Optional

from app.openai_client import client
from Template_Manager.template_manifest import load_manifest, ensure_manifest_vector_store


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

    raw_output = getattr(response, "output_text", None)
    if isinstance(raw_output, list):
        res = " ".join([str(item) for item in raw_output if isinstance(item, str)]).strip()
    elif isinstance(raw_output, str):
        res = raw_output.strip()
    else:
        res = ""
    best_file_id = None
    for template in manifest:
        template_file_id = template.get("file_id")
        if template_file_id and template_file_id in res:
            best_file_id = template_file_id
            break
    if not best_file_id:
        best_file_id = "UNKNOWN"

    return {
        "message": f"File {best_file_id} best matches this prompt: '{prompt}'",
        "file_id": best_file_id,
        "vector_store_id": template_vs_id,
        "response": getattr(response, "to_dict", lambda: {})(),
    }


def edit_docx_template(file_id: str, prompt: str) -> Dict[str, any]:
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
                    "Load the DOCX file, and perform the edits according to these instructions:\n"
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
    output_text = ""
    if isinstance(getattr(response, "output_text", None), list):
        output_text = " ".join(getattr(response, "output_text"))
    elif isinstance(getattr(response, "output_text", None), str):
        output_text = getattr(response, "output_text")
    message = output_text.strip() or f"Edited DOCX for file {file_id}."
    return {
        "message": message,
        "file_ids": file_ids,
        "response": getattr(response, "to_dict", lambda: {})(),
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
        if selection.get("message"):
            messages.append(selection["message"])

    edit_result: Optional[Dict[str, any]] = None
    if edit_instructions and selected_file_id and selected_file_id != "UNKNOWN":
        edit_result = edit_docx_template(selected_file_id, edit_instructions)
        if edit_result.get("message"):
            messages.append(edit_result["message"])

    return {
        "message": "\n\n".join(messages) if messages else "Selection completed.",
        "file_id": selected_file_id,
        "file_ids": (edit_result or {}).get("file_ids") if edit_result else [],
        "vector_store_id": template_vs_id,
        "selection": selection,
        "edit_result": edit_result,
    }
