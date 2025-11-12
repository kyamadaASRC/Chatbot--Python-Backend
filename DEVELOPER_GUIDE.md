# Chatbot-Python Backend — Developer Guide

This guide orients new contributors to the intern-facing chatbot stack. It explains how the Flask API, consultant registry, and browser UI cooperate so you can add features, debug issues, or onboard new consultants confidently.

---

## 1. Architecture At A Glance

- **Backend** – `app/app.py` exposes a Flask API that proxies all chat traffic to the OpenAI Responses API, optionally routing requests through a “consultant” persona with its own instructions, tools, and resource files (`app/app.py:20-200`).
- **Consultant registry** – `app/consultants.py` discovers consultant directories, uploads their reference files into OpenAI vector stores, and knows how to invoke them on demand (`app/consultants.py:12-335`).
- **Frontend** – `app/templates/chatbot.html` + ES modules under `app/static/` render the chat UI, manage local sessions, create vector stores/containers, and talk to the Flask API.
- **Stateful resources** – Uploaded customer files live both in OpenAI file storage (for retrieval) and in per-session containers under `artifacts/containers/` so code-interpreter runs can access them.

Data flow:

1. User enters a prompt in the browser (`main.js`).
2. The client ensures a chat session, vector store, and optional container exist (`chatSession.js`).
3. `/chat` receives the message, auto-selects an appropriate consultant (if keywords match) or falls back to a general chat model, then calls OpenAI (`app/app.py:163-205`).
4. The browser renders the assistant response, handles tool calls such as `generate_pdf`, and updates session history (`main.js`, `chatClient.js`).

---

## 2. Repository Tour

| Path | Purpose |
| --- | --- |
| `app/app.py` | Flask routes for chat, consultant metadata, vector stores, files, containers, and response proxying. |
| `app/openai_client.py` | Loads `.env`, instantiates the shared `OpenAI` client (`app/openai_client.py:7-36`). |
| `app/consultants.py` | Discovers consultants, uploads their artifacts, and runs consultant-specific responses. |
| `app/consultants/<Consultant>` | Each consultant’s `metadata.json`, `instruction.txt`, and optional resource files. |
| `app/templates/chatbot.html` | Bootstrap-based UI shell plus script tags for the ES modules. |
| `app/static/js/main.js` | Entry point that wires DOM events to the session, chat, and file managers. |
| `app/static/modules/*.js` | Modularized browser logic (`chatClient`, `chatSession`, `fileManager`, `ui`, `utils`). |
| `app/scripts/register_consultant_assests.py` | CLI helper to bulk-upload consultant resources to OpenAI files/vector stores. |
| `artifacts/containers/` | Runtime scratch space for per-session code-interpreter containers (configurable via `CONTAINER_STORAGE`). |
| `requirements.txt` | Locked list of Python packages needed to run the Flask backend. |
| `.env.example` | Template for required environment variables (`OPENAI_API_KEY`, optional overrides). |

---

## 3. Environment Setup

1. **Prerequisites**
   - Python 3.11+ (adjust `pyenv`/homebrew as needed).
   - Node is *not* required; the frontend relies on vanilla JS modules plus CDN libraries.
   - OpenAI API access with the Responses, Files, Vector Stores, and Realtime/Assistants features enabled.

2. **Create a virtual environment**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   ```

3. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   ```
   Add any extras (e.g., `gunicorn`, `watchdog`) per deployment needs.

4. **Configure secrets**
   - Copy `.env.example` to `.env` in the repo root and fill in real values:
     ```
     cp .env.example .env
     ```
   - `app/openai_client.py` automatically walks up the directory tree to find this file.

5. **Verify access**
   ```bash
   source .venv/bin/activate
   python - <<'PY'
   from app.openai_client import client
   print(client.models.list().data[:1])
   PY
   ```

---

## 4. Running Locally

1. **Start the Flask server**
   ```bash
   source .venv/bin/activate
   export FLASK_APP=app.app
   flask run --port 5001 --debug
   # or python app/app.py
   ```
   - Root route `/` renders the chatbot UI (served with Flask templates).
   - CORS is open to `http://127.0.0.1:5501`, so you can also run the HTML via Live Server if desired.

2. **Open the UI**
   - Navigate to `http://127.0.0.1:5001` for the embedded UI, or
   - Serve `app/templates/chatbot.html` through your IDE’s live server (ensure it points to the Flask backend).

3. **Smoke test**
   - Click “New Chat”; watch the dev console for vector store + container creation logs from `chatSession.js`.
   - Send a simple question; confirm `/chat` returns `mode: "direct"` or `mode: "consultant"` depending on keywords.
   - Upload a file and ensure a toast confirms OpenAI + vector store linkage.

