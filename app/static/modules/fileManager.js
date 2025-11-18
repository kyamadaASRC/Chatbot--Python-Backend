// modules/fileManager.js
import { fetchWithDiagnostics } from "./utils.js";
import { showToast, dismissToast, renderFileList } from "./ui.js";

const LOCAL_API_BASE = window.LOCAL_API_BASE || "http://localhost:5001";
const apiUrl = (path = "") => `${LOCAL_API_BASE}${path}`;

export class FileManager {
  constructor(apiKey) {
    this.apiKey = apiKey;
  }

  async uploadFile(file, purpose = "assistants") {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("purpose", purpose);

    const res = await fetch(apiUrl("/v1/files"), {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.error?.message || "File upload failed");
    }
    return await res.json();
  }

  async listFiles() {
    return await fetchWithDiagnostics(apiUrl("/v1/files"), {
      method: "GET",
    });
  }

  async deleteFile(fileId) {
    return await fetchWithDiagnostics(apiUrl(`/v1/files/${fileId}`), {
      method: "DELETE",
    });
  }

  async deleteContainerFile(containerId, containerFileId) {
    if (!containerId || !containerFileId) return null;
    return await fetchWithDiagnostics(apiUrl(`/v1/containers/${containerId}/files/${containerFileId}`), {
      method: "DELETE",
    });
  }

  async linkFileToVectorStore(fileId, vectorStoreId) {
    if (!fileId || !vectorStoreId) return null;
    const res = await fetch(apiUrl(`/v1/vector_stores/${vectorStoreId}/files`), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ file_id: fileId }),
    });
    return await res.json().catch(() => ({}));
  }

  addGeneratedFiles(records = []) {
    if (!Array.isArray(records) || !records.length) return;
    const current = getCurrentSessionFiles();
    const byId = new Map();
    current.forEach((rec) => {
      const key = rec.openai_file_id || rec.id;
      if (key) byId.set(key, rec);
    });
    let changed = false;
    for (const record of records) {
      const normalized = normalizeFileRecord(record, { source: record?.source || "generated" });
      if (!normalized) continue;
      const key = normalized.openai_file_id || normalized.id;
      if (!key) continue;
      const existing = byId.get(key);
      byId.set(key, existing ? { ...existing, ...normalized } : normalized);
      changed = true;
    }
    if (!changed) return;
    const updated = Array.from(byId.values());
    saveCurrentSessionFiles(updated);
  }

  async ingestGeneratedFile(blob, filename = "assistant_output.pdf", previewUrl = null, source = "generated") {
    if (!blob) return null;
    const sessionId = window.current_session_id;
    if (!sessionId) {
      showToast("No active session to store generated files.", "error", 2500);
      return null;
    }
    const vectorId =
      window.current_vector_store_id ||
      window.sessionManager?.getVectorStoreId?.();
    const file = new File([blob], filename, { type: blob.type || "application/pdf" });
    const toast = showToast(`Saving ${filename}…`, "info", 0);
    try {
      const uploaded = await this.uploadFile(file, "assistants");
      if (vectorId) {
        try {
          await this.linkFileToVectorStore(uploaded.id, vectorId);
        } catch (err) {
          console.warn("Failed to link generated file to vector store:", err);
        }
      }
      const record = normalizeFileRecord({
        id: uploaded?.id,
        openai_file_id: uploaded?.id,
        name: filename,
        size: file.size,
        vector_store_id: vectorId || null,
        mime: file.type,
        source,
        preview_url: previewUrl || null,
        created_at: uploaded?.created_at || Date.now(),
      });
      if (record) {
        const current = getCurrentSessionFiles();
        current.push(record);
        saveCurrentSessionFiles(current);
      }
      dismissToast(toast);
      showToast(`Saved ${filename}`, "success", 1800);
      return record;
    } catch (err) {
      dismissToast(toast);
      console.error("Failed to save generated file:", err);
      showToast("Failed to save generated file", "error", 3000);
      return null;
    }
  }

}


// --- File Upload wiring ---
const fileUploadBtn = document.getElementById("openai-file-upload-btn");
const fileUploadInput = document.getElementById("openai-file-upload-input");
const uploadedFileList = document.getElementById("uploaded-file-list");

