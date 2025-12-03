
// main.js wires the UI elements to the chat/session/file managers and coordinates router previews.
// import utils.js
import {
  renderUserMessage,
  renderAssistantMessage,
  renderSystemMessage,
  renderSpinner,
  removeSpinner,
  renderDownloadLink,
  renderSessionItem,
  renderSessionMessages,
  renderFileList,
  scrollToBottom,
  showToast,
  showSpinnerToast,
  dismissToast,
  clearToasts,
} from "../modules/ui.js";

import { ChatClient } from "../modules/chatClient.js";
import { ChatSessionManager } from "../modules/chatSession.js";
import { FileManager } from "../modules/fileManager.js";
import { markdownToPDFBlob } from "../modules/utils.js";

// API configuration is injected server-side; never ship real keys in client bundles
const api_key = null;
const model = null;


// --- DOM ---
const sessionManager = new ChatSessionManager(api_key, model);
const fileManager = new FileManager(api_key);
const chatClient = new ChatClient(api_key, model, sessionManager);
// Expose for modules that rely on globals
window.sessionManager = sessionManager;
window.fileManager = fileManager;
window.current_container_id = null;
const sidebar            = document.getElementById("sidebar");
const logo               = document.getElementById("logo");
const chatHistory        = document.getElementById("chat-history");
const chatSessionList    = document.getElementById("chat-session-list");
const newChatButton      = document.getElementById("new-chat-button");
const collapseBtn        = document.getElementById("collapse-sidebar-btn");
const editField          = document.getElementById("edit-field");
const sendStopButton     = document.getElementById("send-stop-button");
const scrollDownBtn      = document.getElementById("scroll-down-btn");
const uploadedFilesList  = document.getElementById("uploaded-file-list");
const assetPaths = window.STATIC_ASSETS || {};
const systemPrompt = `You are a helpful assistant. You can use the tool 'generate_pdf' to create downloadable PDFs.
         When the user requests a document, report, or formatted output, call generate_pdf(markdown_text=your response in raw Markdown).
         Respond using Markdown syntax for code, but do not include additional Markdown fences inside other code blocks. 
         When outputting code, always wrap it in fenced Markdown code blocks (\`\`\`) so it renders as text, not executable HTML.
         Always leave a blank line before and after fenced code blocks.
         If you cannot access the data, just say so and do not provide terminal commands.
         Otherwise, just reply normally in raw Markdown.`;



//  === Runtime State ===
let abortController = null;
let isGenerating = false;
let useStreaming = false; // toggle for streaming mode
let current_session_id = null;
let current_vector_store_id = null;
let current_container_id = null;
let existingSessions = [];
// Shape the recent chat history into the payload the backend router expects.
function buildRouterHistoryPayload(history = [], limit = 6) {
  if (!Array.isArray(history)) return [];
  const trimmed = history
    .filter((entry) => entry && (entry.role === "user" || entry.role === "assistant"))
    .slice(-limit);
  return trimmed.map((entry) => ({
    role: entry.role,
    content: String(entry.content || "").slice(0, 600),
  }));
}

// Swap the text inside a spinner toast while keeping the animation running.
function updateSpinnerToast(toastEl, message) {
  if (!toastEl) return;
  toastEl.textContent = message;
}

// Legacy stub: router/preview removed; return no-op metadata.
async function previewRouterDecision(message) {
  return { decision: null, consultants: [], toasts: [] };
}

function dismissProgressToasts(handles = []) {
  handles.forEach((toast) => dismissToast(toast));
}

// Translate response/router metadata into human-friendly toast summaries.
function announceRouterCompletion(response, previewMeta) {
  if (!response) return;
  if (response.mode === "parallel") {
    const count = Array.isArray(response.consultants) ? response.consultants.length : (previewMeta?.consultants?.length || 2);
    showToast(`Summary ready from ${count} consultant${count === 1 ? "" : "s"}.`, "success", 2600);
  } else if (response.mode === "consultant") {
    const name = response.consultant_display || previewMeta?.consultants?.[0]?.display_name || "Consultant";
    showToast(`${name} finished responding.`, "success", 2200);
  } else if (previewMeta?.decision?.mode === "direct") {
    showToast("General assistant response ready.", "success", 1800);
  }
}

