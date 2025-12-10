"""Manage template manifest files and OpenAI vector store attachments."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from Template_Manager.openai_client import client, api_key

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_VECTOR_STORE_PATH = BASE_DIR / "template_vector_store_id.txt"
MANIFEST_FILE_ID_PATH = BASE_DIR / "template_manifest_file_id.txt"
MANIFEST_VECTOR_STORE_PATH = BASE_DIR / "manifest_vector_store_id.txt"
MANIFEST_CACHE_PATH = BASE_DIR / "manifest_cache.json"


def _extract_id(obj: Any) -> str | None:
    if obj is None:
        return None
    if hasattr(obj, "id"):
        return getattr(obj, "id")
    if isinstance(obj, dict):
        return obj.get("id")
    if hasattr(obj, "to_dict"):
        data = obj.to_dict()
        if isinstance(data, dict):
            return data.get("id")
    return None


def ensure_template_vector_store(vector_store_id: str | None = None) -> str:
    """Return a template vector store id, creating/caching as needed."""
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing; cannot access template vector store.")
    cached = vector_store_id or (TEMPLATE_VECTOR_STORE_PATH.read_text().strip() if TEMPLATE_VECTOR_STORE_PATH.exists() else "")
    if cached:
        return cached
    vs = client.vector_stores.create(name="template-manifest-store")
    vs_id = _extract_id(vs) or ""
    if vs_id:
        TEMPLATE_VECTOR_STORE_PATH.write_text(vs_id, encoding="utf-8")
    return vs_id


def _create_manifest_vector_store() -> str:
    """Create a manifest vector store and cache its id."""
    vs = client.vector_stores.create(name="template-manifest-store")
    vs_id = _extract_id(vs) or ""
    if vs_id:
        MANIFEST_VECTOR_STORE_PATH.write_text(vs_id, encoding="utf-8")
    return vs_id


def ensure_manifest_vector_store(vector_store_id: str | None = None) -> str:
    """Separate vector store dedicated to manifest JSON for faster searching.

    If no id is provided or the file contains a placeholder, create one automatically.
    """
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing; cannot access manifest vector store.")
    # Prefer the on-disk manifest id; only fall back to provided override when explicitly set.
    cached = (MANIFEST_VECTOR_STORE_PATH.read_text().strip() if MANIFEST_VECTOR_STORE_PATH.exists() else "") or vector_store_id or ""
    if cached and not cached.startswith("REPLACE_WITH") and len(cached) >= 5:
        return cached
    # Auto-create when missing or placeholder to avoid hard failures.
    vs_id = _create_manifest_vector_store()
    if not vs_id:
        raise RuntimeError("Failed to create manifest vector store.")
    return vs_id


def _attach_file(vector_store_id: str, file_id: str) -> None:
    try:
        client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=file_id)
    except Exception as exc:  # pragma: no cover - diagnostic logging only
        print(f"[template-manifest] Failed to attach file {file_id} to {vector_store_id}: {exc}")


def _delete_file(file_id: str) -> None:
    try:
        client.files.delete(file_id)
    except Exception:
        pass


def _delete_vs_file(vector_store_id: str, file_id: str) -> None:
    delete = getattr(client.vector_stores.files, "delete", None)
    if not delete:
        return
    try:
        delete(vector_store_id=vector_store_id, file_id=file_id)
    except Exception:
        pass


def _upload_manifest(data: List[Dict[str, Any]], manifest_vector_store_id: str) -> str:
    """Upload manifest JSON, attach it to the manifest vector store, and cache its id."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8") as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name
    with open(tmp_path, "rb") as handle:
        uploaded = client.files.create(file=(tmp_path, handle), purpose="assistants")
    manifest_id = _extract_id(uploaded) or ""
    if manifest_id:
        _attach_file(manifest_vector_store_id, manifest_id)
        MANIFEST_FILE_ID_PATH.write_text(manifest_id, encoding="utf-8")
    return manifest_id


def _read_file_content(file_id: str) -> bytes:
    """Return file content as bytes regardless of SDK return type."""
    content = client.files.content(file_id)
    if hasattr(content, "read"):
        return content.read()
    if isinstance(content, str):
        return content.encode("utf-8")
    return content or b""


