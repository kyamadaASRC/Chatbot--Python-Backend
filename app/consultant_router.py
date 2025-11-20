"""LLM-backed router that selects the best consultant(s) for an incoming request."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Literal

from app.consultants import CONSULTANTS, DEFAULT_CONSULTANT_KEY
from app.openai_client import client

RouterMode = Literal["direct", "single", "parallel"]

ROUTER_MODEL = os.getenv("ROUTER_MODEL") or os.getenv("CHAT_MODEL", "gpt-5")
ROUTER_SYSTEM_PROMPT = """You route acquisition-related questions to specialized virtual consultants.
Input payloads contain:
- `question`: the latest user request,
- `history`: prior user/assistant turns in chronological order (oldest first) so you can see context and what has already been delivered,
- `consultants`: metadata objects with `key`, `display_name`, `summary`, `keywords`, and `aliases`.
Use both the latest question and the recent history to infer what help the user needs right now (e.g., if they just received an Acquisition Plan, the next request may be for an IGCE).
Decide whether to:
- return "single" when one consultant clearly owns the request,
- return "parallel" when the user needs multiple specialties (up to 3 consultants),
- return "direct" when no consultant fits and the general model should answer instead.

Always output STRICT JSON with the shape:
{
  "mode": "direct|single|parallel",
  "primary": "<consultant key or null>",
  "secondaries": ["<key>", ...],
  "reason": "short explanation",
  "summary_prompt": "How to synthesize multi-consultant answers (optional)"
}

When choosing "parallel", include at least two consultant keys and a short prompt that explains
how to merge the results into one summary. Only return "direct" when none of the consultants clearly cover the request or the history. Never hallucinate consultant keys."""


def _build_catalog() -> List[Dict[str, Any]]:
    """Capture lightweight consultant descriptors for router prompts."""
    catalog: List[Dict[str, Any]] = []
    for meta in CONSULTANTS.values():
        catalog.append(
            {
                "key": meta["key"],
                "display_name": meta.get("display_name", meta["key"]),
                "summary": meta.get("summary") or "",
                "keywords": meta.get("keywords", []),
                "aliases": meta.get("aliases", []),
            }
        )
    return catalog


# Cache the catalog when the module loads so each router call only sends a lightweight payload.
ROUTER_CATALOG = _build_catalog()


def _normalize_history(history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """Trim chat history to the last few utterances so the router stays cheap."""
    if not history:
        return []
    trimmed: List[Dict[str, str]] = []
    for entry in history[-6:]:
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, str):
            text = content.strip()
        else:
            text = ""
        if not text:
            continue
        trimmed.append({"role": role, "content": text[:500]})
    return trimmed


def _extract_text(resp: Any) -> str:
    """Best-effort extraction from Responses outputs (mirrors app/app.py helper)."""
    text = getattr(resp, "output_text", None)
    if isinstance(text, list):
        text = text[0] if text else ""
    if isinstance(text, str) and text.strip():
        return text.strip()
    outputs = getattr(resp, "output", None) or []
    for item in outputs:
        if isinstance(item, dict):
            contents = item.get("content")
            if isinstance(contents, list):
                for content in contents:
                    val = content.get("text") if isinstance(content, dict) else None
                    if isinstance(val, str) and val.strip():
                        return val.strip()
            txt = item.get("text")
            if isinstance(txt, str) and txt.strip():
                return txt.strip()
    return ""


def _fallback_decision(message: str) -> "RouterDecision":
    """Legacy keyword matcher used when router calls fail."""
    lowered = (message or "").lower()
    best_key: Optional[str] = None
    best_score = 0.0
    for key, meta in CONSULTANTS.items():
        score = 0.0
        for alias in meta.get("aliases", []):
            alias = (alias or "").lower()
            if alias and alias in lowered:
                score += 2.0
        for kw in meta.get("keywords", []):
            kw = (kw or "").lower()
            if kw and kw in lowered:
                score += 1.0
        if score > best_score:
            best_key = key
            best_score = score
    if best_key:
        return RouterDecision(mode="single", primary=best_key, reason="keyword fallback")
    return RouterDecision(mode="direct", reason="no match fallback")


def _sanitize_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = value.strip().lower()
    return key if key in CONSULTANTS else None


@dataclass
class RouterDecision:
    mode: RouterMode
    primary: Optional[str] = None
    secondaries: List[str] = field(default_factory=list)
    reason: str = ""
    summary_prompt: Optional[str] = None
    raw_text: str = ""
    model: Optional[str] = None
    latency_ms: Optional[int] = None

    def __post_init__(self):
        if self.mode not in ("direct", "single", "parallel"):
            self.mode = "direct"
        self.primary = _sanitize_key(self.primary)
        cleaned = []
        for key in self.secondaries:
            sanitized = _sanitize_key(key)
            if sanitized and sanitized not in cleaned:
                cleaned.append(sanitized)
        self.secondaries = cleaned
        if self.primary and self.primary in self.secondaries:
            self.secondaries.remove(self.primary)

    def selected_consultants(self) -> List[str]:
        picks: List[str] = []
        if self.primary:
            picks.append(self.primary)
        picks.extend(key for key in self.secondaries if key and key not in picks)
        return picks

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        payload = {
            "mode": self.mode,
            "primary": self.primary,
            "secondaries": self.secondaries,
            "reason": self.reason,
            "summary_prompt": self.summary_prompt,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }
        if include_raw and self.raw_text:
            payload["raw_text"] = self.raw_text
        return payload

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["RouterDecision"]:
        if not isinstance(data, dict):
            return None
        mode = data.get("mode") or "direct"
        if mode not in ("direct", "single", "parallel"):
            mode = "direct"
        primary = data.get("primary")
        secondaries = data.get("secondaries") or []
        if not isinstance(secondaries, list):
            secondaries = []
        reason = data.get("reason") or ""
        summary_prompt = data.get("summary_prompt")
        return cls(
            mode=mode,  # type: ignore[arg-type]
            primary=primary,
            secondaries=secondaries,
            reason=reason,
            summary_prompt=summary_prompt,
            raw_text=data.get("raw_text", ""),
            model=data.get("model"),
            latency_ms=data.get("latency_ms"),
        )

    def describe_consultants(self) -> List[Dict[str, str]]:
        details: List[Dict[str, str]] = []
        for key in self.selected_consultants():
            meta = CONSULTANTS.get(key, {})
            details.append({"key": key, "display_name": meta.get("display_name", key)})
        return details


def _parse_router_payload(data: str) -> Optional[Dict[str, Any]]:
    """Handle raw JSON or ```json fenced payloads from the router model."""
    data = data.strip()
    if not data:
        return None
    if data.startswith("```"):
        # handle fenced JSON
        parts = data.strip("`").split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("{"):
                data = candidate
                break
    try:
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return None


