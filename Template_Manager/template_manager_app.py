"""Standalone Flask app for managing template manifests."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from Template_Manager.openai_client import client
from Template_Manager.template_manifest import (
    ensure_template_vector_store,
    ensure_manifest_vector_store,
    load_manifest,
    add_templates,
    delete_template,
)


def _resolve_vs_name(vs_id: str | None) -> str | None:
    if not vs_id:
        return None
    try:
        vs = client.vector_stores.retrieve(vs_id)
        return getattr(vs, "name", None) or (vs.get("name") if isinstance(vs, dict) else None)
    except Exception:
        return None


def create_app() -> Flask:
    base_dir = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        static_folder=str(base_dir / "static"),
        template_folder=str(base_dir / "templates"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # allow up to 64MB per request
    CORS(app)

    @app.route("/")
    def index():
        return render_template("template_manager.html")

    @app.route("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.route("/v1/templates/manifest", methods=["GET"])
    def get_template_manifest():
        vector_store_id = request.args.get("vector_store_id")
        try:
            manifest, manifest_file_id, template_vs_id = load_manifest(vector_store_id)
            manifest_vs_id = ensure_manifest_vector_store(None)
            manifest_vs_name = _resolve_vs_name(manifest_vs_id)
            return jsonify(
                {
                    "manifest": manifest,
                    "manifest_file_id": manifest_file_id,
                    "manifest_vector_store_id": manifest_vs_id,
                    "manifest_vector_store_name": manifest_vs_name,
                }
            )
        except Exception as exc:
            print(f"[template-manager] manifest error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/templates/upload", methods=["POST"])
    def upload_templates():
        vector_store_id = request.form.get("vector_store_id")
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "No files uploaded."}), 400
        try:
            result = add_templates(files, vector_store_id=vector_store_id)
            return jsonify(result)
        except Exception as exc:
            print(f"[template-manager] upload error: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/v1/templates/<file_name>", methods=["DELETE"])
    def delete_template_entry(file_name):
        vector_store_id = request.args.get("vector_store_id")
        if not file_name:
            return jsonify({"error": "file_name is required"}), 400
        try:
            result = delete_template(file_name, vector_store_id=vector_store_id)
            return jsonify(result)
        except Exception as exc:
            print(f"[template-manager] delete error: {exc}")
            return jsonify({"error": str(exc)}), 500

    return app


app = create_app()


if __name__ == "__main__":
    app.run(port=5002, debug=True)
