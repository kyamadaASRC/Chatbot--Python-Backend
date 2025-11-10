import os
import json

from app.openai_client import client


ROUTER_PROMPT = "You are a planner. Return a JSON {steps:[{cap,needs?,artifacts?}]}. Keep it short and structured."

# Default to gpt-5 if available; override with RESPONSES_MODEL
MODEL = os.getenv("RESPONSES_MODEL", "gpt-5")


SCHEMA = {
"type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cap": {"type": "string"},
                    "needs": {"type": "array", "items": {"type": "string"}},
                    "artifacts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "types": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
                "required": ["cap"],
            },
        }
    },
    "required": ["steps"],
}


def _parse_plan_from_response(resp):
    # 1) New SDK structured output field
    try:
        parsed = getattr(resp, "output_parsed", None)
        if parsed:
            return parsed
    except Exception:
        pass
    # 2) Plain text JSON
    text = (getattr(resp, "output_text", None) or "").strip()
    if text:
        # strip fences if present
        if text.startswith("```"):
            lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
            text = "\n".join(lines)
        try:
            return json.loads(text)
        except Exception:
            pass
    # 3) Last resort: scan generic outputs
    for o in getattr(resp, "output", []) or []:
        if getattr(o, "type", None) == "message" and hasattr(o, "content"):
            for c in o.content:
                t = getattr(c, "text", None)
                if isinstance(t, str) and t.strip():
                    try:
                        return json.loads(t)
                    except Exception:
                        continue
    return None


def route_capabilities(user_message: str):
    base_msgs = [
        {"role": "system", "content": ROUTER_PROMPT},
        {"role": "user", "content": user_message},
    ]

    # Prefer structured outputs; fall back for SDKs that don't accept response_format
    try:
        print('[router] calling client.responses.create for planning…')
        resp = client.responses.create(
            model=MODEL,
            input=base_msgs,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "capability_plan",
                    "schema": SCHEMA,
                    "strict": True,
                },
            },
        )
    except TypeError:
        schema_hint = json.dumps(SCHEMA)
        print('[router] falling back to strict prompt planning call…')
        strict_prompt = (
            "Return ONLY a valid JSON object matching this JSON Schema. "
            "No explanation, no code fences. Schema: " + schema_hint
        )
        resp = client.responses.create(
            model=MODEL,
            input=[
                {"role": "system", "content": ROUTER_PROMPT},
                {"role": "system", "content": strict_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        print('[router] received fallback planning response.')

    plan_obj = _parse_plan_from_response(resp)
    if not plan_obj or "steps" not in plan_obj:
        raise ValueError("Failed to parse planning steps from model output.")
    return plan_obj["steps"]
