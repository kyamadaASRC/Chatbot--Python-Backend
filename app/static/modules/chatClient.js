// chatClient.js
import { renderMarkdownPDFDownload } from "./utils.js";
import { showToast, dismissToast } from "./ui.js";

export class ChatClient {
  constructor(apiKey, model, sessionManager) {
    this.apiKey = apiKey;
    this.model = model;
    this.sessionManager = sessionManager;
  }

  async sendMessage(prompt, useStreaming = false, signal = null, systemPrompt = "") {
    const session = this.sessionManager.getCurrentSession() || {};
    const history = this.sessionManager.getHistory() || [];

    if (!prompt || typeof prompt !== "string" || !prompt.trim()) {
      throw new Error("User prompt is missing or invalid.");
    }

    const sysPrompt = systemPrompt?.trim()
      ? systemPrompt
      : `You are a helpful assistant. You can use the tool 'generate_pdf' to create downloadable PDFs.
         When the user requests a document, report, or formatted output, call generate_pdf(markdown_text=your response in raw Markdown).
         Respond using Markdown syntax for code, but do not include additional Markdown fences inside other code blocks. 
         When outputting code, always wrap it in fenced Markdown code blocks (\`\`\`) so it renders as text, not executable HTML.
         Always leave a blank line before and after fenced code blocks.
         Otherwise, just reply normally in raw Markdown.`;

    // Build full conversation context: system + prior history + current user
    const historyMsgs = (history || [])
      .filter(m => m && (m.role === "user" || m.role === "assistant"))
      .map(m => ({ role: m.role, content: m.content }));

    const input = [
      { role: "system", content: sysPrompt },
      ...historyMsgs,
      { role: "user", content: prompt },
    ];

    // Ensure we have a vector store; create one on-demand
    let vectorStoreId = window.current_vector_store_id;
    if (!vectorStoreId && this.sessionManager?.createVectorStore) {
      try {
        const vs = await this.sessionManager.createVectorStore();
        vectorStoreId = vs?.id || null;
        if (vectorStoreId) {
          const s = this.sessionManager.getCurrentSession?.();
          if (s) {
            s.vector_store_id = vectorStoreId;
            this.sessionManager.saveSessionsToLocal?.();
          }
          window.current_vector_store_id = vectorStoreId;
        }
      } catch (e) {
        console.warn("Could not create vector store on-demand:", e);
      }
    }
    // Ensure we have a container; create one on-demand
    let containerId = window.current_container_id;
    if (!containerId && this.sessionManager?.createContainer) {
      try {
        const con = await this.sessionManager.createContainer();
        containerId = con?.id || null;
        if (containerId) {
          const s = this.sessionManager.getCurrentSession?.();
          if (s) {
            s.container_id = containerId;
            this.sessionManager.saveSessionsToLocal?.();
          }
          window.current_container_id = containerId;
        }
      } catch (e) {
        console.warn("Could not create container on-demand:", e);
      }
    }

    const tools = [];
    if (vectorStoreId) {
      // Put file_search first and include both shapes for compatibility
      tools.push({ type: "file_search", vector_store_ids: [vectorStoreId] });
    }
    tools.push({
      type: "function",
      name: "generate_pdf",
      description: "Convert markdown text into a downloadable PDF.",
      parameters: {
        type: "object",
        properties: {
          markdown_text: {
            type: "string",
            description: "The Markdown content to be converted into a PDF document.",
          },
        },
        required: ["markdown_text"],
      },
    });

    tools.push({type: "web_search_preview"});
    if (containerId) {
        tools.push({
            type: "code_interpreter", 
            container: containerId,
        });
    } else {
        // fallback: auto runtime
        tools.push({
            type: "code_interpreter",
            container: "auto",
        });
    };

    const payload = {
      model: this.model,
      input,
      tools,
      stream: useStreaming,
    };

    // Note: Some API variants reject unknown params; omit tool_resources.

    // console.log("Sending payload (history msgs:", historyMsgs.length, "):", JSON.stringify(payload, null, 2)); // Debug
    const pendingToast = showToast("Contacting OpenAI…", "info", 0);

    const res = await fetch("/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        message: prompt,
        vector_store_id: vectorStoreId || null,
        container_id: containerId || null,
      }),
      signal,
    });


    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      console.error("OpenAI API error:", err);
      dismissToast(pendingToast);
      showToast(err?.error?.message || "OpenAI API error", "error", 5000);
      throw new Error(err?.error?.message || "Unknown API error.");
    }

    const data = await res.json();
  
    console.log("OpenAI Response Data:", data);
    dismissToast(pendingToast);
    showToast("Response received", "success", 1200);

    // Extract assistant text robustly from the response
    const extractAssistantTextFromResponse = (resp) => {
      const outs = Array.isArray(resp?.output) ? resp.output : [];
      // Prefer the last textual item in output
      for (let i = outs.length - 1; i >= 0; i--) {
        const o = outs[i];
        if (Array.isArray(o?.content)) {
          const hit = o.content.find((c) => typeof c?.text === "string" && c.text.trim());
          if (hit) return hit.text.trim();
        }
        if (typeof o?.text === "string" && o.text.trim()) return o.text.trim();
      }
      if (Array.isArray(resp?.output_text) && resp.output_text[0]) {
        return String(resp.output_text[0]).trim();
      }
      if (resp?.choices?.[0]?.message?.content) {
        return String(resp.choices[0].message.content).trim();
      }
      return "";
    };

    const assistantMsg = extractAssistantTextFromResponse(data) || data.text || "";

    this.sessionManager.addMessageToCurrent("user", prompt);
    if (assistantMsg) {
      this.sessionManager.addMessageToCurrent("assistant", assistantMsg);
    }

    // Tool-call handling is centralized in main.js to avoid duplicates.
    return data;
  }
}
