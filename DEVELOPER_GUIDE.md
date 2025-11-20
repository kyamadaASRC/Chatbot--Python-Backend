# Chatbot-Python Backend — Developer Guide

This guide orients new contributors to the intern-facing chatbot stack. It explains how the Flask API, consultant registry, and browser UI cooperate so you can add features, debug issues, or onboard new consultants confidently.

---

## 1. Architecture At A Glance

- **Backend** – `app/app.py` exposes a Flask API that proxies all chat traffic to the OpenAI Responses API, optionally routing requests through a “consultant” persona with its own instructions, tools, and resource files (`app/app.py:20-200`).
- **Consultant registry** – `app/consultants.py` discovers consultant directories, uploads their reference files into OpenAI vector stores, and knows how to invoke them on demand (`app/consultants.py:12-335`).
- **Frontend** – `app/templates/chatbot.html` + ES modules under `app/static/` render the chat UI, manage local sessions, create vector stores/containers, and talk to the Flask API.
- **Stateful resources** – Uploaded customer files live in OpenAI file storage and are linked to both the active session’s vector store and its code-interpreter container when runtime access is needed.

Data flow:

1. User enters a prompt in the browser (`main.js`).
2. The client ensures a chat session, vector store, and (on demand) a code-interpreter container exist (`chatSession.js`).
3. `/chat` receives the message, auto-selects an appropriate consultant (if keywords match) or falls back to a general chat model, then calls OpenAI (`app/app.py:163-205`).
4. The browser renders the assistant response, handles tool calls such as `generate_pdf`, and updates session history (`main.js`, `chatClient.js`).

### Intern quick-start checklist

1. **Clone + bootstrap** – Follow §3 verbatim. If something fails, paste the traceback into the team channel so we can update this guide.
2. **Smoke test the UI** – Use §4 to bring up the server, then confirm “New Chat” creates a session and router preview toasts appear when you type.
3. **Trace a prompt end-to-end** – Run `flask --app app.app routes` to list endpoints, set a breakpoint in `/chat`, send a prompt, and step through so you see how router + consultants interact.
4. **Read a consultant folder** – Pick any folder under `app/consultants`, skim `metadata.json` + instructions, then inspect the files that get uploaded. This mental model helps when debugging routing.
5. **Ask “why” twice** – Any time you touch code, identify the entry point that calls your change and the exit point it influences. Note both in your pull request so reviewers can follow your thinking.

---

## 2. Repository Tour

| Path | Purpose |
| --- | --- |
| `app/app.py` | Flask routes for chat, consultant metadata, vector stores, containers, files, and response proxying. |
| `app/openai_client.py` | Loads `.env`, instantiates the shared `OpenAI` client (`app/openai_client.py:7-36`). |
| `app/consultants.py` | Discovers consultants, uploads their artifacts, and runs consultant-specific responses. |
| `app/consultants/<Consultant>` | Each consultant’s `metadata.json`, `instruction.txt`, and optional resource files. |
| `app/templates/chatbot.html` | Bootstrap-based UI shell plus script tags for the ES modules. |
| `app/static/js/main.js` | Entry point that wires DOM events to the session, chat, and file managers. |
| `app/static/modules/*.js` | Modularized browser logic (`chatClient`, `chatSession`, `fileManager`, `ui`, `utils`). |
| `app/scripts/register_consultant_assests.py` | CLI helper to bulk-upload consultant resources to OpenAI files/vector stores. |
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
   - Click “New Chat”; watch the dev console for session + vector store creation logs from `chatSession.js`.
   - Send a simple question; confirm `/chat` returns `mode: "direct"` or `mode: "consultant"` depending on keywords.
   - Upload a file and ensure a toast confirms OpenAI + vector store linkage.

---

## 5. Backend Anatomy

### 5.1 Application factory

- `create_app()` wires all routes and helpers (`app/app.py:43-205`).
- `CHAT_MODEL` and `GENERAL_CHAT_SYSTEM` define the fallback assistant persona for direct chats (`app/app.py:25-31`).

### 5.2 Consultant router & parallel orchestration

1. `app/consultant_router.py` calls `ROUTER_MODEL` (default `gpt-4.1-mini`) with a JSON-only system prompt plus a lightweight catalog of consultants so it can emit `{"mode":"direct|single|parallel","primary":"...", ...}`. A keyword fallback kicks in if the router API fails or the model is missing.
2. `/v1/router/preview` exposes that decision to the frontend so it can show toast notifications (“Routing…”, “Calling 3 consultants…”) before the actual `/chat` request fires.
3. When `/chat` receives a router payload with `mode: "single"`, `_consultant_tool_call()` runs exactly one consultant (same behavior as before but now instrumented with progress logs). When `mode: "parallel"`, `_run_parallel_consultants()` fans out with a `ThreadPoolExecutor`, collects each consultant’s notes/files, and then `_summarize_consultant_results()` feeds everything back through `CHAT_MODEL` with `PARALLEL_SUMMARY_SYSTEM` to produce a merged response.
4. Router metadata plus a chronological `progress_log` array are returned to the browser so the UI can reflect each stage; failures per consultant are captured in `failures[]` without aborting the entire request when at least one agent succeeds.
5. If the caller specifies `consultant_key` explicitly (e.g., via dropdown), the router is bypassed and `/chat` acts exactly like the legacy keyword matcher.