function expandFilesSection() {
  try {
    const collapseEl = document.getElementById("Filecollapse");
    const toggleBtn = document.getElementById("file-toggle");
    if (collapseEl && !collapseEl.classList.contains("show")) {
      collapseEl.classList.add("show");
    }
    if (toggleBtn) toggleBtn.setAttribute("aria-expanded", "true");
  } catch {}
}

function getCurrentSessionFiles() {
  const sessionEl = document.querySelector(`.chat-session-item[sessionID="${window.current_session_id}"]`);
  if (sessionEl) {
    try {
      return JSON.parse(sessionEl.getAttribute("file_ids") || "[]") || [];
    } catch {
      return [];
    }
  }
  const session = window.sessionManager?.getCurrentSession?.();
  return Array.isArray(session?.files) ? [...session.files] : [];
}

function saveCurrentSessionFiles(files = []) {
  const normalized = Array.isArray(files) ? files : [];
  const sessionEl = document.querySelector(`.chat-session-item[sessionID="${window.current_session_id}"]`);
  if (sessionEl) {
    sessionEl.setAttribute("file_ids", JSON.stringify(normalized));
  }
  const session = window.sessionManager?.getCurrentSession?.();
  if (session) {
    session.files = normalized;
    window.sessionManager.saveSessionsToLocal?.();
  }
  renderFileList(normalized);
  expandFilesSection();
}

function normalizeFileRecord(record = {}, defaults = {}) {
  const data = { ...defaults, ...record };
  const id = data.openai_file_id || data.id;
  if (!id) return null;
  return {
    id,
    openai_file_id: id,
    name: data.name || data.filename || id,
    size: data.size ?? data.bytes ?? null,
    vector_store_id: data.vector_store_id || null,
    container_file_id: data.container_file_id || null,
    preview_url: data.preview_url || null,
    mime: data.mime || data.mimetype || "",
    source: data.source || "upload",
    created_at: data.created_at || null,
  };
}

function closeAllFileMenus(except = null) {
  document.querySelectorAll(".file-menu-wrapper.open").forEach((wrapper) => {
    if (wrapper === except) return;
    wrapper.classList.remove("open");
    const menu = wrapper.querySelector(".file-menu");
    if (menu) {
      menu.hidden = true;
      menu.style.position = "";
      menu.style.top = "";
      menu.style.left = "";
      menu.style.width = "";
      menu.style.visibility = "";
    }
  });
}

document.addEventListener("click", (ev) => {
  if (ev.target.closest(".file-menu-wrapper")) return;
  closeAllFileMenus();
});

window.addEventListener("scroll", () => closeAllFileMenus(), { capture: true, passive: true });

// Button opens hidden input
fileUploadBtn?.addEventListener("click", () => {
  fileUploadInput?.click();
});

