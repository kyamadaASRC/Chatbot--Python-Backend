import io
import os
import uuid
from pathlib import Path
from typing import Optional
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import shutil

from app.consultants import (
    run_consultant_response,
    DEFAULT_CONSULTANT_KEY,
    CONSULTANTS,
    list_consultants,
    get_consultant_overview,
)
from app.openai_client import client


CONTAINER_ROOT = Path(os.environ.get("CONTAINER_STORAGE", "./artifacts/containers"))
CONTAINER_ROOT.mkdir(parents=True, exist_ok=True)
CONTAINERS = {}
CONTAINER_FILES = {}

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-5")
GENERAL_CHAT_SYSTEM = """You are a helpful assistant. Keep answers concise unless the user asks for more detail.
When the user requests a document, report, or formatted output, call generate_pdf(markdown_text=your response in raw Markdown).
Respond using Markdown syntax for code and always wrap code in fenced blocks (```), leaving a blank line before and after each block.
Otherwise, reply normally in raw Markdown."""
CONSULTANT_TOOL_PREFIX = "call_"



def _serialize(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)  # enable CORS for all routes

    @app.route("/")
    def index():
        return render_template("chatbot.html")


    def _needs_any_consultant(message: str) -> bool:
        msg = (message or "").lower()
        if "consultant" in msg:
            return True
        matches = 0
        for meta in CONSULTANTS.values():
            for keyword in meta.get("keywords", []):
                if keyword and keyword in msg:
                    matches += 1
                    if matches >= 2:
                        return True
        return False


    def _match_consultant_alias(message: str) -> Optional[str]:
        msg = (message or "").lower()
        for key, meta in CONSULTANTS.items():
            for alias in meta.get("aliases", []):
                if alias and alias in msg:
                    return key
            for keyword in meta.get("keywords", []):
                if keyword and keyword in msg:
                    return key
        return None


    def _select_consultant(message: str, explicit_key: Optional[str] = None) -> Optional[str]:
        if explicit_key:
            return explicit_key if explicit_key in CONSULTANTS else None
        matched = _match_consultant_alias(message)
        if matched:
            return matched
        if _needs_any_consultant(message):
            return DEFAULT_CONSULTANT_KEY
        return None


    def _extract_text(resp) -> str:
        text = getattr(resp, "output_text", None)
        if isinstance(text, list):
            text = text[0] if text else ""
        if isinstance(text, str) and text.strip():
            return text.strip()
        for output in getattr(resp, "output", []) or []:
            if isinstance(output, dict):
                contents = output.get("content")
                if isinstance(contents, list):
                    for c in contents:
                        val = c.get("text") if isinstance(c, dict) else None
                        if isinstance(val, str) and val.strip():
                            return val.strip()
                txt = output.get("text")
                if isinstance(txt, str) and txt.strip():
                    return txt.strip()
        choices = getattr(resp, "choices", None)
        if choices:
            msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            val = msg.get("content")
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""


    def _consultant_tool_call(
        message: str,
        project: str,
        vector_store_id: Optional[str],
        container_id: Optional[str],
        consultant_key: str,
    ):
        result = run_consultant_response(
            message,
            vector_store_id=vector_store_id,
            container_id=container_id,
            consultant_key=consultant_key,
        )
        text = (result.get("text") or "").strip()
        if not text:
            text = "The consultant returned no notes."
        file_ids = result.get("file_ids") or []
        meta = CONSULTANTS.get(consultant_key, {})
        tool_args = {
            "question": message,
            "project": project,
            "vector_store_id": vector_store_id,
            "container_id": container_id,
            "consultant_key": consultant_key,
        }
        tool_name = f"{CONSULTANT_TOOL_PREFIX}{consultant_key}"
        call_stub = {
            "type": "function_call",
            "name": tool_name,
            "arguments": tool_args,
        }
        response_stub = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        }
        return {
            "text": text,
            "mode": "consultant",
            "tool_name": tool_name,
            "consultant": result.get("consultant", consultant_key),
            "consultant_display": result.get("display_name") or meta.get("display_name"),
            "model": result.get("model"),
            "file_ids": file_ids,
            "resources": meta.get("local_files", []),
            "output": [call_stub, response_stub],
        }

    @app.route("/chat", methods=["POST"])
    def chat():
        data = request.get_json(force=True)
        msg = data.get("message", "").strip()
        project = data.get("project", "demo-project")
        vector_store_id = data.get("vector_store_id")
        container_id = data.get("container_id")
        requested_consultant = data.get("consultant_key")
        if not msg:
            return jsonify({"error": "Missing message"}), 400

        consultant_key = _select_consultant(msg, requested_consultant)
        if requested_consultant and not consultant_key:
            return jsonify({"error": f"Unknown consultant '{requested_consultant}'"}), 400

        if consultant_key:
            try:
                payload = _consultant_tool_call(
                    msg,
                    project,
                    vector_store_id,
                    container_id,
                    consultant_key,
                )
                return jsonify(payload)
            except Exception as exc:
                return jsonify({"error": str(exc)}), 500

        try:
            resp = client.responses.create(
                model=CHAT_MODEL,
                input=[
                    {"role": "system", "content": GENERAL_CHAT_SYSTEM},
                    {"role": "user", "content": msg},
                ],
            )
            text = _extract_text(resp) or "I wasn't able to produce a response."
            return jsonify({"text": text, "mode": "direct"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/consultants", methods=["GET"])
    def list_consultants_route():
        return jsonify({
            "consultants": list_consultants(),
            "overview": get_consultant_overview(),
        })

    @app.route("/v1/vector_stores", methods=["POST"])
    def create_vector_store():
        payload = request.get_json(force=True) or {}
        name = payload.get("name", "Session Vector Store")
        try:
            vs = client.vector_stores.create(name=name)
            return jsonify(_serialize(vs))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/vector_stores/<vector_store_id>", methods=["DELETE"])
    def delete_vector_store(vector_store_id):
        try:
            res = client.vector_stores.delete(vector_store_id)
            data = _serialize(res) if res else {"id": vector_store_id, "deleted": True}
            return jsonify(data)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/vector_stores/<vector_store_id>/files", methods=["POST"])
    def link_file_to_vector_store(vector_store_id):
        payload = request.get_json(force=True) or {}
        file_id = payload.get("file_id")
        if not file_id:
            return jsonify({"error": "file_id is required"}), 400
        try:
            res = client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=file_id)
            return jsonify(_serialize(res))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/files", methods=["GET"])
    def list_files():
        try:
            files = client.files.list()
            return jsonify(_serialize(files))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/files", methods=["POST"])
    def upload_file():
        if "file" not in request.files:
            return jsonify({"error": "file is required"}), 400
        file = request.files["file"]
        purpose = request.form.get("purpose", "assistants")
        try:
            uploaded = client.files.create(
                file=(file.filename, file.stream, file.mimetype or "application/octet-stream"),
                purpose=purpose,
            )
            return jsonify(_serialize(uploaded))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/files/<file_id>", methods=["DELETE"])
    def delete_file(file_id):
        try:
            res = client.files.delete(file_id)
            return jsonify(_serialize(res))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/files/<file_id>/content", methods=["GET"])
    def get_file_content(file_id):
        try:
            content = client.files.content(file_id)
            if hasattr(content, "read"):
                data = content.read()
            else:
                data = content
            return send_file(
                io.BytesIO(data),
                download_name=f"{file_id}.bin",
                mimetype="application/octet-stream",
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/containers", methods=["POST"])
    def create_container_runtime():
        container_id = str(uuid.uuid4())
        path = CONTAINER_ROOT / container_id
        path.mkdir(parents=True, exist_ok=True)
        CONTAINERS[container_id] = {"id": container_id}
        return jsonify({"id": container_id})

    @app.route("/v1/containers/<container_id>", methods=["DELETE"])
    def delete_container_runtime(container_id):
        CONTAINERS.pop(container_id, None)
        CONTAINER_FILES.pop(container_id, None)
        path = CONTAINER_ROOT / container_id
        shutil.rmtree(path, ignore_errors=True)
        return jsonify({"id": container_id, "deleted": True})

    @app.route("/v1/containers/<container_id>/files", methods=["POST"])
    def upload_container_file(container_id):
        if "file" not in request.files:
            return jsonify({"error": "file is required"}), 400
        path = CONTAINER_ROOT / container_id
        if not path.exists():
            return jsonify({"error": "container not found"}), 404
        file = request.files["file"]
        file_id = str(uuid.uuid4())
        safe_name = file.filename or "upload.bin"
        dest = path / f"{file_id}_{safe_name}"
        file.save(dest)
        files_map = CONTAINER_FILES.setdefault(container_id, {})
        files_map[file_id] = str(dest)
        return jsonify({"id": file_id, "name": safe_name})

    @app.route("/v1/containers/<container_id>/files/<file_id>", methods=["DELETE"])
    def delete_container_file(container_id, file_id):
        files_map = CONTAINER_FILES.get(container_id, {})
        file_path = files_map.pop(file_id, None)
        if file_path:
            try:
                Path(file_path).unlink(missing_ok=True)
            except OSError:
                pass
        return jsonify({"id": file_id, "deleted": True})

    @app.route("/v1/responses", methods=["POST"])
    def create_response():
        payload = request.get_json(force=True) or {}
        try:
            resp = client.responses.create(**payload)
            return jsonify(_serialize(resp))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    return app


app = create_app()
CORS(app, resources={r"/*": {"origins": ["http://127.0.0.1:5501"]}})



if __name__ == "__main__":
    app.run(port=5001, debug=True)
