import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import app.app as app_module


class DummyResponse:
    """Minimal stand-in for OpenAI Responses output during tests."""

    def __init__(self, output):
        self._output = output
        self.output_text = []
        self.output_file_ids = []
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