---

## 5. Backend Anatomy

### 5.1 Application factory

- `create_app()` wires all routes and helpers (`app/app.py:43-205`).
- `CONTAINER_ROOT` determines where uploaded container files live (`app/app.py:20-24`).
- `CHAT_MODEL` and `GENERAL_CHAT_SYSTEM` define the fallback assistant persona for direct chats (`app/app.py:25-31`).

### 5.2 Chat routing

1. `_select_consultant()` inspects user text + optional `consultant_key` to decide whether to invoke a specialist (`app/app.py:52-87`).
2. If a consultant is chosen, `_consultant_tool_call()` delegates to `run_consultant_response()` and shapes the output so the frontend can render function-call transcripts (`app/app.py:115-161`).
3. Otherwise, `/chat` calls `client.responses.create()` with the general system prompt and returns plain text (`app/app.py:191-200`).

### 5.3 REST surface area

| Endpoint | Description |
| --- | --- |
| `GET /` | Render chatbot UI (`chatbot.html`). |
| `POST /chat` | Main entry point; auto consults specialists or general model. |
| `GET /v1/consultants` | Returns consultant metadata plus the contents of `app/consultants/overview.md` so the UI can summarize available specialists. |
| Vector stores (`POST/DELETE /v1/vector_stores*`) | Create/delete stores via OpenAI SDK. |
| Files (`GET/POST/DELETE /v1/files*`) | Proxy OpenAI file APIs including content download. |
| Containers (`POST/DELETE /v1/containers*` + `/files`) | Manage lightweight “code interpreter” storage on disk. |
| `POST /v1/responses` | Transparent pass-through to OpenAI Responses for UI utilities (e.g., auto session naming). |

### 5.4 OpenAI client bootstrap

- `app/openai_client.py` loads `.env`, warns when keys are missing, and exposes the shared `client` (`app/openai_client.py:7-36`).
- Every backend module imports this singleton, so updating authentication or proxies only needs to happen once here.

### 5.5 Container + file hygiene

- Uploads to `/v1/containers/<id>/files` are stored under `CONTAINER_ROOT` and tracked in `CONTAINER_FILES` for deletion (`app/app.py:214-271`).
- Always pair container/file cleanup with UI teardown to avoid leaked disk usage (see `FileManager.deleteFile()` and `ChatSessionManager.deleteContainer()`).

---

## 6. Consultant Registry & Workflow

### 6.1 Discovery

- `_discover_consultants()` walks `app/consultants/*`, reading `metadata.json`, instructions, and collateral (`app/consultants.py:176-218`).
- Aliases/keywords are lowercased and used both for auto-selection and UI display.

### 6.2 Resource syncing

- `_initialize_vector_stores()` hashes the consultant’s local files, uploads them to OpenAI, and caches the resulting vector store ID in `vector_store_cache.json` (`app/consultants.py:143-174`).
- If you add or edit supporting files, delete the cache entry or bump the file to force a refresh.

### 6.3 Execution

- `run_consultant_response()` merges consultant-defined tools (file search, web search, code interpreter) with the active session’s vector store/container before calling `client.responses.create()` (`app/consultants.py:269-335`).
- Failover is built-in: the method iterates through `model_try` until one succeeds.

### 6.4 Adding a consultant

1. Create a folder under `app/consultants/<your_consultant>`.
2. Populate `metadata.json` (see `Agent_iWant_GPT/metadata.json` for shape) with `key`, `display_name`, optional `model_try`, `tools`, `aliases`, and `keywords`.
3. Author `instruction.txt` (or set `instruction_file` in metadata) with the system prompt.
4. Drop any supplemental files (rubrics, templates, datasets) beside the metadata; they’ll be auto-uploaded into the consultant vector store.
5. Restart the server so `_discover_consultants()` runs.
6. (Optional) Use `python app/scripts/register_consultant_assests.py` if you need to pre-register files manually or keep an offline cache.

---

## 7. Frontend Workflow

### 7.1 UI shell

- `chatbot.html` loads Bootstrap, icons, Markdown/highlight libs, pdfmake/jsPDF, and the module loader for `static/js/main.js`. The template also exposes `window.STATIC_ASSETS` and `window.CHAT_MODEL` for the modules to consume.

### 7.2 Main modules