function renderConsultantNotesSection(response) {
  if (!response || !response.consultant_notes) return;
  const notes = response.consultant_notes;
  const chatContainer = document.getElementById("chat-history");
  if (!chatContainer) return;
  const wrapper = document.createElement("div");
  wrapper.className = "consultant-notes-block";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "consultant-notes-toggle";
  toggle.innerHTML = `<span>Consultant notes</span><span class="chevron">▼</span>`;

  const panel = document.createElement("div");
  panel.className = "consultant-notes-panel";
  panel.style.display = "none";

  const consultantMap = new Map();
  if (Array.isArray(response.consultants)) {
    response.consultants.forEach((entry) => {
      if (!entry || !entry.consultant) return;
      consultantMap.set(entry.consultant, entry);
    });
  }

  Object.entries(notes).forEach(([key, text]) => {
    const data = consultantMap.get(key) || {};
    const card = document.createElement("div");
    card.className = "consultant-note-card";

    const title = document.createElement("div");
    title.className = "consultant-note-title";
    title.textContent = data.display_name || key || "Consultant";
    card.appendChild(title);

    const body = document.createElement("div");
    body.className = "consultant-note-body";
    body.textContent = text || "No notes were returned.";
    card.appendChild(body);

    panel.appendChild(card);
  });

  let expanded = false;
  toggle.addEventListener("click", () => {
    expanded = !expanded;
    panel.style.display = expanded ? "block" : "none";
    toggle.classList.toggle("open", expanded);
  });

  wrapper.appendChild(toggle);
  wrapper.appendChild(panel);
  chatContainer.appendChild(wrapper);
  scrollToBottom();
}



// ===== Utilities =====
function isNearBottom(el, thresholdPx = 120) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < thresholdPx;
}

function updateScrollDownVisibility() {
  if (!chatHistory) return;
  if (isNearBottom(chatHistory)) {
    scrollDownBtn?.classList.add("hidden");   // style this in CSS (display:none or opacity)
  } else {
    scrollDownBtn?.classList.remove("hidden");
  }
}

function activateSessionDiv(sessionDiv) {
  document.querySelectorAll(".chat-session-item").forEach((el) => el.classList.remove("active"));
  sessionDiv.classList.add("active");
}

// Disable/enable input while generating
function setInputDisabled(disabled) {
  const chatBox = document.getElementById("chat-box");
  const uploadBtn = document.getElementById("openai-file-upload-btn");
  const uploadInput = document.getElementById("openai-file-upload-input");
  if (!editField || !sendStopButton || !chatBox) return;
  if (disabled) {
    chatBox.classList.add("generating");
    sendStopButton.src = assetPaths.messageStop || "/static/img/message-stop.svg";
    sendStopButton.classList.add("sent");
    editField.disabled = true;
    editField.placeholder = "Generating...";
    if (uploadBtn) { uploadBtn.disabled = true; uploadBtn.classList.add("disabled"); uploadBtn.title = "Disabled while generating"; }
    if (uploadInput) uploadInput.disabled = true;
  } else {
    chatBox.classList.remove("generating");
    sendStopButton.src = assetPaths.messageSend || "/static/img/message-send.svg";
    sendStopButton.classList.remove("sent");
    editField.disabled = false;
    editField.placeholder = "Type your message...";
    if (uploadBtn) { uploadBtn.disabled = false; uploadBtn.classList.remove("disabled"); uploadBtn.title = "Upload file"; }
    if (uploadInput) uploadInput.disabled = false;
  }
}

