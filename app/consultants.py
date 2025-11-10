# consultants.py (testing version)

from pathlib import Path
from typing import Optional

from app.openai_client import client

DEFAULT_MODELS = ["gpt-5", "gpt-4.1"]

BASE_DIR = Path(__file__).resolve().parent
CONSULTANT_DIR = BASE_DIR / "consultants" / "Agent_iWant_GPT"


def _instruction_path() -> Optional[Path]:
    if not CONSULTANT_DIR.exists():
        return None
    for name in ("instruction.txt", "Instruction.txt", "Instructions.txt"):
        candidate = CONSULTANT_DIR / name
        if candidate.exists():
            return candidate
    return None


instr_path = _instruction_path()
if instr_path:
    instructions = instr_path.read_text(encoding="utf-8").strip()
else:
    instructions = "You are the Agent_iWant_GPT assistant."


if CONSULTANT_DIR.exists():
    local_files = [
        str(f)
        for f in CONSULTANT_DIR.iterdir()
        if f.is_file() and f.name.lower() not in {"instruction.txt", "instructions.txt"}
    ]
else:
    local_files = []


def _is_openai_container(container_id: Optional[str]) -> bool:
    return isinstance(container_id, str) and container_id.startswith("cntr")


CONSULTANTS = {
    "agent_iwant_gpt": {
        "model_try": DEFAULT_MODELS,
        "tools": [{"type": "file_search"}],
        "instructions": instructions,
        "local_files": local_files,
        "rubric_files": [],
        "default_files": [],
    }
}


def run_consultant_response(
    user_text: str, vector_store_id: Optional[str] = None, container_id: Optional[str] = None
):
    """Run the single test consultant on user_text."""
    meta = CONSULTANTS["agent_iwant_gpt"]
    models = meta["model_try"]
    last_err = None

    base_tools = list(meta.get("tools", []) or [])
    request_tools = []
    for tool in base_tools:
        t = dict(tool)
        tool_type = t.get("type")
        if tool_type == "file_search":
            if vector_store_id:
                t["vector_store_ids"] = t.get("vector_store_ids") or [vector_store_id]
            else:
                continue
        if tool_type == "code_interpreter":
            continue
        request_tools.append(t)

    for model in models:
        try:
            print(f'[consultant] calling Responses API with model {model} and {len(request_tools)} tools…')
            extra_kwargs = {"tools": request_tools}

            resp = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": meta["instructions"]},
                    {"role": "user", "content": user_text},
                ],
                **extra_kwargs,
            )

            text_out = getattr(resp, "output_text", "") or ""
            print('[consultant] received response successfully.')
            file_ids = getattr(resp, "output_file_ids", None) or []
            return {"model": model, "text": text_out, "file_ids": file_ids}
        except Exception as e:
            print(f'[consultant] model {model} call failed: {e}')
            last_err = e
            continue

    raise last_err
