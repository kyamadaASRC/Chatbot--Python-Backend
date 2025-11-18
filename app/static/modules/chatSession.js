// modules/chatSession.js
import { fetchWithDiagnostics } from "./utils.js";

const LOCAL_API_BASE = window.LOCAL_API_BASE || window.location.origin || "http://127.0.0.1:5001";
const apiUrl = (path = "") => `${LOCAL_API_BASE}${path}`;

export class ChatSessionManager {
    constructor(apiKey, model) {
        this.apiKey = apiKey;
        this.model = model;
        this.sessions = new Map();
        this.currentSessionId = null;
        this.defaultSystemPrompt = `You are a helpful assistant. You can use the tool 'generate_pdf' to create downloadable PDFs.
         When the user requests a document, report, or formatted output, call generate_pdf(markdown_text=your response in raw Markdown).
         Respond using Markdown syntax for code, but do not include additional Markdown fences inside other code blocks. 
         When outputting code, always wrap it in fenced Markdown code blocks (\`\`\`) so it renders as text, not executable HTML.
         Always leave a blank line before and after fenced code blocks.
         Otherwise, just reply normally in raw Markdown.`;
    }

    // Returns full chat history for a session (user + assistant messages)
    getSessionMessages(sessionId = this.currentSessionId) {
    const session = this.sessions.get(sessionId);
    if (!session) return [];
    return session.history || [];
    }

    //Loads session messages and optional metadata (vector store, files)
    loadSessionData(sessionId) {
    const session = this.switchSession(sessionId);
    if (!session) return null;

    return {
        id: session.id,
        name: session.name,
        vector_store_id: session.vector_store_id,
        container_id: session.container_id || null,
        files: session.files || [],
        messages: session.history || []
    };
    }

    hydrateSessionFiles(session, retry = 0) {
        if (!session || !session.id) return session;

        const selector = `.chat-session-item[sessionID="${session.id}"]`;
        const sessionEl = document.querySelector(selector);

        if (!sessionEl && retry < 3) {
            // wait 100ms and try again (element might not be in DOM yet)
            setTimeout(() => this.hydrateSessionFiles(session, retry + 1), 100);
            return session;
        }

        if (!sessionEl) {
            console.warn(`[Session] No DOM element found for ${session.id}`);
            return session;
        }

        try {
            const storedFiles = JSON.parse(sessionEl.getAttribute("file_ids") || "[]");
            if (Array.isArray(storedFiles) && storedFiles.length) {
                session.files = storedFiles;
                console.log(`[Session] Hydrated ${storedFiles.length} file(s) for session ${session.id}`);
            } else {
                session.files = session.files || [];
                console.log(`[Session] No stored files for ${session.id}`);
            }
        } catch (err) {
            console.warn("[Session] Failed to hydrate files:", err);
        }

        return session;
    }

    // Create Session
    async createSession(name = "New Chat", systemPrompt = this.defaultSystemPrompt) {
        const id = crypto.randomUUID();

        // 🧠 Create a dedicated vector store for this session
        const vector = await this.createVectorStore(id);

        const session = {
            id,
            name: "New Chat",
            history: [],
            files: [],
            vector_store_id: vector?.id || null,
            container_id: null,
        };

        // Track in-memory and set as current
        this.sessions.set(id, session);
        this.currentSessionId = id;
        window.current_session_id = id;
        window.current_vector_store_id = vector?.id || null;
        window.current_container_id = null;
        localStorage.setItem(`session_${id}`, JSON.stringify(session));

        return session;
    }

    getCurrentSession() {
        return this.sessions.get(this.currentSessionId);
    }

    switchSession(id) {
        if (this.sessions.has(id)) {
            this.currentSessionId = id;
            const session = this.sessions.get(id);

            this.hydrateSessionFiles(session);

            return session;
        }
        return null;
    }

    async createVectorStore() {
        try {
            const res = await fetch(apiUrl("/v1/vector_stores"), {
                method: "POST",
                headers: {"Content-Type": "application/json"
                },
                body: JSON.stringify({
                name: `Session Vector Store - ${new Date().toISOString()}`
                })
            });

            const vs = await res.json();
            console.log("[Vector Store] Created:", vs.id);
            return vs
        } catch (err) {
            console.error("❌ Failed to create vector store:", err);
            return null;
            }
    }

