import io
import os
import uuid
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import shutil

from app.executor import execute_plan
from app.planner import plan_pipeline
from app.router import route_capabilities
from app.openai_client import client


CONTAINER_ROOT = Path(os.environ.get("CONTAINER_STORAGE", "./artifacts/containers"))
CONTAINER_ROOT.mkdir(parents=True, exist_ok=True)
CONTAINERS = {}
CONTAINER_FILES = {}


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


    @app.route("/chat", methods=["POST"])
    def chat():
        data = request.get_json(force=True)
        msg = data.get("message", "").strip()
        project = data.get("project", "demo-project")
        vector_store_id = data.get("vector_store_id")
        container_id = data.get("container_id")
        if not msg:
            return jsonify({"error": "Missing message"}), 400

        draft = route_capabilities(msg)
        batches = plan_pipeline(draft)
        out = execute_plan(
            project,
            batches,
            msg,
            vector_store_id=vector_store_id,
            container_id=container_id,
        )

        return jsonify({
            "draft": draft,
            "batches": batches,
            "consultant_notes": out["notes"],
            "warnings": out["warnings"],
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