def load_manifest(vector_store_id: str | None = None) -> Tuple[List[Dict[str, Any]], str, str]:
    """
    Fetch manifest JSON from the template vector store.
    If missing, create an empty manifest and upload it.
    """
    manifest_vs_id = ensure_manifest_vector_store(vector_store_id)
    template_vs_id = ensure_template_vector_store(None)
    manifest_id = MANIFEST_FILE_ID_PATH.read_text().strip() if MANIFEST_FILE_ID_PATH.exists() else ""
    if manifest_id.startswith("REPLACE_WITH"):
        manifest_id = ""
    manifest_data: List[Dict[str, Any]] = []

    def _load_json_blob(blob_obj: Any, source: str) -> List[Dict[str, Any]]:
        try:
            if blob_obj is None:
                raise ValueError("manifest blob is None")
            if isinstance(blob_obj, (bytes, bytearray, memoryview)):
                text = bytes(blob_obj).decode("utf-8")
            elif isinstance(blob_obj, str):
                text = blob_obj
            else:
                text = str(blob_obj)
            return json.loads(text)
        except Exception as exc:
            print(f"[template-manifest] Failed to parse manifest JSON from {source}: {exc}")
            return []

    # Prefer local cache to avoid download restrictions on assistants files.
    if MANIFEST_CACHE_PATH.exists():
        try:
            cached = json.loads(MANIFEST_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                manifest_data = cached
        except Exception as exc:
            print(f"[template-manifest] Failed to read local manifest cache: {exc}")

    # If we still have nothing, try cached manifest id (may fail due to purpose restrictions).
    # If cache is empty, force a fresh empty manifest upload instead of downloading (assistants files block download).
    if not manifest_data:
        manifest_id = _upload_manifest([], manifest_vs_id)
        manifest_data = []

    # Cache locally for subsequent loads.
    try:
        MANIFEST_CACHE_PATH.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[template-manifest] Failed to write local manifest cache: {exc}")

    if manifest_id and not MANIFEST_FILE_ID_PATH.exists():
        MANIFEST_FILE_ID_PATH.write_text(manifest_id, encoding="utf-8")

    return manifest_data, manifest_id, template_vs_id


def _generate_file_info(file_id: str, file_name: str) -> str:
    """Ask the model (with code interpreter) to summarize the template for selection/anchors."""
    try:
        resp = client.responses.create(
            model="gpt-5.1",
            input=[
                {
                    "role": "user",
                    "content": (
                        "Inspect the attached DOCX and generate a structured file_info description similar to the sample "
                        "in template_file_structure.txt.\n\n"
                        "Priorities:\n"
                        "- Provide a concise description and the main sections.\n"
                        "- List any fillable/logical fields or placeholders.\n"
                        "- Add a short 'Selection Keywords' line with 5-12 keywords/phrases that help choose this template.\n"
                        "- Keep content compact and deterministic so it can be reused as anchor hints during editing.\n"
                        "Return plain Markdown text only."
                    ),
                }
            ],
            tools=[{"type": "code_interpreter", "container": {"type": "auto", "file_ids": [file_id]}}],
            tool_choice="required",
        )
        output = getattr(resp, "output_text", None)
        if isinstance(output, list):
            return "\n".join([str(item) for item in output if isinstance(item, str)]).strip()
        if isinstance(output, str):
            return output.strip()
    except Exception as exc:
        print(f"[template-manifest] file_info generation failed for {file_id}: {exc}")
        return f"{file_name} uploaded; file_info generation unavailable. Please retry."
    return f"{file_name} uploaded; file_info generation unavailable. Please retry."


def _find_entry_index(manifest: List[Dict[str, Any]], file_name: str) -> int:
    for idx, entry in enumerate(manifest):
        if entry.get("file_name") == file_name:
            return idx
    return -1


def add_templates(files: List[Any], vector_store_id: str | None = None) -> Dict[str, Any]:
    """
    Upload files, attach to template vector store, regenerate manifest JSON, and return updated manifest.
    Files is a list of Werkzeug FileStorage-like objects (from request.files.getlist).
    """
    template_vs_id = ensure_template_vector_store(vector_store_id)
    manifest_vs_id = ensure_manifest_vector_store(None)
    manifest, old_manifest_id, _ = load_manifest(manifest_vs_id)
    for file in files:
        uploaded = client.files.create(
            file=(file.filename, file.stream, file.mimetype or "application/octet-stream"),
            purpose="assistants",
        )
        file_id = _extract_id(uploaded)
        if not file_id:
            continue
        _attach_file(template_vs_id, file_id)
        # Remove any previous file with the same name so the template store stays in sync with the manifest.
        existing_idx = _find_entry_index(manifest, file.filename)
        if existing_idx >= 0:
            old_id = manifest[existing_idx].get("file_id")
            if old_id and old_id != file_id:
                _delete_vs_file(template_vs_id, old_id)
                _delete_file(old_id)
        file_info = _generate_file_info(file_id, file.filename)
        entry = {
            "file_name": file.filename,
            "file_id": file_id,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "file_info": file_info,
        }
        if existing_idx >= 0:
            manifest[existing_idx] = entry
        else:
            manifest.append(entry)
    manifest = sorted(manifest, key=lambda e: e.get("file_name", "").lower())
    new_manifest_id = _upload_manifest(manifest, manifest_vs_id)
    if old_manifest_id and old_manifest_id != new_manifest_id:
        _delete_vs_file(manifest_vs_id, old_manifest_id)
        _delete_file(old_manifest_id)
    try:
        MANIFEST_CACHE_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[template-manifest] Failed to write local manifest cache after upload: {exc}")
    return {
        "manifest": manifest,
        "manifest_file_id": new_manifest_id,
        "template_vector_store_id": template_vs_id,
        "manifest_vector_store_id": manifest_vs_id,
    }


def delete_template(file_name: str, vector_store_id: str | None = None) -> Dict[str, Any]:
    """Remove an entry and its file from the manifest and vector store."""
    template_vs_id = ensure_template_vector_store(vector_store_id)
    manifest_vs_id = ensure_manifest_vector_store(None)
    manifest, old_manifest_id, _ = load_manifest(manifest_vs_id)
    remaining: List[Dict[str, Any]] = []
    removed_file_ids: List[str] = []
    for entry in manifest:
        if entry.get("file_name") == file_name:
            fid = entry.get("file_id")
            if fid:
                removed_file_ids.append(fid)
            continue
        remaining.append(entry)
    for fid in removed_file_ids:
        _delete_vs_file(template_vs_id, fid)
        _delete_file(fid)
    new_manifest_id = _upload_manifest(remaining, manifest_vs_id)
    if old_manifest_id and old_manifest_id != new_manifest_id:
        _delete_vs_file(manifest_vs_id, old_manifest_id)
        _delete_file(old_manifest_id)
    try:
        MANIFEST_CACHE_PATH.write_text(json.dumps(remaining, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[template-manifest] Failed to write local manifest cache after delete: {exc}")
    return {
        "manifest": remaining,
        "manifest_file_id": new_manifest_id,
        "template_vector_store_id": template_vs_id,
        "manifest_vector_store_id": manifest_vs_id,
        "removed_file_ids": removed_file_ids,
    }