### 5.3 REST surface area

| Endpoint | Description |
| --- | --- |
| `GET /` | Render chatbot UI (`chatbot.html`). |
| `POST /chat` | Main entry point; auto consults specialists or general model. |
| `GET /v1/consultants` | Returns consultant metadata plus the contents of `app/consultants/overview.md` so the UI can summarize available specialists. |
| Vector stores (`POST/DELETE /v1/vector_stores*`) | Create/delete stores via OpenAI SDK. |
| Files (`GET/POST/DELETE /v1/files*`) | Proxy OpenAI file APIs including content download. |
| `POST /v1/responses` | Transparent pass-through to OpenAI Responses for UI utilities (e.g., auto session naming). |

### 5.4 OpenAI client bootstrap

- `app/openai_client.py` loads `.env`, warns when keys are missing, and exposes the shared `client` (`app/openai_client.py:7-36`).
- Every backend module imports this singleton, so updating authentication or proxies only needs to happen once here.

### 5.5 Container runtime

- `/v1/containers` maps to `client.containers.create(...)`, giving each chat session its own code-interpreter workspace.
- `/v1/containers/<id>/files` either uploads raw bytes (multipart) or copies an uploaded OpenAI File into the container so the runtime can read it.
- `/v1/containers/<id>/files/<file_id>` deletes container artifacts when users remove uploads or entire sessions.
- `ChatSessionManager.ensureContainer()` lazily provisions a container; `FileManager` mirrors analysis files into it and tracks `container_file_id` so deletions stay in sync.
- Delete containers alongside vector stores during session cleanup to prevent lingering OpenAI resources (see `main.js` + `chatSession.js`).

## 6. Consultant Registry & Workflow

### 6.1 Discovery

- `_discover_consultants()` walks `app/consultants/*`, reading `metadata.json`, instructions, and collateral (`app/consultants.py:176-218`).
- Aliases/keywords are lowercased and used both for auto-selection and UI display.

### 6.2 Resource syncing

- `_initialize_vector_stores()` hashes the consultant’s local files, uploads them to OpenAI, and caches the resulting vector store ID in `vector_store_cache.json` (`app/consultants.py:143-174`).
- If you add or edit supporting files, delete the cache entry or bump the file to force a refresh.

### 6.3 Execution

- `run_consultant_response()` merges consultant-defined tools (file search, web search, code interpreter) with the active session’s vector store and container before calling `client.responses.create()` (`app/consultants.py:269-335`).
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
| `static/js/main.js` | Creates managers, wires DOM events, handles send/stop button state, manages scrolling, triggers the router preview/toast workflow, and processes tool calls like `generate_pdf`. |
| `static/modules/chatSession.js` | Keeps per-session metadata, persists to `localStorage`, provisions vector stores and containers via backend endpoints, and renames sessions using `/v1/responses` (`chatSession.js:75-226`, `chatSession.js:246-374`). |
| `static/modules/chatClient.js` | Builds the payload for `/chat`, ensures vector store + container IDs exist, defines the `generate_pdf`, `web_search_preview`, and `code_interpreter` tools, and records returned messages (`chatClient.js:12-181`). |
| `static/modules/fileManager.js` | Handles drag/drop + input uploads, pushes files to OpenAI, mirrors analysis files into the session’s container, links everything to the vector store, ingests model-generated artifacts, and cleans up OpenAI copies on delete (`fileManager.js`). |
| `static/modules/ui.js` | Renders messages, session list items, spinners, and toast notifications with Markdown sanitization and syntax highlighting. |
| `static/modules/utils.js` | Provides `fetchWithDiagnostics`, Markdown → PDF helpers, and sanitization utilities so tool calls can drop downloadable artifacts into the transcript. |

### 7.3 Message lifecycle

1. User submits a prompt → `main.js` disables inputs, renders the user bubble, and calls `chatClient.sendMessage()`.
2. `ChatClient` fetches `/chat` with the session’s `vector_store_id` and `container_id`.
3. The backend either routes to a consultant or to the general assistant and returns JSON describing the response/tool stream.
4. `main.js` renders the assistant text, inspects `output` for tool calls (e.g., `generate_pdf`), invokes utilities, and, when a PDF blob is returned, hands it to `FileManager` so it’s uploaded/linked automatically.
5. `ChatSessionManager` updates local history and triggers auto title summarization through `/v1/responses`.

