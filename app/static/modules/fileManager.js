// modules/fileManager.js
import { fetchWithDiagnostics } from "./utils.js";
import { showToast, dismissToast } from "./ui.js";

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
    return await fetchWithDiagnostics(apiUrl(`/v1/containers/${containerId}/files/${containerFileId}`), {
        method: "DELETE",
    })
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
    // --- UI setup ---
    const listItem = document.createElement("div");
    listItem.className = "uploaded-file-item";
    listItem.innerHTML = `
      <span title="${file.name}">${file.name}</span>
      <button class="delete-file-btn" title="Delete file">×</button>
    `;
    listItem.dataset.name = file.name;
    listItem.dataset.mime = file.type || "";
    try {
      listItem.dataset.previewUrl = URL.createObjectURL(file);
    } catch {}
    uploadedFileList?.appendChild(listItem);

    // --- Initialize tracking variables ---
    let toastUploading;
    let linkRes = null;
    let uploadedContainerFile = null;

    try {
      // 1️⃣ Upload to OpenAI Files
      toastUploading = showToast(`Uploading ${file.name}…`, "info", 0);
      const lowerName = (file.name || "").toLowerCase();
      const isImage =
        /\.(png|jpe?g|gif|bmp|webp|svg|tiff?|heic)$/.test(lowerName) ||
        (file.type || "").startsWith("image/");
      const purpose = isImage ? "vision" : "assistants";

      const uploaded = await window.fileManager.uploadFile(file, purpose);
      listItem.dataset.fileId = uploaded.id;
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
      const sessionEl = document.querySelector(
        `.chat-session-item[sessionID="${sessionId}"]`
      );
      if (sessionEl) {
        const fileRecord =
          JSON.parse(sessionEl.getAttribute("file_ids") || "[]") || [];
        fileRecord.push({
          id: uploaded.id,
          name: file.name,
          size: file.size,
          openai_file_id: uploaded.id,
          vector_store_id: vectorId || window.current_vector_store_id || null,
          container_file_id: uploadedContainerFile?.id || null,
        });
        sessionEl.setAttribute("file_ids", JSON.stringify(fileRecord));
        console.log("[Upload] Session file_ids updated:", fileRecord);
      }

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
              const sessionEl2 = document.querySelector(
                `.chat-session-item[sessionID="${sessionId}"]`
              );
              if (sessionEl2) {
                const arr = JSON.parse(
                  sessionEl2.getAttribute("file_ids") || "[]"
                );
                arr.push({
                  id: capUpload.id,
                  name: `${file.name}.caption.txt`,
                  size: caption.length,
                });
                sessionEl2.setAttribute("file_ids", JSON.stringify(arr));
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
      listItem.remove();
    }
  }

  // Reset input so the same file can be reselected
  e.target.value = "";
});

// Delete and preview handler
uploadedFileList?.addEventListener("click", async (e) => {
  const item = e.target.closest(".uploaded-file-item");
  if (!item) return;

  const fileId = item.dataset.fileId;
  console.log(`This is fileId: ${fileId}`) //Debug
  if (!fileId) return;

  const session = window.sessionManager?.getCurrentSession?.(); 
  const containerId = 
    window.current_container_id ||
    window.sessionManager?.getCurrentSession?.()?.container_id;
   
  // 🔍 Find matching file record in session to get container_file_id
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
  console.log(`This is fileRecord: ${fileRecord}`) //Debug
  const containerFileId = fileRecord?.container_file_id
  console.log(`This is containerFileId: ${containerFileId}`) //Debug


  // Delete button
  if (e.target.classList.contains("delete-file-btn")) {
     try {
        showToast("🗑️ Deleting file...", "info", 1000);

        // 1️⃣ Delete from /v1/files
        await window.fileManager.deleteFile(fileId);

        console.log("DEBUG Delete context:", {
            fileId,
            containerId,
            sessionFiles: session?.files,
            fileRecord,
            containerFileId
        });
        // 2️⃣ Delete from container if one exists
        if (containerId && containerFileId) {
            await window.fileManager.deleteContainerFile(containerId, containerFileId);
            console.log(`[Container] Deleted file ${containerFileId} from container ${containerId}`);
        } else {
            console.log(`[Container] Skipped container delete (no container found)`);
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
    try {
      const sessionEl = document.querySelector(`.chat-session-item[sessionID="${window.current_session_id}"]`);
      if (sessionEl) {
        const arr = JSON.parse(sessionEl.getAttribute('file_ids') || '[]').filter(f => f.id !== fileId);
        sessionEl.setAttribute('file_ids', JSON.stringify(arr));
        console.log("[Upload] Session file_ids after deletion:", arr);
      }
    } catch {}
    // Revoke blob URL if present
    try { if (item.dataset.previewUrl) URL.revokeObjectURL(item.dataset.previewUrl); } catch {}
    item.remove();
    return;
  }

  // Filename click → preview
  if (e.target.tagName && e.target.tagName.toLowerCase() === 'span') {
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