// Extract assistant text from Responses API payload (robust to shape variants)
function extractAssistantText(data) {
  if (!data) return "";
  const direct = typeof data.text === "string" ? data.text.trim() : "";
  if (direct) return direct;
  if (data.mode === "consultant" && data.consultant_notes) {
    const values = Object.values(data.consultant_notes || {}).filter((v) => typeof v === "string" && v.trim());
    if (values.length) return values[values.length - 1].trim();
  }
  const item1 = data.output?.[1]?.content?.[0]?.text;
  if (typeof item1 === "string" && item1.trim()) return item1;
  const outputs = Array.isArray(data.output) ? data.output : [];
  for (const o of outputs) {
    if (Array.isArray(o.content)) {
      for (const c of o.content) {
        if (typeof c?.text === "string" && c.text.trim()) return c.text;
      }
    }
    if (typeof o?.text === "string" && o.text.trim()) return o.text;
  }
  return "";
}

// Extract Markdown text from any tool/function call arguments
function extractMarkdownFromToolCall(data) {
  const outputs = Array.isArray(data?.output) ? data.output : [];
  for (const o of outputs) {
    if (o?.type === "output_tool_call" || o?.type === "function_call") {
      let args = o.arguments || o.function?.arguments || null;
      if (!args) continue;
      if (typeof args === "string") {
        try { args = JSON.parse(args); } catch { args = null; }
      }
      if (args && typeof args === "object" && args.markdown_text) {
        return String(args.markdown_text);
      }
    }
  }
  return "";
}

// Some consultants return tool calls (e.g., "generate_pdf"); fulfill them client-side so files show up instantly.
async function handleToolCallsIfAny(data) {
  // Look for function/tool calls named "generate_pdf"
  const outputs = Array.isArray(data?.output) ? data.output : [];
  for (const o of outputs) {
    if (o.type === "function_call" || o.type === "output_tool_call") {
      const name = o.name || o.function?.name;
      let args = o.arguments || o.function?.arguments || {};
      if (typeof args === "string") {
        try { args = JSON.parse(args); } catch { args = {}; }
      }
      if (name === "generate_pdf") {
        try {
          const md = args.markdown_text || "";
          if (md) {
            // Convert to PDF Blob (utils.js should expose markdownToPDFBlob)
            // If you already have a helper, import and call it here. For now,
            // just route to chatClient or a shared util if that's your setup.
            const { renderMarkdownPDFDownload } = await import("../modules/utils.js");
            const pdfResult = await renderMarkdownPDFDownload(md);
            if (pdfResult?.blob && window.fileManager?.ingestGeneratedFile) {
              try {
                await window.fileManager.ingestGeneratedFile(
                  pdfResult.blob,
                  pdfResult.filename || "assistant_output.pdf",
                  pdfResult.url || null
                );
              } catch (err) {
                console.warn("Failed to ingest generated PDF:", err);
              }
            }
          }
        } catch (err) {
          console.error("PDF tool call failed:", err);
        }
      }
    }
  }
}

// Creates a new session record, renders it in the sidebar, and syncs window.* globals.
async function createAndMountSession(name = "New Chat") {
    const progressToast = showToast("🧠 Initializing... ", "info", 0);

  // 1) create session data (in-memory) + vector store
  const session = await sessionManager.createSession?.(name);
  const sessionId = session?.id || crypto.randomUUID();

  // If sessionManager.createSession doesn't create vector store, do it here:
      if (!session?.vector_store_id) {
        const vs = await sessionManager.createVectorStore(sessionId);
        session.vector_store_id = vs?.id || null;
      }

  // 2) render session div w/ attributes (sessionID, vector_store_id, history, file_ids)
  const sessionDiv = renderSessionItem({
    id: sessionId,
    name: session?.name || name,
    history: session?.history || [],
    files: session?.files || [],
    vector_store_id: session.vector_store_id || null,
    container_id: session.container_id || null,
  });
  chatSessionList.appendChild(sessionDiv);

  // 3) set active
  activateSessionDiv(sessionDiv);
  sessionManager.hydrateSessionFiles(session);
  current_session_id = sessionId;
  window.current_session_id = current_session_id;
  current_vector_store_id = session.vector_store_id || null;
  window.current_vector_store_id = current_vector_store_id;
  current_container_id = session.container_id || null;
  window.current_container_id = current_container_id;

  // 4) clear chat view & show system line
  chatHistory.innerHTML = "";

  // Reset Files panel for a new chat
  try {
    if (uploadedFilesList) uploadedFilesList.innerHTML = "";
    const fileCollapse = document.getElementById("Filecollapse");
    const fileToggle = document.getElementById("file-toggle");
    if (fileCollapse) fileCollapse.classList.remove("show");
    if (fileToggle) fileToggle.setAttribute("aria-expanded", "false");
    
  } catch {}
  // renderSystemMessage(`🧠 Active Vector Store: ${current_vector_store_id || "none"}`);
    progressToast.textContent = "✅ Initialization complete — ready to chat!";
    setTimeout(() => dismissToast(progressToast), 2500);
    return sessionDiv;
}

