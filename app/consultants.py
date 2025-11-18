"""Consultant registry and execution utilities."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.openai_client import client

DEFAULT_MODELS = ["gpt-5", "gpt-4.1"]
BASE_DIR = Path(__file__).resolve().parent
CONSULTANTS_ROOT = BASE_DIR / "consultants"
VECTOR_CACHE_PATH = CONSULTANTS_ROOT / "vector_store_cache.json"
OVERVIEW_PATH = CONSULTANTS_ROOT / "overview.md"
DEFAULT_INSTRUCTION_TEXT = "You are the Agent_iWant_GPT assistant."


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "_")


def _load_metadata(directory: Path) -> Dict[str, Any]:
    meta_path = directory / "metadata.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _locate_instruction(directory: Path, explicit_name: Optional[str] = None) -> Optional[Path]:
    if explicit_name:
        candidate = directory / explicit_name
        if candidate.exists():
            return candidate
    for name in ("instruction.txt", "Instruction.txt", "Instructions.txt"):
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _gather_resource_files(directory: Path, ignore: Optional[List[str]] = None) -> List[str]:
    ignore = ignore or []
    resources: List[str] = []
    for item in directory.iterdir():
        if not item.is_file():
            continue
        if item.name in ignore:
            continue
        resources.append(str(item))
    return sorted(resources)


def _load_vector_cache() -> Dict[str, Any]:
    if not VECTOR_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(VECTOR_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_vector_cache(data: Dict[str, Any]) -> None:
    VECTOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTOR_CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _vector_store_exists(vs_id: str) -> bool:
    if not vs_id:
        return False
    retrieve = getattr(client.vector_stores, "retrieve", None)
    if retrieve is None:
        # Older SDKs may not expose retrieve; assume success to avoid thrashing.
        return True
    try:
        retrieve(vs_id)
        return True
    except Exception:
        return False


def _file_signature(paths: List[str]) -> List[Dict[str, Any]]:
    signature: List[Dict[str, Any]] = []
    for raw_path in sorted(paths):
        p = Path(raw_path)
        if not p.exists():
            signature.append({"path": str(p), "missing": True})
            continue
        stat = p.stat()
        signature.append(
            {
                "path": str(p),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return signature


def _extract_id(obj: Any) -> Optional[str]:
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


def _upload_files_to_vector_store(vs_id: str, files: List[str]) -> List[Dict[str, str]]:
    uploaded: List[Dict[str, str]] = []
    for filepath in files:
        path = Path(filepath)
        if not path.exists():
            print(f"[consultants] Skipping missing file {filepath}")
            continue
        with path.open("rb") as handle:
            uploaded = client.files.create(
                file=(path.name, handle, "application/octet-stream"),
                purpose="assistants",
            )
        file_id = _extract_id(uploaded)
        if not file_id:
            raise RuntimeError(f"Failed to upload file {filepath}: missing id")
        client.vector_stores.files.create(vector_store_id=vs_id, file_id=file_id)
        uploaded.append({"path": str(path), "file_id": file_id})
    return uploaded


def _create_vector_store_for(display_name: str, key: str) -> str:
    vs = client.vector_stores.create(name=f"{display_name} ({key}) resources")
    vs_id = _extract_id(vs)
    if not vs_id:
        raise RuntimeError("Vector store creation returned object without id")
    return vs_id


def _initialize_vector_stores(registry: Dict[str, Dict[str, Any]]) -> None:
    cache = _load_vector_cache()
    updated = False

    for key, meta in registry.items():
        local_files = meta.get("local_files") or []
        if not local_files:
            continue

        cached = cache.get(key) or {}
        cached_vs = cached.get("vector_store_id") or meta.get("vector_store_id")
        cached_signature = cached.get("file_signature")
        current_signature = _file_signature(local_files)

        if (
            cached_vs
            and cached_signature == current_signature
            and _vector_store_exists(cached_vs)
        ):
            meta["vector_store_id"] = cached_vs
            meta["uploaded_files"] = cached.get("uploaded_files", [])
            continue

        print(f"[consultants] Creating vector store for {key} with {len(local_files)} files…")
        vs_id = _create_vector_store_for(meta.get("display_name", key), key)
        uploaded_records = _upload_files_to_vector_store(vs_id, local_files)
        meta["vector_store_id"] = vs_id
        meta["uploaded_files"] = uploaded_records
        cache[key] = {
            "vector_store_id": vs_id,
            "file_signature": current_signature,
            "uploaded_files": uploaded_records,
        }
        updated = True

    if updated:
        _save_vector_cache(cache)


def _discover_consultants() -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    if not CONSULTANTS_ROOT.exists():
        return registry

    for directory in sorted(CONSULTANTS_ROOT.iterdir()):
        if not directory.is_dir():
            continue
        metadata = _load_metadata(directory)
        key = metadata.get("key") or _slugify(directory.name)
        instruction_path = _locate_instruction(directory, metadata.get("instruction_file"))
        if instruction_path:
            instructions = instruction_path.read_text(encoding="utf-8").strip()
        else:
            instructions = metadata.get("instructions", DEFAULT_INSTRUCTION_TEXT)

        ignore_files = ["metadata.json"]
        if instruction_path:
            ignore_files.append(instruction_path.name)
        local_files = _gather_resource_files(directory, ignore_files)

        display_name = metadata.get("display_name") or directory.name
        aliases = metadata.get("aliases") or []
        alias_candidates = {
            key,
            key.replace("_", " "),
            directory.name.lower(),
            display_name.lower(),
        }
        alias_candidates.update(alias.strip().lower() for alias in aliases if isinstance(alias, str))

        summary = metadata.get("summary")
        if not summary:
            lines = [line.strip() for line in instructions.splitlines() if line.strip()]
            summary = lines[0] if lines else ""
        registry[key] = {
            "key": key,
            "display_name": display_name,
            "instructions": instructions,
            "summary": summary,
            "model_try": metadata.get("model_try") or DEFAULT_MODELS,
            "tools": metadata.get("tools") or [{"type": "file_search"}],
            "local_files": local_files,
            "aliases": sorted(alias_candidates),
            "keywords": [kw.lower() for kw in metadata.get("keywords", []) if isinstance(kw, str)],
            "vector_store_id": metadata.get("vector_store_id"),
            "uploaded_files": [],
            "conversation_starters": [
                starter
                for starter in metadata.get("conversation_starters", [])
                if isinstance(starter, str) and starter.strip()
            ],
        }
    return registry


def _response_to_dict(resp: Any) -> Dict[str, Any]:
    if hasattr(resp, "model_dump"):
        try:
            return resp.model_dump()
        except Exception:
            pass
    if hasattr(resp, "to_dict"):
        try:
            data = resp.to_dict()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


CONSULTANTS = _discover_consultants()
if not CONSULTANTS:
    CONSULTANTS = {
        "agent_iwant_gpt": {
            "key": "agent_iwant_gpt",
        "display_name": "Agent iWant GPT",
        "instructions": DEFAULT_INSTRUCTION_TEXT,
        "model_try": DEFAULT_MODELS,
        "tools": [{"type": "file_search"}],
        "local_files": [],
            "aliases": ["agent iwant", "agent_iwant_gpt"],
            "keywords": [],
        }
    }

_initialize_vector_stores(CONSULTANTS)


DEFAULT_CONSULTANT_KEY = os.getenv("DEFAULT_CONSULTANT_KEY") or next(iter(CONSULTANTS.keys()))


def list_consultants() -> List[Dict[str, Any]]:
    """Return lightweight consultant metadata for UI consumption."""
    items: List[Dict[str, Any]] = []
    for meta in CONSULTANTS.values():
        items.append(
            {
                "key": meta["key"],
                "display_name": meta.get("display_name", meta["key"]),
                "aliases": meta.get("aliases", []),
                "keywords": meta.get("keywords", []),
                "local_files": meta.get("local_files", []),
                "vector_store_id": meta.get("vector_store_id"),
                "summary": meta.get("summary"),
                "conversation_starters": meta.get("conversation_starters", []),
            }
        )
    return items


def get_consultant_overview() -> str:
    """Return the contents of consultants/overview.md if present."""
    if OVERVIEW_PATH.exists():
        try:
            return OVERVIEW_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


def run_consultant_response(
    user_text: str,
    vector_store_id: Optional[str] = None,
    container_id: Optional[str] = None,
    consultant_key: str = DEFAULT_CONSULTANT_KEY,
):
    if consultant_key not in CONSULTANTS:
        raise ValueError(f"Unknown consultant key: {consultant_key}")

    meta = CONSULTANTS[consultant_key]
    models = meta.get("model_try") or DEFAULT_MODELS
    base_tools = list(meta.get("tools", []) or [])

    request_tools = []
    for tool in base_tools:
        t = dict(tool)
        tool_type = t.get("type")
        if tool_type == "file_search":
            ids = list(t.get("vector_store_ids") or [])
            consultant_vs = meta.get("vector_store_id")
            if consultant_vs and consultant_vs not in ids:
                ids.append(consultant_vs)
            if vector_store_id and vector_store_id not in ids:
                ids.append(vector_store_id)
            if not ids:
                continue
            t["vector_store_ids"] = ids
        if tool_type == "code_interpreter":
            if container_id:
                t["container"] = container_id
            else:
                t["container"] = {"type": "auto"}
        request_tools.append(t)

    last_err: Optional[Exception] = None
    for model in models:
        try:
            print(
                f"[consultant:{consultant_key}] calling Responses API with model {model} and {len(request_tools)} tools…"
            )
            resp = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": meta["instructions"]},
                    {"role": "user", "content": user_text},
                ],
                tools=request_tools or None,
            )
            serialized = _response_to_dict(resp)
            text_out = getattr(resp, "output_text", "") or ""
            file_ids = getattr(resp, "output_file_ids", None) or []
            print(f"[consultant:{consultant_key}] received response successfully.")
            return {
                "model": model,
                "text": text_out,
                "file_ids": file_ids,
                "consultant": consultant_key,
                "display_name": meta.get("display_name", consultant_key),
                "local_files": meta.get("local_files", []),
                "vector_store_id": meta.get("vector_store_id"),
                "response_payload": serialized,
            }
        except Exception as exc:  # pragma: no cover - diagnostic logging
            print(f"[consultant:{consultant_key}] model {model} call failed: {exc}")
            last_err = exc
            continue

    if last_err:
        raise last_err
    raise RuntimeError("Consultant execution failed without exception context")