| Module | Highlights |
| --- | --- |
| `static/js/main.js` | Creates managers, wires DOM events, handles send/stop button state, manages scrolling, and processes tool calls like `generate_pdf`. |
| `static/modules/chatSession.js` | Keeps per-session metadata, persists to `localStorage`, creates vector stores and containers via backend endpoints, and renames sessions using `/v1/responses` (`chatSession.js:75-226`, `chatSession.js:246-374`). |
| `static/modules/chatClient.js` | Builds the payload for `/chat`, ensures vector store/container IDs exist, defines the `generate_pdf`, `web_search_preview`, and `code_interpreter` tools, and records returned messages (`chatClient.js:12-181`). |
| `static/modules/fileManager.js` | Handles drag/drop + input uploads, pushes files to OpenAI, links them to the session’s vector store, mirrors them into containers for analysis, and deletes both OpenAI + container copies (`fileManager.js:1-210`). |
| `static/modules/ui.js` | Renders messages, session list items, spinners, and toast notifications with Markdown sanitization and syntax highlighting. |
| `static/modules/utils.js` | Provides `fetchWithDiagnostics`, Markdown → PDF helpers, and sanitization utilities so tool calls can drop downloadable artifacts into the transcript. |

### 7.3 Message lifecycle

1. User submits a prompt → `main.js` disables inputs, renders the user bubble, and calls `chatClient.sendMessage()`.
2. `ChatClient` fetches `/chat` with the session’s `vector_store_id` + `container_id`.
3. The backend either routes to a consultant or to the general assistant and returns JSON describing the response/tool stream.
4. `main.js` renders the assistant text and inspects `output` for tool calls (e.g., `generate_pdf`), invoking utilities as needed.
5. `ChatSessionManager` updates local history and triggers auto title summarization through `/v1/responses`.

---

## 8. Files, Vector Stores, and Containers

- **Vector stores** – Each session calls `POST /v1/vector_stores` on creation (`chatSession.js:75-99`) so file search has isolated context. Store IDs are cached on the session object and reused for future uploads.
- **File uploads** – `FileManager.uploadFile()` sends files to `/v1/files` → OpenAI file storage, links them back via `/v1/vector_stores/{id}/files`, and keeps DOM metadata (`fileManager.js:29-154`). Vision files optionally trigger caption generation for better retrieval.
- **Containers** – Long-running analysis (CSV, XLSX, etc.) optionally spins up a local container via `POST /v1/containers` and mirrors relevant files there so `code_interpreter` has read/write access (`chatSession.js:150-222`, `fileManager.js:155-210`).
- **Cleanup** – Deleting a chat session should cascade through `ChatSessionManager.deleteVectorStore()` and `deleteContainer()`; deleting an individual file removes it from OpenAI plus the container, then purges DOM metadata (`fileManager.js:212-302`).

---

## 9. Operational Tips & Troubleshooting

- **Missing API key** – Backend will log “⚠️ WARNING: OPENAI_API_KEY not found” on startup (`app/openai_client.py:25-36`). Ensure `.env` is accessible from the working directory.
- **Vector store churn** – If consultant resources change frequently, clear `app/consultants/vector_store_cache.json` to force re-upload on next boot.
- **File preview issues** – Most previews rely on `URL.createObjectURL`. If you refresh the page, revoke stale blob URLs or re-upload.
- **Container cleanup** – Stuck files under `artifacts/containers/*` mean containers were never deleted. Use `DELETE /v1/containers/<id>` or remove the directory manually once you confirm nothing relies on it.
- **Cross-origin requests** – When serving the UI externally (e.g., VS Code Live Server), keep it on `127.0.0.1:5501` or update the `CORS` config near the bottom of `app/app.py`.
- **PDF generation errors** – `renderMarkdownPDFDownload()` depends on pdfmake/html-to-pdfmake/jsPDF scripts loaded in the template. If those CDNs fail, the fallback will still attempt jsPDF but logs warnings in the console (`static/modules/utils.js:34-163`).

---

## 10. Extending the Project

1. **Add tools or capabilities**
   - Expand the `tools` array in `chatClient.js` or per-consultant metadata to expose new OpenAI tool types (e.g., function calls hitting your own microservices).
   - Mirror the tool behavior in the frontend if it needs local handling (like `generate_pdf`).

2. **New consultant personas**
   - Follow the workflow in §6.4, then update `CONSULTANT_TOOL_PREFIX`-driven handlers as needed if you add custom output schemas.

3. **UI Enhancements**
   - `main.js` is intentionally modular; add new panels or status chips by extending the DOM helpers in `ui.js`.

4. **Deployment**
   - Wrap the app with Gunicorn or another WSGI server. Remember to provision persistent storage for `artifacts/containers` if you rely on code interpreter.

5. **Testing ideas**
   - Mock OpenAI by swapping `app/openai_client.client` with a fake during unit tests.
   - Add Cypress-style smoke tests around the frontend once it is hosted behind an HTTP server.

Welcome aboard! Tweak this guide as the stack evolves so the next intern can ramp up even faster.