// ===== Event Delegation: Sessions Sidebar =====
chatSessionList.addEventListener("click", async (e) => {
  const menuBtn = e.target.closest(".session-menu-btn");
  const renameBtn = e.target.closest(".rename-session");
  const deleteBtn = e.target.closest(".delete-session");
  const sessionDiv = e.target.closest(".chat-session-item");

  // 0) No session div => ignore
  if (!menuBtn && !renameBtn && !deleteBtn && !sessionDiv) return;

  // A) Toggle 3-dot menu (CSS handles visibility via class)
  if (menuBtn && sessionDiv) {
    const wrapper = menuBtn.closest(".session-menu-wrapper");
    const isOpen = wrapper.classList.contains("open");
    document.querySelectorAll(".session-menu-wrapper.open").forEach((w) => {
      w.classList.remove("open");
      const m = w.querySelector(".session-menu");
      if (m) {
        m.hidden = true;
        m.style.position = "";
        m.style.top = "";
        m.style.left = "";
      }
    });
    if (!isOpen) {
      wrapper.classList.add("open");
      const m = wrapper.querySelector(".session-menu");
      if (m) {
        // Use fixed positioning so it isn't clipped by scrolling containers
        // Prepare for measurement
        m.hidden = false;
        m.style.position = 'fixed';
        m.style.right = 'auto';
        m.style.left = '0px';
        m.style.top = '0px';
        const prevVisibility = m.style.visibility;
        m.style.visibility = 'hidden';

        // Measure
        const btnRect = menuBtn.getBoundingClientRect();
        const menuRect = m.getBoundingClientRect();
        const vw = window.innerWidth || document.documentElement.clientWidth;
        const vh = window.innerHeight || document.documentElement.clientHeight;

        // Compute target position (prefer below and right-aligned to button)
        let top = btnRect.bottom;
        let left = Math.min(vw - 8 - menuRect.width, Math.max(8, btnRect.right - menuRect.width));
        if (top + menuRect.height > vh) {
          top = Math.max(8, btnRect.top - menuRect.height);
        }
        // Apply
        m.style.top = `${top}px`;
        m.style.left = `${left}px`;
        // Lock width to measured width to avoid accidental full-width
        m.style.width = `${menuRect.width}px`;
        m.style.visibility = prevVisibility || 'visible';
      }
      console.log('[Menu] Opened session menu for', sessionDiv.getAttribute('sessionID'));
    }
    return;
  }

  // B) Rename session
  if (renameBtn) {
    const session = renameBtn.closest(".chat-session-item");
    const sessionID = session.getAttribute("sessionID");
    const nameEl = session.querySelector(".session-name");
    const newName = prompt("Enter new chat name:", nameEl?.textContent || "New Chat");
    if (newName && newName.trim()) {
      await sessionManager.renameSession?.(sessionID, newName.trim());
      nameEl.textContent = newName.trim();
      session.name = newName.trim();
      sessionDiv.setAttribute("title", newName.trim());
    }
    // close menus
    document.querySelectorAll(".session-menu-wrapper.open").forEach((w) => {
      w.classList.remove("open");
      const m = w.querySelector(".session-menu");
      if (m) m.hidden = true;
    });
    console.log('[Menu] Closed after rename/delete');
    return;
  }

  // C) Delete session
  if (deleteBtn) {
    const session = deleteBtn.closest(".chat-session-item");
    const sessionID = session.getAttribute("sessionID");
    const vectorId = session.getAttribute("vector_store_id");
    const files = JSON.parse(session.getAttribute("file_ids") || "[]");
    const containerId = session.getAttribute("container_id");

    if (!confirm("Delete this session and its files?")) {
      // close menus
      document.querySelectorAll(".session-menu-wrapper.open").forEach((w) => w.classList.remove("open"));
      return;
    }

    try {
      // Delete files from OpenAI
          for (const file of files) {
            try {
              await fileManager.deleteFile(file.id);
              if (containerId && file.container_file_id) {
                await fileManager.deleteContainerFile(containerId, file.container_file_id);
              }
            } catch (err) {
              console.warn("File delete failed:", file.id, err);
            }
          }
      // Delete vector store
          if (vectorId) await sessionManager.deleteVectorStore(vectorId);
      // Delete container
          if (containerId) await sessionManager.deleteContainer(containerId);
    } catch (err) {
      console.error("Session cleanup error:", err);
    }

    // Remove from view
    session.remove();

    // If that was active, create a new blank session
    if (current_session_id === sessionID) {
      current_session_id = null;
      window.current_session_id = current_session_id;
      current_vector_store_id = null;
      window.current_vector_store_id = current_vector_store_id;
      current_container_id = null;
      window.current_container_id = current_container_id;
      chatHistory.innerHTML = "";
      uploadedFilesList && (uploadedFilesList.innerHTML = "");
      const newDiv = await createAndMountSession("New Chat");
      activateSessionDiv(newDiv);
    }

    // close menus
    document.querySelectorAll(".session-menu-wrapper.open").forEach((w) => {
      w.classList.remove("open");
      const m = w.querySelector(".session-menu");
      if (m) m.hidden = true;
    });
    return;
  }

  // D) Switch session (click on item but not on the menu button)
  if (sessionDiv && !e.target.closest(".session-menu-btn")) {
    if (sessionDiv.classList.contains("active")) return; // already active

    const sessionID = sessionDiv.getAttribute("sessionID");
    const data = sessionManager.loadSessionData?.(sessionID);

    activateSessionDiv(sessionDiv);
    current_session_id = sessionID;
    window.current_session_id = current_session_id;
    current_vector_store_id = sessionDiv.getAttribute("vector_store_id") || data?.vector_store_id || null;
    window.current_vector_store_id = current_vector_store_id;
    current_container_id = sessionDiv.getAttribute("container_id") || data?.container_id || null;
    window.current_container_id = current_container_id;
    // Render history + files
    chatHistory.innerHTML = "";
    if (data) {
      renderSessionMessages(data);
      const filesAttr = (() => {
        try { return JSON.parse(sessionDiv.getAttribute("file_ids") || "[]"); }
        catch { return []; }
      })();
      const filesList = (filesAttr && filesAttr.length) ? filesAttr : (data.files || []);
      renderFileList(filesList);
      // Auto-expand Files section when the session has files; collapse otherwise
      try {
        const fileCollapse = document.getElementById("Filecollapse");
        const fileToggle = document.getElementById("file-toggle");
        if (filesList && filesList.length) {
          if (fileCollapse && !fileCollapse.classList.contains("show")) fileCollapse.classList.add("show");
          if (fileToggle) fileToggle.setAttribute("aria-expanded", "true");
        } else {
          if (fileCollapse) fileCollapse.classList.remove("show");
          if (fileToggle) fileToggle.setAttribute("aria-expanded", "false");
        }
      } catch {}
      //renderSystemMessage(`🧠 Active Vector Store: ${current_vector_store_id || "none"}`);
    } else {
      //renderSystemMessage(`🧠 Active Vector Store: ${current_vector_store_id || "none"}`);
    }

    scrollToBottom();
  }
});

