// Frontend Template Manager: upload, delete, and search template manifest entries.

const tableBody = document.querySelector("#templateTable tbody");
const statusEl = document.querySelector("#status");
const manifestStoreChip = document.querySelector("#manifestVectorStoreId");
const searchInput = document.querySelector("#searchInput");
const fileInput = document.querySelector("#fileInput");
const refreshBtn = document.querySelector("#refreshBtn");

let manifest = [];
let manifestVectorStoreId = null;
let manifestVectorStoreName = null;
let sortKey = "file_name";
let sortDir = "asc";

function setStatus(message, tone = "info") {
  if (!statusEl) return;
  statusEl.textContent = message || "";
  statusEl.style.color = tone === "error" ? "#f87171" : "#e5e7eb";
}

function renderVectorStores() {
  if (manifestStoreChip) {
    const label = manifestVectorStoreName || manifestVectorStoreId || "n/a";
    manifestStoreChip.textContent = `Manifest VS: ${label}`;
  }
}

function applySort(data) {
  const copy = [...data];
  copy.sort((a, b) => {
    const av = (a?.[sortKey] || "").toLowerCase();
    const bv = (b?.[sortKey] || "").toLowerCase();
    if (av === bv) return 0;
    return sortDir === "asc" ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
  });
  return copy;
}

function renderTable() {
  if (!tableBody) return;
  const query = (searchInput?.value || "").toLowerCase();
  let rows = manifest;
  if (query) {
    rows = rows.filter((r) =>
      (r.file_name || "").toLowerCase().includes(query) ||
      (r.file_id || "").toLowerCase().includes(query)
    );
  }
  rows = applySort(rows);
  tableBody.innerHTML = "";
  console.debug("Rendering table with", rows.length, "rows");
  for (const row of rows) {
    const tr = document.createElement("tr");
    const uploaded = row.uploaded_at ? String(row.uploaded_at).slice(0, 10) : "";
    const needsInfo = typeof row.file_info === "string" && row.file_info.toLowerCase().includes("generation unavailable");
    const statusIcon = needsInfo ? `<span class="status-error" title="file_info generation failed, please reupload">×</span>` : "";
    tr.innerHTML = `
      <td>${statusIcon}${row.file_name || ""}</td>
      <td>${uploaded}</td>
      <td class="muted">${row.file_id || ""}</td>
      <td>
        <button class="btn secondary" data-delete="${row.file_name}">Delete</button>
      </td>
    `;
    tableBody.appendChild(tr);
  }
}

async function fetchManifest() {
  setStatus("Loading manifest…");
  const res = await fetch("/v1/templates/manifest");
  if (!res.ok) {
    console.warn("Manifest fetch failed with status", res.status);
    setStatus("Failed to load manifest.", "error");
    return;
  }
  const data = await res.json();
  console.debug("Manifest payload:", data);
  manifest = Array.isArray(data?.manifest) ? data.manifest : [];
  manifestVectorStoreId = data?.manifest_vector_store_id || null;
  manifestVectorStoreName = data?.manifest_vector_store_name || null;
  renderVectorStores();
  renderTable();
  setStatus(`Loaded ${manifest.length} template(s).`);
}

async function uploadTemplates(files) {
  if (!files?.length) return;
  console.debug("Starting upload for", files.length, "file(s)");
  setStatus("Uploading templates...");
  const spinner = document.querySelector("#uploadSpinner");
  if (spinner) spinner.style.display = "flex";
  for (const file of files) {
    const fd = new FormData();
    fd.append("files", file);
    if (manifestVectorStoreId) fd.append("vector_store_id", manifestVectorStoreId);
    try {
      const res = await fetch("/v1/templates/upload", {
        method: "POST",
        body: fd,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        console.warn("Upload failed for", file.name, err);
        setStatus(err?.error || `Upload failed for ${file.name}`, "error");
        continue;
      }
      const data = await res.json();
      console.debug("Upload response for", file.name, data);
      const last = Array.isArray(data?.manifest) ? data.manifest[data.manifest.length - 1] : null;
      if (last && last.file_info && last.file_info.includes("generation unavailable")) {
        setStatus(`Upload complete, but file_info failed for ${file.name}. Please reupload to retry.`, "error");
      }
      manifest = Array.isArray(data?.manifest) ? data.manifest : manifest;
      manifestVectorStoreId = data?.manifest_vector_store_id || manifestVectorStoreId;
      renderVectorStores();
      renderTable();
    } catch (err) {
      console.warn("Upload error for", file.name, err);
      setStatus(`Upload failed for ${file.name}`, "error");
    }
  }
  setStatus("Upload complete.");
  console.debug("Finished upload batch");
  if (spinner) spinner.style.display = "none";
}

async function deleteTemplate(fileName) {
  if (!fileName) return;
  setStatus(`Deleting ${fileName}...`);
  const res = await fetch(`/v1/templates/${encodeURIComponent(fileName)}` + (manifestVectorStoreId ? `?vector_store_id=${encodeURIComponent(manifestVectorStoreId)}` : ""), {
    method: "DELETE",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.warn("Delete failed:", err);
    setStatus(err?.error || "Delete failed.", "error");
    return;
  }
  const data = await res.json();
  console.debug("Delete response:", data);
  manifest = Array.isArray(data?.manifest) ? data.manifest : manifest;
  renderTable();
  setStatus(`${fileName} removed.`);
  console.debug("Deleted", fileName, "remaining:", manifest.length);
}

function bindEvents() {
  document.addEventListener("click", (e) => {
    const target = e.target;
    if (target?.dataset?.delete) {
      deleteTemplate(target.dataset.delete);
    }
  });

  const headers = document.querySelectorAll("th[data-sort]");
  headers.forEach((h) => {
    h.addEventListener("click", () => {
      const key = h.dataset.sort;
      if (sortKey === key) {
        sortDir = sortDir === "asc" ? "desc" : "asc";
      } else {
        sortKey = key;
        sortDir = "asc";
      }
      renderTable();
    });
  });

  if (searchInput) {
    searchInput.addEventListener("input", renderTable);
  }
  if (fileInput) {
    fileInput.addEventListener("change", (e) => uploadTemplates(e.target.files));
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", fetchManifest);
  }
}

bindEvents();
fetchManifest().catch((err) => {
  console.error(err);
  setStatus("Failed to initialize template manager.", "error");
});
