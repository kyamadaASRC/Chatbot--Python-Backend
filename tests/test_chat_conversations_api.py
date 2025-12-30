import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import app.app as app_module


class DummyResponse:
    """Minimal stand-in for OpenAI Responses output during tests."""

    def __init__(self, output_text=None):
        self.output = []
        self.output_text = output_text or ["ok"]
        self.output_file_ids = []
        self.choices = []

    def model_dump(self):
        return {"output": self.output, "output_text": self.output_text}


@pytest.fixture(autouse=True)
def patch_network(monkeypatch):
    """Patch networked calls so tests stay offline."""

    class DummyConversations:
        def create(self, **_):
            return {"id": "c1"}

    class DummyResponses:
        def create(self, **_):
            return DummyResponse()

    monkeypatch.setattr(app_module.client, "conversations", DummyConversations())
    monkeypatch.setattr(app_module.client, "responses", DummyResponses())
    monkeypatch.setattr(app_module, "_process_server_tool_calls", lambda *_, **__: {})
    monkeypatch.setattr(app_module, "_link_files_to_vector_store", lambda *_, **__: [])
    monkeypatch.setattr(app_module, "_persist_container_file", lambda *_, **__: None)
    yield


@pytest.fixture
def flask_client():
    app = app_module.create_app()
    app.config.update(TESTING=True)
    with app.test_client() as client:
        yield client


def test_chat_uses_conversations_api(monkeypatch, flask_client):
    payload = {"message": "hello", "tools": [], "history": []}
    response = flask_client.post("/chat", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["conversation_id"] == "c1"
    assert data["conversation_mode"] == "conversations_api"
    stages = [entry["stage"] for entry in data.get("progress_log", [])]
    assert "conversation_api" in stages


def test_chat_falls_back_when_conversation_create_fails(monkeypatch, flask_client):
    class FailingConversations:
        def create(self, **_):
            raise RuntimeError("nope")

    # Override autouse stubs for this test only.
    monkeypatch.setattr(app_module.client, "conversations", FailingConversations())
    monkeypatch.setattr(
        app_module.client,
        "responses",
        type(
            "DummyResponses",
            (),
            {"create": lambda *_, **__: DummyResponse(output_text=["fallback ok"])},
        )(),
    )

    payload = {"message": "hello", "tools": [], "history": []}
    response = flask_client.post("/chat", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["conversation_mode"] == "legacy_history"
    assert data.get("conversation_id") is None
    stages = [entry["stage"] for entry in data.get("progress_log", [])]
    assert "conversation_legacy_fallback" in stages
