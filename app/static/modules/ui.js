// modules/ui.js
// Clean model/user text to remove stray tokens and private-use glyphs
function collapseSpacedOutWords(str) {
  try {
    return str.replace(/((?:\p{L}\s){3,}\p{L})/gu, (m) => m.replace(/\s+/g, ""));
  } catch { return str; }
}

function cleanModelText(text = "") {
  try {
    let t = String(text);
    // Remove special citation tokens sometimes emitted by models, e.g. "cite..."
    t = t.replace(/cite[\s\S]*?/g, "");
    // Remove Private Use Area glyphs which render as odd icons in many fonts
    t = t.replace(/[\uE000-\uF8FF]/g, "");
    // Remove zero-width characters and directional marks
    t = t.replace(/[\u200B-\u200D\uFEFF\u200E\u200F]/g, "");
    // Normalize uncommon spaces to regular space (NBSP, thin space, hair space, etc.)
    t = t.replace(/[\u00A0\u2000-\u200A\u202F\u205F]/g, " ");
    // Remove stray ampersands not part of HTML entities (often causes &M&a&k&e artifacts)
    t = t.replace(/&(?!amp;|lt;|gt;|quot;|apos;|nbsp;|#[0-9]+;|#x[0-9A-Fa-f]+;)/g, "");
    // If &amp; is interleaved between letters/digits, drop it (handle both sides)
    t = t.replace(/([A-Za-z0-9])&amp;(?=[A-Za-z0-9])/g, '$1');
    t = t.replace(/(?<=[A-Za-z0-9])&amp;([A-Za-z0-9])/g, '$1');
    // Collapse repeated spaces
    t = t.replace(/[ \t]{2,}/g, " ");
    // Collapse sequences like "M a k e" into "Make" when detected
    t = collapseSpacedOutWords(t);
    // Prevents <ul><li><p>code</p></li></ul> issues)
    t = t.replace(/([^\n])\n(```)/g, "$1\n\n$2");
    // Ensure blank line after fenced blocks
    t = t.replace(/(```[\s\S]*?```)([^\n])/g, "$1\n\n$2");
    // Only trim outside of code fences
    if (!/^```/.test(t)) {
        t = t.trim();
    }
    return t;
  } catch { return String(text || ""); }
}

// Render helpers that prefer Markdown + sanitization when available
function renderContent(div, content, { allowMarkdown = true } = {}) {
  const cleaned = cleanModelText(content);
  const hasMarked = !!(typeof marked?.parse === "function");
  const hasDOMPurify = !!(globalThis.DOMPurify && typeof globalThis.DOMPurify.sanitize === "function");
  
  if (allowMarkdown && hasMarked) {
    try {
      const html = marked.parse(cleaned);
      div.innerHTML = hasDOMPurify ? globalThis.DOMPurify.sanitize(html) : html;

      // ✅ Apply syntax highlighting after markdown is rendered
      if (window.hljs) {
        div.querySelectorAll("pre code").forEach((block) => {
          block.classList.add("hljs"); // ensure the class exists
          hljs.highlightElement(block);
        });
      }
      
      return;
    } catch { /* fall through to text */ }
  }
  // Fallback: plain text
  div.textContent = cleaned;
}

export function renderUserMessage(content) {
  const chatContainer = document.getElementById("chat-history");
  const msg = document.createElement("div");
  msg.className = "chat-message user";
  const inner = document.createElement("div");
  inner.className = "message-content";
  // For user text, render as plain text (no markdown) for safety
  renderContent(inner, content, { allowMarkdown: false });
  msg.appendChild(inner);
  chatContainer.appendChild(msg);
  scrollToBottom();
}

export function renderAssistantMessage(content) {
  const chatContainer = document.getElementById("chat-history");
  const msg = document.createElement("div");
  msg.className = "chat-message assistant";
  const inner = document.createElement("div");
  inner.className = "message-content";
  // Assistant output: allow Markdown; sanitize HTML to avoid XSS
  renderContent(inner, content, { allowMarkdown: true });
  msg.appendChild(inner);
  chatContainer.appendChild(msg);
  scrollToBottom();
}

export function renderSystemMessage(content) {
  const chatContainer = document.getElementById("chat-history");
  const msg = document.createElement("div");
  msg.className = "chat-message system";
  const inner = document.createElement("div");
  inner.className = "message-content"
  renderContent(inner, content, { allowMarkdown: false });
  msg.appendChild(inner);
  chatContainer.appendChild(msg);
  scrollToBottom();
}

export function renderSpinner() {
  const spinner = document.createElement("div");
  spinner.className = "spinner";
  spinner.innerHTML = `<div class="dot"></div><div class="dot"></div><div class="dot"></div>`;
  document.getElementById("chat-history").appendChild(spinner);
  scrollToBottom();
  return spinner;
}

export function removeSpinner(spinner) {
  if (spinner && spinner.parentNode) spinner.remove();
}

export function scrollToBottom() {
  const chatContainer = document.getElementById("chat-history");
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

export function renderDownloadLink(blob, filename = "output.pdf") {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.textContent = "⬇️ Download PDF";
  link.className = "download-link";
  document.getElementById("chat-history").appendChild(link);
  scrollToBottom();
}

// Toasts
export function showToast(message, type = 'info', dismissAfterMs = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return null;
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  // Force reflow for transition
  // eslint-disable-next-line no-unused-expressions
  toast.offsetHeight;
  toast.classList.add('show');
  if (dismissAfterMs > 0) {
    setTimeout(() => dismissToast(toast), dismissAfterMs);
  }
  return toast;
}

export function showSpinnerToast(message) {
  const toast = showToast("", "info", 0);
  toast.innerHTML = `<span class="spinner" style="margin-right:8px"><div class="dot"></div><div class="dot"></div><div class="dot"></div></span>${message}`;
  return toast;
}

export function dismissToast(toastEl) {
  if (!toastEl) return;
  toastEl.classList.remove('show');
  setTimeout(() => { try { toastEl.remove(); } catch {} }, 200);
}

export function clearToasts() {
  const container = document.getElementById('toast-container');
  if (container) container.innerHTML = '';
}

export function renderSessionMessages(sessionData) {
    const chatContainer = document.getElementById("chat-history");
    chatContainer.innerHTML = "";

    if (!sessionData || !Array.isArray(sessionData.messages)) return;

    sessionData.messages.forEach((msg) => {
        if (msg.role === "user") renderUserMessage(msg.content);
        else if (msg.role === "assistant") renderAssistantMessage(msg.content);
    });

    // renderSystemMessage(`🧠 Active Vector Store: ${sessionData.vector_store_id || "none"}`);
}

export function renderSessionItem(session) {
    const session_div = document.createElement("div");
    session_div.className = "chat-session-item";
    session_div.setAttribute("sessionID", session.id);
    session_div.setAttribute("title", session.name);
    session_div.setAttribute("vector_store_id", session.vector_store_id);
    session_div.setAttribute("file_ids", JSON.stringify(session.files || []));
    session_div.setAttribute("history", JSON.stringify(session.history || []));
    session_div.innerHTML = `
        <span class="session-name">${session.name || "New Chat"}</span>
        <div class="session-menu-wrapper" style="margin-left:auto; position:relative;">
        <button class="session-menu-btn" title="Options" aria-expanded="false">
            <i class="bi bi-three-dots"></i>
        </button>
        <div class="session-menu" hidden>
            <button class="rename-session">Rename</button>
            <button class="delete-session">Delete</button>
        </div>
        </div>
    `;
    session_div.style.cursor = "pointer";
    return session_div;
}

// Renders the uploaded files list for a session
export function renderFileList(files = []) {
  const list = document.getElementById("uploaded-file-list");
  if (!list) return;
  list.innerHTML = "";

  files.forEach(file => {
    const div = document.createElement("div");
    div.className = "uploaded-file-item";
    div.innerHTML = `
      <span>${file.name}</span>
      <button class="delete-file-btn" title="Delete file">×</button>
    `;
    list.appendChild(div);
  });
}

// Code Coloring
const { Marked } = globalThis.marked;
const { markedHighlight } = globalThis.markedHighlight;

const marked = new Marked(
  markedHighlight({
	emptyLangClass: 'hljs',
    langPrefix: 'hljs language-',
    highlight(code, lang, info) {
      const aliases = { py: "python", js: "javascript" };
      const language = hljs.getLanguage(aliases[lang] || lang) ? aliases[lang] || lang : "plaintext";
      return window.hljs.highlight(code, { language }).value;
    }
  })
);
marked.use({
  renderer: {
    code(code, infostring, escaped) {
      // Prevent automatic trim()
      return `<pre><code class="hljs language-${infostring || ''}">${code}</code></pre>`;
    }
  }
});

// Toast (if needed)
//show: () => {
//    toast-element-bootstrap.show();
//},
//hide: () => {
//    toast-element-bootstrap.hide();
//},
//dispose () => {
//    return new PRmise ((resolve) => {
//        toast-element-bootstrap.dispose();
//        toast-element.addEventListner("hidden.bs.toast", () => {
//            toast-element.remove();
//            resolve()true;
//        });
//    });
//},
