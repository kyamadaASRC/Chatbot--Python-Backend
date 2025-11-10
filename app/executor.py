from typing import Optional

from app.artifacts import save_hardcopies
from app.consultants import run_consultant_response

DEFAULT_AGENT_KEY = "agent_iwant_gpt"


def execute_plan(
    project: str,
    plan_batches,
    user_message: str,
    vector_store_id: Optional[str] = None,
    container_id: Optional[str] = None,
):
    """Run every step through the single iWant agent consultant for now."""
    notes = {}
    warnings = []
    step_counter = 0

    for batch in plan_batches:
        for step in batch:
            step_counter += 1
            result = run_consultant_response(
                user_message,
                vector_store_id=vector_store_id,
                container_id=container_id,
            )
            text = result.get("text", "")
            file_ids = result.get("file_ids", [])
            save_hardcopies(project, DEFAULT_AGENT_KEY, step_counter, file_ids)

            step_name = step.get("cap") or f"step{step_counter}"
            notes[f"{DEFAULT_AGENT_KEY}:{step_name}"] = text

    return {"notes": notes, "warnings": warnings}
