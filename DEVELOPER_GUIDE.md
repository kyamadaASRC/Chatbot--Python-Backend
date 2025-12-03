# Chatbot-Python Backend — Developer Guide (Slim Edition)

This repo now focuses on a single assistant path with template selection/editing. Consultant routing/delegation has been removed; the only tools are file search, code interpreter, doc generators, and `select_and_edit_docx`.

## 1. Architecture
- **Backend** (`app/app.py`): Flask API that proxies chat to OpenAI Responses. Direct path only; no router/consultant orchestration. Tool helpers handle DOCX/XLSX/PDF generation and template selection/editing.
- **Template Manager** (`Template_Manager/`): Standalone Flask app + UI to upload DOCX templates, build a manifest JSON, and attach it to a manifest vector store. The manifest cache is stored locally in `Template_Manager/manifest_cache.json`.
- **Template selection/editing** (`app/Select_Edit_Docx.py`): Uses the manifest vector store to pick the best template (`file_search` over manifest `file_info`), edits it via code interpreter, and can convert the filled DOCX to PDF.
- **Frontend** (`app/templates/chatbot.html`, `app/static/js/main.js`, `app/static/modules/*`): Browser UI for chat, sessions, file upload, and file previews. Generated files appear in the Files list; PDFs render inline when available.

### Data flow (chat)
1) Browser sends `/chat` with the user message, optional history, vector store id, container id, and tool specs (includes `select_and_edit_docx`, doc generators, code interpreter, file_search).
2) Flask calls `client.responses.create` with `GENERAL_CHAT_SYSTEM` and the provided tools.
3) `_process_server_tool_calls` executes server-side tool outputs (generate_docx/xlsx, select_and_edit_docx) and links generated files to the session vector store.
4) Response returns `text` plus `generated_files`; the UI renders the assistant text, inline PDF previews, and updates the Files list.

### Data flow (template manager)
1) Upload DOCX files in `/template-manager`. Files attach to the template vector store; manifest entries (file_name, file_id, uploaded_at, file_info) are rebuilt.
2) Manifest JSON is cached locally (`manifest_cache.json`) and attached to the manifest vector store. Due to org policy blocking downloads of `assistants` files, the cache is the source of truth.
3) `select_docx_template` uses the manifest vector store for `file_search` to find the best template.

## 2. Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
Secrets:
- Root `.env`: `OPENAI_API_KEY=...` (used by main app).
- `Template_Manager/.env`: `OPENAI_API_KEY=...` (used by template manager).
- `Template_Manager/manifest_vector_store_id.txt`: manifest vector store id (vs_…).
- `Template_Manager/template_vector_store_id.txt`: template vector store id (vs_…).

## 3. Running
- Main chat app:
  ```bash
  source .venv/bin/activate
  FLASK_APP=app.app flask run --port 5001 --debug
  ```
  Open http://127.0.0.1:5001
- Template manager:
  ```bash
  source .venv/bin/activate
  FLASK_APP=Template_Manager.template_manager_app flask run --port 5002 --debug
  ```
  Open http://127.0.0.1:5002

## 4. Key Modules
- `app/app.py`: Flask routes for chat, vector stores, files, containers. `_fetch_file_metadata` caches generated files under `artifacts/generated_files` and exposes `/local_files/<name>` for preview/download.
- `app/Select_Edit_Docx.py`: `select_docx_template`, `edit_docx_template` (optionally converts to PDF), `select_and_edit_docx`.
- `Template_Manager/template_manifest.py`: Uploads templates, regenerates manifest, caches to `manifest_cache.json`, attaches manifest to manifest vector store.
- `Template_Manager/static/modules/templateManager.js`: Upload UI, search/sort list, logs to console, shows red “×” when `file_info` generation fails.
- `app/static/js/main.js`: Chat orchestration; renders inline PDF preview cards when PDFs are returned in `generated_files`.
- `app/static/modules/fileManager.js`: Files sidebar, download/preview; uses `preview_url` when provided (local cache).

## 5. File handling and previews
- Generated files are uploaded to OpenAI (`purpose=assistants`) but downloads may be blocked by org policy. The backend tries to cache generated files locally and serves them via `/local_files/<name>`.
- PDFs in `generated_files` render inline (iframe) and have “Open in new tab” links.
- Files list uses server-returned filenames/mime to set correct extensions.

## 6. Template selection/editing
- Manifest lives in `Template_Manager/manifest_cache.json` and manifest vector store.
- `select_docx_template` runs `file_search` on the manifest store (`file_info` embeddings) to pick the best template; returns template `file_id`.
- `edit_docx_template` runs code interpreter against that `file_id`, outputs a new DOCX (and PDF if enabled), and returns `output_file_ids` that the UI exposes for download/preview.

## 7. Known limitations
- Org policy blocks downloading `assistants` files directly from OpenAI; rely on local cache (`preview_url`) or switch to storage you control (disk/S3) for guaranteed downloads/previews.
- If `file_info` generation fails, the template manager shows a red “×”; reupload to retry.

## 8. Cleanup checklist (post-consultant removal)
- Consultant router/delegation is removed; only direct chat path remains.
- Consultant folder remains for template assets but is not executed.
- Update any scripts/automation to use `select_and_edit_docx` and direct tools only.