// ===== New Chat =====
newChatButton?.addEventListener("click", async () => {
  const div = await createAndMountSession("New Chat");
  // ensure menus closed
  document.querySelectorAll(".session-menu-wrapper.open").forEach((w) => w.classList.remove("open"));
});

// ===== Sidebar Collapse (class-only) =====
function updateLogoHoverState() {
  if (!logo) return;
  if (sidebar?.classList.contains("collapsed")) {
    logo.setAttribute("title", "Expand sidebar");
    logo.style.cursor = "pointer";
  } else {
    logo.removeAttribute("title");
    logo.style.cursor = "default";
  }
}

collapseBtn?.addEventListener("click", () => {
  // Collapse when expanded; CSS hides this button when collapsed
  const wasCollapsed = sidebar.classList.contains("collapsed");
  sidebar.classList.toggle("collapsed");
  // If we are expanding now, add a transient class for icon animation
  if (wasCollapsed && !sidebar.classList.contains("collapsed")) {
    sidebar.classList.add("expanding");
    setTimeout(() => sidebar.classList.remove("expanding"), 350);
  }
  updateLogoHoverState();
});

// In collapsed state, clicking the logo expands the sidebar
logo?.addEventListener("click", () => {
  if (sidebar?.classList.contains("collapsed")) {
    sidebar.classList.remove("collapsed");
    sidebar.classList.add("expanding");
    setTimeout(() => sidebar.classList.remove("expanding"), 350);
    updateLogoHoverState();
  }
});

