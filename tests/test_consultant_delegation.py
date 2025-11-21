import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import app.app as app_module


class DummyResponse:
    """Minimal stand-in for OpenAI Responses output during tests."""

    def __init__(self, output, output_text=None, output_file_ids=None):
        self._output = output
        self.output_text = output_text or []
        self.output_file_ids = output_file_ids or []
        self.choices = []

    @property
    def output(self):
        return self._output

    def model_dump(self):
        # `app.app._ensure_dict` calls `.model_dump()` when present.
        return {"output": self._output, "output_text": self.output_text}


@pytest.fixture
def flask_client():
    app = app_module.create_app()
    app.config.update(TESTING=True)
    with app.test_client() as client:
        yield client


def test_chat_delegates_when_general_model_calls_consultant(monkeypatch, flask_client):
    target_key = next(iter(app_module.CONSULTANTS.keys()))

    def fake_run_consultant_response(*_, **__):
        return {
            "text": "delegated",
            "model": "stub-model",
            "file_ids": [],
            "consultant": target_key,
            "display_name": "Stub Consultant",
            "local_files": [],
            "vector_store_id": None,
            "response_payload": {"output": []},
        }

    monkeypatch.setattr(app_module, "run_consultant_response", fake_run_consultant_response)
    monkeypatch.setattr(app_module, "_link_files_to_vector_store", lambda *_, **__: [])
    monkeypatch.setattr(app_module, "_process_server_tool_calls", lambda *_, **__: [])

    dummy_output = [
        {
            "type": "function_call",
            "name": f"{app_module.CONSULTANT_TOOL_PREFIX}{target_key}",
            "arguments": {"question": "Handle this"},
        }
    ]

    monkeypatch.setattr(
        app_module.client.responses,
        "create",
        lambda **_: DummyResponse(dummy_output),
    )

    response = flask_client.post(
        "/chat",
        json={
            "message": "Need consultant help",
            "router_decision": {"mode": "direct"},
            "tools": [],
            "history": [],
        },
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["triggered_by_general_model"] is True
    assert data["triggered_tool"] == f"{app_module.CONSULTANT_TOOL_PREFIX}{target_key}"
    assert data["consultant"] == target_key
    stages = [entry["stage"] for entry in data["progress_log"]]
    assert "consultant_tool_delegate" in stages


def test_parallel_mode_summarizes_multiple_consultants(monkeypatch, flask_client):
    # Fabricate two consultant keys for the parallel flow.
    keys = list(app_module.CONSULTANTS.keys())[:2]
    if len(keys) < 2:
        pytest.skip("Need at least two consultants to exercise parallel mode")

    # Stub router to force parallel mode with our two consultants.
    monkeypatch.setattr(
        app_module,
        "route_consultants",
        lambda *_, **__: app_module.RouterDecision(
            mode="parallel", primary=keys[0], secondaries=[keys[1]], reason="test"
        ),
    )

    # Each consultant returns a tiny note.
    def fake_run_consultant_response(*_, consultant_key, **__):
        return {
            "model": "stub-model",
            "text": f"note from {consultant_key}",
            "file_ids": [],
            "consultant": consultant_key,
            "display_name": consultant_key,
            "local_files": [],
            "vector_store_id": None,
            "response_payload": {"output": []},
        }

    monkeypatch.setattr(app_module, "run_consultant_response", fake_run_consultant_response)
    # Stub summary model call.
    monkeypatch.setattr(
        app_module.client.responses,
        "create",
        lambda **_: DummyResponse([], output_text=["summary"]),
    )
    response = flask_client.post(
        "/chat",
        json={
            "message": "force parallel",
            "router_decision": {"mode": "parallel", "primary": keys[0], "secondaries": [keys[1]]},
            "tools": [],
            "history": [],
        },
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["mode"] == "parallel"
    assert data["consultants"][0]["consultant"] == keys[0]
    assert data["consultants"][1]["consultant"] == keys[1]
    stages = [entry["stage"] for entry in data["progress_log"]]
    assert "summary" in stages


def test_force_direct_logs_progress(monkeypatch, flask_client):
    # Ensure _should_force_direct returns True for this input.
    payload = {
        "message": "please generate pdf of this",
        "tools": [],
        "history": [],
    }

    # Stub Responses call so we don't hit the network.
    monkeypatch.setattr(
        app_module.client.responses,
        "create",
        lambda **_: DummyResponse([]),
    )

    response = flask_client.post("/chat", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    stages = [entry["stage"] for entry in data["progress_log"]]
    assert "forced_direct" in stages
    assert data["mode"] == "direct"