    async deleteVectorStore(vectorStoreId) {
        try {
            const res = await fetch(apiUrl(`/v1/vector_stores/${vectorStoreId}`), {
                method: "DELETE",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            return await res.json();
        } catch (err) {
            console.error("Failed to delete vector store:", err);
            return null;
        }
    }

    getVectorStoreId() {
        return this.getCurrentSession()?.vector_store_id || null;
    }

    // Create a new container runtime via backend/OpenAI
    async createContainer() {
        try {
            const res = await fetch(apiUrl("/v1/containers"), {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    name: `session-container-${new Date().toISOString()}`
                }),
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err?.error?.message || res.statusText);
            }

            const container = await res.json();
            console.log("[Container] Created:", container.id);
            return container;
        } catch (err) {
            console.error("❌ Failed to create container:", err);
            return null;
        }
    }

    // Lazily create container if needed
    async ensureContainer() {
        const session = this.getCurrentSession();
        if (session?.container_id) return session.container_id;

        const container = await this.createContainer();
        const id = container?.id || null;
        if (id) {
            if (session) {
                session.container_id = id;
                this.saveSessionsToLocal?.();
            }
            window.current_container_id = id;
            try {
                const sessionEl = document.querySelector(`.chat-session-item[sessionID=\"${session?.id}\"]`);
                if (sessionEl) sessionEl.setAttribute("container_id", id);
            } catch {}
        }
        return id;
    }

    async deleteContainer(containerId) {
        if (!containerId) return null;
        try {
            const res = await fetch(apiUrl(`/v1/containers/${containerId}`), {
                method: "DELETE",
                headers: {
                    "Content-Type": "application/json",
                },
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err?.error?.message || res.statusText);
            }
            const data = await res.json().catch(() => null);
            const session = this.getCurrentSession();
            if (session && session.container_id === containerId) {
                session.container_id = null;
                window.current_container_id = null;
                this.saveSessionsToLocal?.();
            }
            return data;
        } catch (err) {
            console.warn("❌ Failed to delete container:", err);
            return null;
        }
    }

    addMessageToCurrent(role, content) {
        const session = this.getCurrentSession();
        if (!session) return;
        session.history.push({ role, content });
        this.saveSessionsToLocal();
    }

    getHistory() {
        return this.getCurrentSession()?.history || [];
    }

    getSystemPrompt() {
        return this.getCurrentSession()?.systemPrompt || this.defaultSystemPrompt;
    }

    setSystemPrompt(prompt) {
        const session = this.getCurrentSession();
        if (session) {
        session.systemPrompt = prompt;
        this.saveSessionsToLocal();
        }
    }

    addMessageToCurrent(role, content) {
        const session = this.getCurrentSession();
        if (!session) return;

        // Prevent adding duplicate system prompts
        if (role === "system" && session.history.some(m => m.role === "system")) return;

         session.history.push({ role, content });
        this.saveSessionsToLocal();
    }

    async updateSessionSummary(prompt, assistantMessage) {
        const session = this.getCurrentSession();
        if (!session) return;

        const cleanHistory = session.history.filter(msg =>
        ["user", "assistant"].includes(msg.role)
        );

        const text = cleanHistory
        .slice(0, 4)
        .map(msg => `${msg.role}: ${msg.content}`)
        .join("\n");

        const summaryPrompt = `Summarize the following chat in 5 words or fewer:\n\n${text}`;
        const summaryModel = this.model || window.CHAT_MODEL || "gpt-4.1-mini";
        if (!summaryModel) {
            console.warn("⚠️ No summary model configured; skipping title update.");
            return;
        }
        const payload = {
            model: summaryModel,
            input: [
                { role: "user", content: summaryPrompt }
            ]
        };

        try {
            const res = await fetchWithDiagnostics(apiUrl("/v1/responses"), {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(payload)
            });
            const data = await res.json().catch(() => ({}));

            const title = data.output?.[0]?.content?.[0]?.text?.trim() || session.name;
            session.name = title;
            this.saveSessionsToLocal();
        } catch (err) {
            console.warn("⚠️ Failed to summarize session:", err);
        }
    }