---

## 8. Files, Vector Stores & Containers

- **Vector stores** – Each session calls `POST /v1/vector_stores` on creation (`chatSession.js:75-99`) so file search has isolated context. Store IDs are cached on the session object and reused for future uploads.
- **File uploads** – `FileManager.uploadFile()` sends files to `/v1/files` → OpenAI file storage, links them back via `/v1/vector_stores/{id}/files`, and keeps DOM metadata (`fileManager.js`). Vision files optionally trigger caption generation for better retrieval.
- **Generated artifacts** – When the Responses API returns `output_file_ids` (from consultants or the default assistant), the backend links them to the active session vector store and the frontend calls `fileManager.addGeneratedFiles()` so they appear in the Files list. PDF blobs created locally via `generate_pdf` are also ingested and uploaded automatically, with their preview/download links backed by the local object URL.
- **Server-generated DOCX/XLSX** – Models can call the `generate_docx` and `generate_xlsx` function tools. The Flask backend uses `python-docx` and `openpyxl` to build the documents, uploads them to OpenAI Files, links them into the session’s vector store, and returns metadata so the Files sidebar updates immediately.
- **Containers** – `ChatSessionManager.ensureContainer()` calls `/v1/containers` only when needed; `FileManager` mirrors CSV/Excel-style uploads via `/v1/containers/<id>/files` so code interpreter can read them, and deletions remove both OpenAI Files and container copies.
- **Cleanup** – Deleting a chat session should cascade through `ChatSessionManager.deleteVectorStore()` and `deleteContainer()`; deleting an individual file removes it from OpenAI, then from the container if present, before purging DOM metadata (`fileManager.js:212-302`).

---

## 9. Operational Tips & Troubleshooting

- **Missing API key** – Backend will log “⚠️ WARNING: OPENAI_API_KEY not found” on startup (`app/openai_client.py:25-36`). Ensure `.env` is accessible from the working directory.
- **Vector store churn** – If consultant resources change frequently, clear `app/consultants/vector_store_cache.json` to force re-upload on next boot.
- **File preview issues** – Most previews rely on `URL.createObjectURL`. If you refresh the page, revoke stale blob URLs or re-upload.
- **Cross-origin requests** – When serving the UI externally (e.g., VS Code Live Server), keep it on `127.0.0.1:5501` or update the `CORS` config near the bottom of `app/app.py`.
- **PDF generation errors** – `renderMarkdownPDFDownload()` depends on pdfmake/html-to-pdfmake/jsPDF scripts loaded in the template. If those CDNs fail, the fallback will still attempt jsPDF but logs warnings in the console (`static/modules/utils.js:34-163`).
- **Router sanity check** – Run `flask shell` and call `from app.consultant_router import route_consultants; route_consultants("Need an IGCE for cloud work?")` to see which consultant the LLM chooses without involving the UI.
- **Trace tool calls** – Tail the Flask log while triggering PDF/DOCX/XLSX generation; each helper prints `[files]` or `[vector-store]` log lines when uploads/linking succeed.
- **Know when to mock** – When tests cannot touch the OpenAI API, patch `app.openai_client.client` with a stub that records calls. The helper functions in `app/app.py` accept raw dicts, so your fake only needs `.responses.create()` and `.files.create()` minimal behavior.

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
   - Wrap the app with Gunicorn or another WSGI server. Ensure the `artifacts/` directory (used for generated files/vector-store caches) lives on persistent storage if you scale beyond a single instance.

5. **Testing ideas**
   - Mock OpenAI by swapping `app/openai_client.client` with a fake during unit tests.
   - Add Cypress-style smoke tests around the frontend once it is hosted behind an HTTP server.

---

## 11. Glossary & acronyms

| Term | Meaning |
| --- | --- |
| **Consultant** | A persona folder under `app/consultants/` that owns a prompt, keywords, and resource files. |
| **Vector store** | Dense retrieval index hosted by OpenAI. Each chat session gets its own store, and each consultant has a pre-seeded store of reference docs. |
| **Container / code interpreter** | OpenAI’s sandbox runtime for running Python. We provision one lazily per chat session. |
| **Router** | The LLM prompt in `consultant_router.py` that decides whether to run the general assistant, one consultant, or multiple consultants in parallel. |
| **Tool call** | OpenAI Responses feature that lets a model request `generate_pdf`, `generate_docx`, `generate_xlsx`, or `file_search`. Backend helpers satisfy the request and feed the resulting file IDs back into the session. |

Welcome aboard! Tweak this guide as the stack evolves so the next intern can ramp up even faster.