// Close any open session menus when clicking outside of them
document.addEventListener('click', (ev) => {
  if (ev.target.closest && (ev.target.closest('.session-menu-wrapper') || ev.target.closest('.session-menu'))) return;
  const openMenus = document.querySelectorAll('.session-menu-wrapper.open');
  if (openMenus.length) {
    openMenus.forEach(w => {
      w.classList.remove('open');
      const m = w.querySelector('.session-menu');
      if (m) { m.hidden = true; m.style.position=''; m.style.top=''; m.style.left=''; }
    });
    console.log('[Menu] Closed all menus (click outside)');
  }
});

// Close menus on scroll to avoid desynced fixed position
window.addEventListener('scroll', () => {
  const openMenus = document.querySelectorAll('.session-menu-wrapper.open');
  if (openMenus.length) {
    openMenus.forEach(w => {
      w.classList.remove('open');
      const m = w.querySelector('.session-menu');
      if (m) { m.hidden = true; m.style.position=''; m.style.top=''; m.style.left=''; }
    });
    console.log('[Menu] Closed all menus (scroll)');
  }
}, { capture: true, passive: true });

// ===== Scroll Handling =====
chatHistory?.addEventListener("scroll", updateScrollDownVisibility);
scrollDownBtn?.addEventListener("click", () => {
  try {
    const prefersReduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    chatHistory.scrollTo({ top: chatHistory.scrollHeight, behavior: prefersReduced ? 'auto' : 'smooth' });
  } catch {
    chatHistory.scrollTop = chatHistory.scrollHeight;
  }
  // Nudge visibility update after the smooth scroll completes
  setTimeout(updateScrollDownVisibility, 350);
});

// ===== Send / Stop =====
    async function handleSendMessage(textArg = null) {
      const text = (textArg ?? editField.value).trim();
      if (!text) return;

  renderUserMessage(text);
  editField.value = "";
  const spinner = renderSpinner();

  let routerPreview = null;
  try {
    abortController = new AbortController();
    isGenerating = true;
    setInputDisabled(true);
    // Show staged toasts (routing → consultant → summary) before sending /chat.
    routerPreview = await previewRouterDecision(text);
    const response = await chatClient.sendMessage(
      text,
      false,
      abortController.signal,
      systemPrompt,
      routerPreview?.decision || null
    );
    dismissProgressToasts(routerPreview?.toasts || []);
    announceRouterCompletion(response, routerPreview);

    if (
      window.fileManager?.addGeneratedFiles &&
      Array.isArray(response?.generated_files) &&
      response.generated_files.length
    ) {
      try {
        window.fileManager.addGeneratedFiles(response.generated_files);
      } catch (err) {
        console.warn("Failed to record generated files:", err);
      }
    }

    removeSpinner(spinner);
    const output = extractAssistantText(response) || "";
    const toolMd = extractMarkdownFromToolCall(response) || "";
    const finalText = output && output.trim() ? output : (toolMd && toolMd.trim() ? toolMd : "Generated a PDF (see link below).");
    sessionManager.addMessageToCurrent("user", text);
    sessionManager.addMessageToCurrent("assistant", finalText);
    renderAssistantMessage(finalText);
    if (response?.generated_files) {
      renderGeneratedPreviews(response.generated_files);
    }
    renderConsultantNotesSection(response);
    await handleToolCallsIfAny(response);
    await sessionManager.updateSessionSummarySafe(text, finalText);
    isGenerating = false;
    setInputDisabled(false);

  } catch (err) {
    removeSpinner(spinner);
    dismissProgressToasts(routerPreview?.toasts || []);
    console.error("OpenAI error:", err);
    const message = err?.message ? `⚠️ ${err.message}` : "⚠️ Request failed.";
    renderSystemMessage(message);
    isGenerating = false;
    setInputDisabled(false);
  }
}