    // Safer version used by main.js to rename session after first assistant reply
    async updateSessionSummarySafe(prompt, assistantMessage) {
        const session = this.getCurrentSession();
        if (!session) return;

        const isDefaultName = !session.name || session.name === "New Chat";
        const convo = (session.history || []).filter(m => ["user","assistant"].includes(m.role));
        if (!isDefaultName || convo.length < 2) return;

        // Show loading UI on the active session item
        try {
            const sessionEl = document.querySelector(`.chat-session-item[sessionID="${session.id}"]`);
            const nameEl = sessionEl?.querySelector('.session-name');
            if (nameEl && !nameEl.dataset.renaming) {
                nameEl.dataset.renaming = '1';
                nameEl.dataset.prev = nameEl.textContent || '';
                nameEl.innerHTML = '<span class="spinner"><div class="dot"></div><div class="dot"></div><div class="dot"></div></span>';
            }
        } catch {}

        console.log('Requesting chat title summary…');
        const text = convo.slice(0, 6).map(m => `${m.role}: ${m.content}`).join("\n");
        const summaryPrompt = `Summarize the following chat in 5 words or fewer:\n\n${text}`;
        const summaryModel = this.model || window.CHAT_MODEL || "gpt-4.1-mini";
        if (!summaryModel) {
            console.warn("⚠️ No summary model configured; skipping title update.");
            return;
        }
        const payload = {
            model: summaryModel,
            input: [
                { role: "system", content: "Create a short, descriptive title." },
                { role: "user", content: summaryPrompt }
            ]
        };

        try {
            const res = await fetchWithDiagnostics(apiUrl("/v1/responses"), {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(payload)
            });
            const data = await res.json().catch(() => ({}));
            console.log("OpenAI Response Data:", data);

            // Extract title robustly from response
            let title = "";
            const outs = Array.isArray(data?.output) ? data.output : [];
            for (const o of outs) {
                if (Array.isArray(o?.content)) {
                    const hit = o.content.find(v => typeof v?.text === 'string' && v.text.trim());
                    if (hit) { title = hit.text.trim(); break; }
                }
                if (typeof o?.text === 'string' && o.text.trim()) { title = o.text.trim(); break; }
            }
            if (!title && Array.isArray(data?.output_text) && data.output_text[0]) {
                title = String(data.output_text[0]).trim();
            }
            if (!title && data?.choices?.[0]?.message?.content) {
                title = String(data.choices[0].message.content).trim();
            }

            // If the title equals the first user message verbatim, compute a local combined one
            const firstUser = convo.find(m => m.role === "user");
            const firstAssistant = convo.find(m => m.role === "assistant");
            const buildLocalTitle = () => {
                const u = (firstUser?.content || "").split(/\s+/).slice(0, 3).join(" � ");
                const a = (firstAssistant?.content || "").split(/\s+/).slice(0, 3).join(" � ");
                return [u, a].filter(Boolean).join(" � ") || "New Chat";
            };
            if (title && firstUser?.content && title.toLowerCase() === firstUser.content.trim().toLowerCase()) {
                title = buildLocalTitle();
            }

            if (!title) {
                title = buildLocalTitle();
            }

            if (title) {
                session.name = title;
                this.saveSessionsToLocal();
                // Update UI name and clear loading
                try {
                    const sessionEl = document.querySelector(`.chat-session-item[sessionID="${session.id}"]`);
                    const nameEl = sessionEl?.querySelector('.session-name');
                    if (nameEl) {
                        nameEl.textContent = title;
                        delete nameEl.dataset.renaming;
                        delete nameEl.dataset.prev;
                    }
                } catch {}
                console.log('Chat title updated:', title);
            }
        } catch (err) {
            console.warn("Failed to summarize session:", err);
            // Local combined fallback if API fails
            const firstUser = convo.find(m => m.role === "user");
            const firstAssistant = convo.find(m => m.role === "assistant");
            const u = (firstUser?.content || "").split(/\s+/).slice(0, 3).join(" � ");
            const a = (firstAssistant?.content || "").split(/\s+/).slice(0, 3).join(" � ");
            session.name = [u, a].filter(Boolean).join(" � ") || session.name;
            this.saveSessionsToLocal();
            try {
                const sessionEl = document.querySelector(`.chat-session-item[sessionID="${session.id}"]`);
                const nameEl = sessionEl?.querySelector('.session-name');
                if (nameEl) {
                    nameEl.textContent = session.name;
                    delete nameEl.dataset.renaming;
                    delete nameEl.dataset.prev;
                }
            } catch {}
        }
    }

    // --- Persistence ---
    saveSessionsToLocal() {
        localStorage.setItem("chatSessions", JSON.stringify(Array.from(this.sessions.entries())));
    }

    loadSessionsFromLocal() {
        const data = JSON.parse(localStorage.getItem("chatSessions") || "[]");
        this.sessions = new Map(data);
    }

}