// Input change handles uploads
fileUploadInput?.addEventListener("change", async (e) => {
  const files = e.target.files || [];
  if (!files.length) return;

  const sessionId = window.current_session_id;
  if (!sessionId) {
    alert("No active session. Create one first!");
    return;
  }

  expandFilesSection();

  for (const file of files) {
    // --- Initialize tracking variables ---
    let toastUploading;
    let linkRes = null;
    let uploadedContainerFile = null;
    let previewUrl = null;
    try {
      previewUrl = URL.createObjectURL(file);
    } catch {}

    try {
      // 1️⃣ Upload to OpenAI Files
      toastUploading = showToast(`Uploading ${file.name}…`, "info", 0);
      const lowerName = (file.name || "").toLowerCase();
      const isImage =
        /\.(png|jpe?g|gif|bmp|webp|svg|tiff?|heic)$/.test(lowerName) ||
        (file.type || "").startsWith("image/");
      const purpose = isImage ? "vision" : "assistants";

      const uploaded = await window.fileManager.uploadFile(file, purpose);
      dismissToast(toastUploading);
      showToast(`Uploaded ${file.name}`, "success", 1500);
      console.log("[Upload] File uploaded:", uploaded.id);

      // 2️⃣ Link to Vector Store
      const vectorId =
        window.current_vector_store_id ||
        window.sessionManager?.getVectorStoreId?.();
      if (vectorId) {
        try {
          linkRes = await window.fileManager.linkFileToVectorStore(
            uploaded.id,
            vectorId
          );
          console.log(
            "[VectorStore] Linked file to vector store:",
            linkRes?.id || linkRes
          );
          showToast(`Linked ${file.name} to vector store`, "success", 1500);
        } catch (err) {
          console.warn("[VectorStore] Linking failed:", err);
          showToast(
            `Failed to link ${file.name} to vector store`,
            "error",
            3000
          );
        }
      }

      // 3️⃣ Ensure container only for analysis files
      const isAnalysisFile = /\.(csv|tsv|xlsx?|parquet|jsonl?|feather|arrow)$/i.test(
        file.name || ""
      );
      let containerID = window.current_container_id;

      if (!containerID && isAnalysisFile && window.sessionManager?.ensureContainer) {
        try {
          const toast = showToast("⚙️ Starting Python container…", "info", 0);
          containerID = await window.sessionManager.ensureContainer();
          dismissToast(toast);
          if (containerID)
            showToast("✅ Python runtime ready", "success", 1500);
        } catch (e) {
          showToast("⚠️ Could not start Python runtime", "error", 2500);
        }
      }

      // 4️⃣ Upload to container if one exists
      if (containerID) {
        try {
          const formData = new FormData();
          formData.append("file", file);
          const uploadRes = await fetch(
            apiUrl(`/v1/containers/${containerID}/files`),
            {
              method: "POST",
              body: formData,
            }
          );
          if (uploadRes.ok) {
            uploadedContainerFile = await uploadRes.json();
            console.log("[Container] File uploaded:", uploadedContainerFile.id);
            showToast(`Added ${file.name} to container`, "success", 1200);
          } else {
            console.warn(
              "[Container] Failed to upload to container:",
              uploadRes.status
            );
          }
        } catch (err) {
          console.warn("[Container] Upload error:", err);
        }
      }

      // 5️⃣ Record in session
      const fileRecordList = getCurrentSessionFiles();
      fileRecordList.push({
        id: uploaded.id,
        name: file.name,
        size: file.size,
        openai_file_id: uploaded.id,
        vector_store_id: vectorId || window.current_vector_store_id || null,
        container_file_id: uploadedContainerFile?.id || null,
        preview_url: previewUrl,
        mime: file.type || "",
      });
      saveCurrentSessionFiles(fileRecordList);
      renderFileList(fileRecordList);
      console.log("[Upload] Session file_ids updated:", fileRecordList);

      // 6️⃣ Optional: caption image and store in vector store
      if (isImage) {
        let capToast;
        try {
          capToast = showToast(`Captioning ${file.name}…`, "info", 0);
          const caption = await tryCaptionImage(
            uploaded.id,
            "Provide a concise, descriptive caption for this image (1-3 sentences)."
          );
          if (caption && caption.trim()) {
            const captionFile = new File([caption], `${file.name}.caption.txt`, {
              type: "text/plain",
            });
            const capUpload = await window.fileManager.uploadFile(captionFile);
            const vectorId2 =
              window.current_vector_store_id ||
              window.sessionManager?.getVectorStoreId?.();
            if (vectorId2 && capUpload?.id) {
              await window.fileManager.linkFileToVectorStore(
                capUpload.id,
                vectorId2
              );
              console.log("[Vision] Caption file linked:", capUpload.id);
              const captionRecord = normalizeFileRecord({
                id: capUpload.id,
                openai_file_id: capUpload.id,
                name: `${file.name}.caption.txt`,
                size: caption.length,
                vector_store_id: vectorId2,
                mime: "text/plain",
                source: "generated",
              });
              if (captionRecord) {
                const arr = getCurrentSessionFiles();
                arr.push(captionRecord);
                saveCurrentSessionFiles(arr);
                renderFileList(arr);
              }
              dismissToast(capToast);
              showToast(`Caption created for ${file.name}`, "success", 1800);
            }
          }
        } catch (err) {
          console.warn("[Vision] Caption failed:", err);
          if (capToast) dismissToast(capToast);
          showToast(`Failed to caption ${file.name}`, "error", 3000);
        }
      }
    } catch (err) {
      console.error("[Upload] Error:", err);
      if (toastUploading) dismissToast(toastUploading);
      showToast(`Upload failed: ${file.name}`, "error", 3500);
      if (previewUrl) {
        try { URL.revokeObjectURL(previewUrl); } catch {}
      }
    }
  }

  // Reset input so the same file can be reselected
  e.target.value = "";
});

