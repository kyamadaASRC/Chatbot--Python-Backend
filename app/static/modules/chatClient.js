// chatClient.js centralizes how we build payloads for the Flask `/chat` route and post-process responses.
import { renderMarkdownPDFDownload } from "./utils.js";
import { showToast } from "./ui.js";

export class ChatClient {
  constructor(apiKey, model, sessionManager) {
    // API key/model are injected by the server; sessionManager provides IDs for vector stores/containers.
    this.apiKey = apiKey;
    this.model = model;
    this.sessionManager = sessionManager;
  }

  // Optional routerDecision allows the caller to hint which consultants to run.
  async sendMessage(prompt, useStreaming = false, signal = null, systemPrompt = "", routerDecision = null) {
    const session = this.sessionManager.getCurrentSession() || {};
    const history = this.sessionManager.getHistory() || [];

    if (!prompt || typeof prompt !== "string" || !prompt.trim()) {
      throw new Error("User prompt is missing or invalid.");
    }

    const sysPrompt = systemPrompt?.trim()
      ? systemPrompt
      : `You are a helpful assistant. You can use the tools 'generate_pdf' and 'generate_xlsx' to create downloadable artifacts, and 'select_docx' + 'edit_docx' to work with DOCX templates.
         For DOCX/template requests (e.g., lesson plans, forms), call select_docx to pick a template (file_info provides keywords/summary). IN THE SAME TURN, once you have the file_id, draft anchor points/sections/placeholders for the request and then call edit_docx with the file_id and those anchor-driven instructions. Do NOT end after selection; always follow with edit_docx. Do NOT use generate_pdf for DOCX/template generation.
         When the user requests a PDF report, call generate_pdf with your response in raw Markdown (and an optional filename). For tabular deliverables, call generate_xlsx with one or more worksheets.
         Respond using Markdown syntax for code, but do not include additional Markdown fences inside other code blocks. 
         When outputting code, always wrap it in fenced Markdown code blocks (\`\`\`) so it renders as text, not executable HTML.
         Always leave a blank line before and after fenced code blocks.
         Otherwise, just reply normally in raw Markdown.` + (window.last_selected_docx_id ? ` You already have a selected template (file_id=${window.last_selected_docx_id}). Do NOT call select_docx again; call edit_docx with that file_id and the user's latest answers.` : "");

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

    // Use existing container if already provisioned; do not auto-create unless explicitly requested elsewhere.
    const containerId = window.current_container_id || null;

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

    tools.push({
      type: "function",
      name: "generate_xlsx",
      description: "Create an .xlsx workbook from structured row data.",
      parameters: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "Optional name for the generated .xlsx file.",
          },
          sheets: {
            type: "array",
            description: "List of worksheets to include. Each sheet must define a name and rows.",
            items: {
              type: "object",
              properties: {
                name: { type: "string", description: "Worksheet name (31 chars max)." },
                rows: {
                  type: "array",
                  description: "Rows of data; each row is an array of cell values.",
                  items: {
                    type: "array",
                    items: {},
                  },
                },
              },
              required: ["rows"],
            },
          },
        },
        required: ["sheets"],
      },
    });

    if (!window.last_selected_docx_id) {
      tools.push({
        type: "function",
        name: "select_docx",
        description: "Pick the best DOCX template from the library manifest.",
        parameters: {
          type: "object",
          properties: {
            prompt: {
              type: "string",
              description: "Description of the document/template that is needed.",
            },
            vector_store_id: {
              type: "string",
              description: "Optional vector store id to scope search and attach files.",
            },
          },
          required: ["prompt"],
        },
      });
    }

    tools.push({
      type: "function",
      name: "edit_docx",
      description: "Apply edit instructions to a selected DOCX template.",
      parameters: {
        type: "object",
        properties: {
          file_id: {
            type: "string",
            description: "The OpenAI file_id of the template to edit.",
          },
          edit_instructions: {
            type: "string",
            description: "Instructions to apply to the chosen template.",
          },
          selection_text: {
            type: "string",
            description: "Optional selection summary/context from select_docx.",
          },
          vector_store_id: {
            type: "string",
            description: "Optional vector store id to attach generated files to.",
          },
        },
        required: ["file_id", "edit_instructions"],
      },
    });

    tools.push({ type: "web_search_preview" });
    const codeInterpreterTool = { type: "code_interpreter" };
    if (containerId) {
      codeInterpreterTool.container = containerId;
    } else {
      codeInterpreterTool.container = { type: "auto" };
    }
    tools.push(codeInterpreterTool);

  const payload = {
      model: this.model,
      input,
      tools,
      stream: useStreaming,
    };

    // Note: Some API variants reject unknown params; omit tool_resources.

    // console.log("Sending payload (history msgs:", historyMsgs.length, "):", JSON.stringify(payload, null, 2)); // Debug
    // Backend `/chat` handles routing + tool execution so the browser only makes this single request.
    const res = await fetch("/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        message: prompt,
        vector_store_id: vectorStoreId || null,
        container_id: containerId || null,
        tools,
        history: historyMsgs,
        files: Array.isArray(session.files) ? session.files : [],
        selected_file_id: window.last_selected_docx_id || null,
        // Pass along the previewed router recommendation so the backend avoids double work.
        router_decision: routerDecision || null,
      }),
      signal,
    });


    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      console.error("OpenAI API error:", err);
      showToast(err?.error?.message || "OpenAI API error", "error", 5000);
      throw new Error(err?.error?.message || "Unknown API error.");
    }

    const data = await res.json();
  
    console.log("OpenAI Response Data:", data);
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
    data._assistant_message = assistantMsg;

    // Expose the full chat model payload for debugging (parity with router/summary logs).
    if (data?.response_payload) {
      console.log("Chat model response payload:", data.response_payload);
    }

    // Tool-call handling is centralized in main.js to avoid duplicates.
    return data;
  }
}