editField?.addEventListener("keydown", async (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const text = editField.value.trim();
    if (!text) return;
    editField.value = "";
    await handleSendMessage(text);
  }
});

sendStopButton?.addEventListener("click", async () => {
  if (isGenerating && abortController) {
    abortController.abort();
    isGenerating = false;
    setInputDisabled(false);
    return;
  }
  const text = editField.value.trim();
  if (!text) return;
  editField.value = "";
  await handleSendMessage(text);
});

// ===== Initialize (no persistence) =====
// Entry point when DOM loads: ensure sidebar state, spawn a starter session, prime scroll controls.
async function initialize() {
  // ensure sidebar visible on load (CSS handles state)
  sidebar?.classList.remove("collapsed");
  
  // Ensure scroll-down button is managed by class, not inline style
  try { if (scrollDownBtn) scrollDownBtn.style.removeProperty('display'); } catch {}
  
  // Create one fresh session
  const div = await createAndMountSession("New Chat");
  activateSessionDiv(div);

  // initial scroll button state
  updateScrollDownVisibility();
  updateLogoHoverState();
}

document.addEventListener("DOMContentLoaded", initialize);
// Render inline preview cards for generated artifacts (PDF inline; DOCX with download link).
function renderGeneratedPreviews(files = []) {
  if (!chatHistory || !Array.isArray(files) || !files.length) return;

  const getLink = (f) => {
    const name = f.name || f.filename || "";
    const cid = f.container_id || f.containerId;
    const cfile = f.container_file_id || f.containerFileId;
    if (cid && cfile) return `/v1/containers/${cid}/files/${cfile}/content?name=${encodeURIComponent(name || cfile)}`;
    if (f.preview_url || f.previewUrl) return f.preview_url || f.previewUrl;
    return null;
  };

  files.forEach((file) => {
    const name = (file.name || file.filename || "").toLowerCase();
    const mime = (file.mime || file.mimetype || "").toLowerCase();
    const link = getLink(file);
    if (!link) return;

    if (mime.includes("pdf") || name.endsWith(".pdf")) {
      const card = document.createElement("div");
      card.className = "assistant-message pdf-preview-card";
      card.innerHTML = `
        <div class="preview-header">PDF Preview: ${file.name || file.id}</div>
        <div class="preview-body">
          <iframe src="${link}" title="PDF preview" loading="lazy"></iframe>
        </div>
        <div class="preview-actions">
          <a href="${link}" target="_blank" rel="noopener noreferrer">Open in new tab</a>
        </div>
      `;
      chatHistory.appendChild(card);
    } else if (mime.includes("word") || name.endsWith(".docx")) {
      const card = document.createElement("div");
      card.className = "assistant-message pdf-preview-card";
      card.innerHTML = `
        <div class="preview-header">DOCX Generated: ${file.name || file.id}</div>
        <div class="preview-actions">
          <a href="${link}" target="_blank" rel="noopener noreferrer">Download DOCX</a>
        </div>
      `;
      chatHistory.appendChild(card);
    }
  });
}