// Delete, rename, download, and preview handler
uploadedFileList?.addEventListener("click", async (e) => {
  const menuBtn = e.target.closest(".file-menu-btn");
  if (menuBtn) {
    const wrapper = menuBtn.closest(".file-menu-wrapper");
    if (!wrapper) return;
    const isOpen = wrapper.classList.contains("open");
    closeAllFileMenus();
    if (!isOpen) {
      wrapper.classList.add("open");
      const menu = wrapper.querySelector(".file-menu");
      if (menu) {
        menu.hidden = false;
        menu.style.position = "fixed";
        menu.style.right = "auto";
        menu.style.left = "0px";
        menu.style.top = "0px";
        const prevVisibility = menu.style.visibility;
        menu.style.visibility = "hidden";
        const btnRect = menuBtn.getBoundingClientRect();
        const menuRect = menu.getBoundingClientRect();
        const vw = window.innerWidth || document.documentElement.clientWidth;
        const vh = window.innerHeight || document.documentElement.clientHeight;
        let top = btnRect.bottom;
        let left = Math.min(vw - 8 - menuRect.width, Math.max(8, btnRect.right - menuRect.width));
        if (top + menuRect.height > vh) {
          top = Math.max(8, btnRect.top - menuRect.height);
        }
        menu.style.top = `${top}px`;
        menu.style.left = `${left}px`;
        menu.style.width = `${menuRect.width}px`;
        menu.style.visibility = prevVisibility || "visible";
      }
    }
    return;
  }

  const renameBtn = e.target.closest(".file-rename");
  const deleteBtn = e.target.closest(".file-delete") || e.target.classList.contains("delete-file-btn");
  const downloadBtn = e.target.closest(".file-download");
  const item = e.target.closest(".uploaded-file-item");
  if (!item) return;

  const fileId = item.dataset.fileId;
  if (!fileId) return;

  const session = window.sessionManager?.getCurrentSession?.(); 
  const containerId = 
    window.current_container_id ||
    window.sessionManager?.getCurrentSession?.()?.container_id;
   
  // 🔍 Find matching file record in session
  let fileRecord = session?.files?.find(
    (f) => f.openai_file_id === fileId || f.id === fileId
  );
  // fallback: read from DOM if session.files is empty
  if (!fileRecord) {
    const sessionEl = document.querySelector(
      `.chat-session-item[sessionID="${window.current_session_id}"]`
    );
    if (sessionEl) {
      const arr = JSON.parse(sessionEl.getAttribute("file_ids") || "[]");
      fileRecord = arr.find(
        (f) => f.openai_file_id === fileId || f.id === fileId
      );
    }
  }
  const containerFileId = fileRecord?.container_file_id;

  if (renameBtn) {
    const currentName = item.dataset.name || fileRecord?.name || "Attachment";
    const newName = prompt("Rename file", currentName);
    if (newName && newName.trim()) {
      const trimmed = newName.trim();
      item.dataset.name = trimmed;
      const span = item.querySelector(".file-name");
      if (span) {
        span.textContent = trimmed;
        span.title = trimmed;
      }
      if (fileRecord) {
        fileRecord.name = trimmed;
      }
      const files = getCurrentSessionFiles().map((f) => {
        if ((f.openai_file_id || f.id) === fileId) {
          return { ...f, name: trimmed };
        }
        return f;
      });
      saveCurrentSessionFiles(files);
      renderFileList(files);
    }
    closeAllFileMenus();
    return;
  }

  if (downloadBtn) {
    closeAllFileMenus();
    const previewUrl = item.dataset.previewUrl;
    const name = item.dataset.name || fileRecord?.name || fileId;
    if (previewUrl) {
      try {
        const resp = await fetch(previewUrl);
        if (!resp.ok) throw new Error(`Download failed (${resp.status})`);
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = name;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
      } catch (err) {
        console.warn("Download failed:", err);
        showToast("⚠️ Download failed", "error", 2500);
      }
      return;
    }
    // Fallback: server proxy
    try {
      const res = await fetch(apiUrl(`/v1/files/${fileId}/content`));
      if (!res.ok) throw new Error(`Download failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.warn("Download failed:", err);
      showToast("⚠️ Download failed", "error", 2500);
    }
    return;
  }

  // Delete button
  if (deleteBtn) {
     try {
        showToast("🗑️ Deleting file...", "info", 1000);

        // 1️⃣ Delete from /v1/files
        await window.fileManager.deleteFile(fileId);

        // 2️⃣ Delete from container if available
        if (containerId && containerFileId) {
          try {
            await window.fileManager.deleteContainerFile(containerId, containerFileId);
            console.log(`[Container] Deleted file ${containerFileId} from container ${containerId}`);
          } catch (err) {
            console.warn("Failed to delete container file:", err);
          }
        }

        // 3️⃣ Remove from session + UI
        if (session?.files) {
        session.files = session.files.filter(
            (f) => f.openai_file_id !== fileId && f.id !== fileId
        );
        window.sessionManager.saveSessionsToLocal?.();
        }

        showToast("✅ File deleted", "success", 1500);
        console.log(`[Upload] Deleted file: ${fileId}`);
    } catch (err) {
        console.warn("❌ Failed to delete remote file:", err);
        showToast("⚠️ Failed to delete file", "error", 2500);
    }
    const updatedFiles = getCurrentSessionFiles().filter(
      (f) => f.openai_file_id !== fileId && f.id !== fileId
    );
    saveCurrentSessionFiles(updatedFiles);
    renderFileList(updatedFiles);
    closeAllFileMenus();
    // Revoke blob URL if present
    try { if (item.dataset.previewUrl) URL.revokeObjectURL(item.dataset.previewUrl); } catch {}
    return;
  }

  // Filename click → preview
  if (e.target.classList.contains("file-name")) {
    const name = item.dataset.name || e.target.textContent || 'File Preview';
    if (item.dataset.previewUrl) {
      // Use local blob URL when available (client-only preview)
      const mime = (item.dataset.mime || '').toLowerCase();
      if (mime.includes('pdf')) return showPreviewIframe(name, item.dataset.previewUrl, 'application/pdf');
      if (mime.startsWith('image/')) return showPreviewImage(name, item.dataset.previewUrl);
      if (mime.startsWith('text/')) {
        // Fetch text from blob URL
        try { const resp = await fetch(item.dataset.previewUrl); const txt = await resp.text(); return showPreviewText(name, txt); } catch { return showPreviewDownload(name, item.dataset.previewUrl); }
      }
      // Fallback: offer download
      return showPreviewDownload(name, item.dataset.previewUrl);
    }
    // If no local preview, show message (or add server proxy later)
    return showPreviewMessage(name, 'Preview not available for this file.');
  }
});

// --- File preview helpers ---
async function captionImageWithFileId(imageFileId, promptText) {
  const payload = {
    model: "gpt-4o",
    input: [
      {
        role: "user",
        content: [
          { type: "input_text", text: promptText || "Describe this image in detail." },
          { type: "input_image", image_file_id: imageFileId }
        ]
      }
    ]
  };
  try { console.log('[Vision] Payload (file_id):', JSON.stringify(payload)); } catch {}
  const res = await fetch(apiUrl("/v1/responses"), {
    method: "POST",
    headers: {
        "Content-Type": "application/json",
    },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    let raw = '';
    try { raw = await res.text(); } catch {}
    console.error('[Vision] Error response:', res.status, res.statusText, raw);
    let message = '';
    try { const j = JSON.parse(raw); message = j?.error?.message || ''; } catch {}
    throw new Error(message || raw || `Vision API error ${res.status}`);
  }
  const data = await res.json();
  return extractCaptionText(data);
}

async function captionImageWithUrl(imageFileId, promptText) {
  const imageUrl = apiUrl(`/v1/files/${imageFileId}/content`);
  const payload = {
    model: "gpt-4o",
    input: [
      {
        role: "user",
        content: [
          { type: "input_text", text: promptText || "Describe this image in detail." },
          { type: "input_image", image_url: imageUrl }
        ]
      }
    ]
  };
  try { console.log('[Vision] Payload (image_url):', JSON.stringify(payload)); } catch {}
  const res = await fetch(apiUrl("/v1/responses"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    let raw = '';
    try { raw = await res.text(); } catch {}
    console.error('[Vision] URL error response:', res.status, res.statusText, raw);
    let message = '';
    try { const j = JSON.parse(raw); message = j?.error?.message || ''; } catch {}
    throw new Error(message || raw || `Vision API error ${res.status}`);
  }
  const data = await res.json();
  return extractCaptionText(data);
}

function extractCaptionText(data) {
  let text = "";
  const outs = Array.isArray(data?.output) ? data.output : [];
  for (const o of outs) {
    if (Array.isArray(o?.content)) {
      const hit = o.content.find(v => typeof v?.text === 'string' && v.text.trim());
      if (hit) { text = hit.text.trim(); break; }
    }
    if (typeof o?.text === 'string' && o.text.trim()) { text = o.text.trim(); break; }
  }
  if (!text && Array.isArray(data?.output_text) && data.output_text[0]) {
    text = String(data.output_text[0]).trim();
  }
  if (!text && data?.choices?.[0]?.message?.content) {
    text = String(data.choices[0].message.content).trim();
  }
  return text;
}

// Removed base64 fallback helpers as requested

async function tryCaptionImage(imageFileId, promptText) {
  try {
    return await captionImageWithFileId(imageFileId, promptText);
  } catch (e) {
    console.warn('[Vision] file_id caption failed, trying image_url:', e?.message || e);
    return await captionImageWithUrl(imageFileId, promptText);
  }
}

function openModal(title) {
  const modal = document.getElementById('file-preview-modal');
  const body = document.getElementById('file-preview-body');
  const heading = document.getElementById('file-preview-title');
  if (!modal || !body || !heading) return null;
  body.innerHTML = '';
  heading.textContent = title || 'Preview';
  modal.style.display = 'flex';
  const close = document.getElementById('file-preview-close');
  const onEsc = (ev)=>{ if (ev.key === 'Escape'){ closeModal(); } };
  document.addEventListener('keydown', onEsc, { once: true });
  close?.addEventListener('click', closeModal, { once: true });
  modal.addEventListener('click', (ev)=>{ if (ev.target === modal) closeModal(); });
  return body;
}

function closeModal(){ const modal=document.getElementById('file-preview-modal'); if(modal) modal.style.display='none'; }

function showPreviewIframe(title, url, type){
  const body = openModal(title); if(!body) return;
  const iframe = document.createElement('iframe');
  iframe.src = url; iframe.style.width='100%'; iframe.style.height='100%'; iframe.style.border='0';
  iframe.type = type || 'application/pdf';
  body.appendChild(iframe);
}

function showPreviewImage(title, url){
  const body = openModal(title); if(!body) return;
  const img = document.createElement('img');
  img.src = url; img.style.maxWidth='100%'; img.style.maxHeight='100%'; img.style.objectFit='contain';
  body.appendChild(img);
}

function showPreviewText(title, text){
  const body = openModal(title); if(!body) return;
  const pre = document.createElement('pre');
  pre.style.margin='0'; pre.style.padding='12px'; pre.textContent = text;
  body.appendChild(pre);
}

function showPreviewMessage(title, message){
  const body = openModal(title); if(!body) return;
  const p = document.createElement('div');
  p.style.padding='12px'; p.textContent = message;
  body.appendChild(p);
}

function showPreviewDownload(title, url){
  const body = openModal(title); if(!body) return;
  const wrapper = document.createElement('div');
  wrapper.style.display = 'flex';
  wrapper.style.flexDirection = 'column';
  wrapper.style.alignItems = 'center';
  wrapper.style.justifyContent = 'center';
  wrapper.style.padding = '12px';

  const msg = document.createElement('div');
  msg.textContent = 'Preview not supported. You can download the file:';

  const a = document.createElement('a');
  a.href = url;
  a.download = 'file';
  a.textContent = 'Download';
  a.className = 'download-link';
  a.style.margin = '12px auto 0';

  wrapper.appendChild(msg);
  wrapper.appendChild(a);
  body.appendChild(wrapper);
}