def route_consultants(
    message: str,
    history: Optional[List[Dict[str, Any]]] = None,
    max_parallel: int = 3,
) -> RouterDecision:
    """Primary entry point used by backend + UI preview."""
    message = (message or "").strip()
    if not message:
        return RouterDecision(mode="direct", reason="empty request")
    if not ROUTER_MODEL:
        return _fallback_decision(message)

    # The router sees the latest user question, recent context, and a trimmed catalog.
    payload = {
        "question": message,
        "history": _normalize_history(history),
        "consultants": ROUTER_CATALOG,
    }
    start = time.time()
    try:
        resp = client.responses.create(  # type: ignore[call-arg]
            model=ROUTER_MODEL,
            input=[
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
        )
        latency_ms = int((time.time() - start) * 1000)
        text = _extract_text(resp)
        parsed = _parse_router_payload(text) or {}
        decision = RouterDecision(
            mode=parsed.get("mode") or "direct",  # type: ignore[arg-type]
            primary=parsed.get("primary"),
            secondaries=parsed.get("secondaries", []),
            reason=parsed.get("reason") or "",
            summary_prompt=parsed.get("summary_prompt"),
            raw_text=text,
            model=ROUTER_MODEL,
            latency_ms=latency_ms,
        )
        picks = decision.selected_consultants()
        if decision.mode == "single":
            if not picks:
                decision.primary = DEFAULT_CONSULTANT_KEY
        elif decision.mode == "parallel":
            trimmed = picks[:max_parallel]
            if not trimmed:
                decision.mode = "direct"
            else:
                decision.primary = trimmed[0]
                decision.secondaries = trimmed[1:]
        return decision
    except Exception as exc:  # pragma: no cover - diagnostic
        print(f"[router] Failed to route via model: {exc}")
        return _fallback_decision(message)


def describe_decision(
    decision: RouterDecision,
) -> Dict[str, Any]:
    """Return a JSON-safe payload for frontend previews."""
    return {
        "decision": decision.to_dict(include_raw=True),
        "consultants": decision.describe_consultants(),
    }
