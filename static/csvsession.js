const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[ch]));
function friendlyError(value) {
  const message = String(value || "").toLowerCase();
  if (message.includes("đăng nhập") || message.includes("login")) return "Cần đăng nhập lại.";
  if (message.includes("hết lượt") || message.includes("quota")) return "Tạm hết lượt tạo ảnh. Hãy thử lại sau.";
  if (message.includes("ảnh 1")) return "Thiếu ảnh đầu tiên của bộ. Hãy tạo lại bộ.";
  return "Không tạo được ảnh. Hãy thử lại.";
}

let polling = null;
let lastSignature = "";
let library = [];
const picked = new Set();
let csvFile = null;
let csvHeaders = [];
let promptFields = [];
const viewer = $("#csv-image-viewer");
let viewerDocument = document;
try {
  if (window.parent !== window && window.parent.document?.body) {
    window.parent.document.getElementById("csv-image-viewer")?.remove();
    window.parent.document.body.appendChild(viewer);
    viewerDocument = window.parent.document;
  }
} catch (_) { /* Chạy độc lập nếu trang cha khác origin. */ }

function closeImageViewer() {
  viewer.hidden = true;
  viewer.querySelector("#csv-viewer-image").removeAttribute("src");
}
function openImageViewer(url, name) {
  viewer.querySelector("#csv-viewer-image").src = url;
  viewer.querySelector("#csv-viewer-name").textContent = name;
  const download = viewer.querySelector("#csv-viewer-download");
  download.href = url;
  download.download = name;
  viewer.hidden = false;
  viewer.querySelector("#csv-viewer-close").focus();
}
viewer.querySelector("#csv-viewer-close").onclick = closeImageViewer;
viewer.querySelector(".lightbox-backdrop").onclick = closeImageViewer;
viewerDocument.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !viewer.hidden) closeImageViewer();
});

function updateRunCount() {
  const hasChart = $("#csv-size-enabled").checked && $("#csv-size-field").value !== "__none__";
  $("#btn-start-csv").textContent = `▶ Tạo ảnh · ${promptFields.length} yêu cầu${hasChart ? " + bảng kích thước" : ""}`;
}

function renderCsvFields() {
  const chosen = new Set(promptFields);
  const ordered = [...promptFields, ...csvHeaders.filter((h) => !chosen.has(h))];
  $("#csv-field-list").innerHTML = ordered.map((header) => {
    const index = promptFields.indexOf(header);
    return `<div class="csvs-field"><input type="checkbox" data-field="${esc(header)}" ${index >= 0 ? "checked" : ""} aria-label="Chọn cột ${esc(header)}"><span>${esc(header)}</span>${index >= 0 ? `<button type="button" class="btn action-btn sm" data-up="${esc(header)}" ${index === 0 ? "disabled" : ""} title="Đưa lên">↑</button><button type="button" class="btn action-btn sm" data-down="${esc(header)}" ${index === promptFields.length - 1 ? "disabled" : ""} title="Đưa xuống">↓</button>` : ""}</div>`;
  }).join("");
  $("#csv-field-list").querySelectorAll("[data-field]").forEach((input) => {
    input.onchange = () => {
      const field = input.dataset.field;
      promptFields = input.checked ? [...promptFields, field] : promptFields.filter((h) => h !== field);
      renderCsvFields();
    };
  });
  for (const [direction, step] of [["up", -1], ["down", 1]]) {
    $("#csv-field-list").querySelectorAll(`[data-${direction}]`).forEach((button) => {
      button.onclick = () => {
        const index = promptFields.indexOf(button.dataset[direction]);
        const other = index + step;
        if (index < 0 || other < 0 || other >= promptFields.length) return;
        [promptFields[index], promptFields[other]] = [promptFields[other], promptFields[index]];
        renderCsvFields();
      };
    });
  }
  updateRunCount();
}

function toast(message, type = "info") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  $("#toast-container").appendChild(item);
  setTimeout(() => item.remove(), 4000);
}

async function loadLibrary() {
  try {
    const response = await fetch("/api/templates");
    if (!response.ok) throw new Error("Không tải được thư viện ảnh");
    const data = await response.json();
    library = data.items || [];
    for (const rel of [...picked]) {
      if (!library.some((item) => item.rel === rel)) picked.delete(rel);
    }
    renderLibrary();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderLibrary() {
  const grid = $("#attach-grid");
  grid.innerHTML = library.length ? library.map((item) => `
    <div class="csvs-thumb ${picked.has(item.rel) ? "picked" : ""}" data-rel="${esc(item.rel)}">
      <img src="${esc(item.url)}" loading="lazy" alt=""><span>${esc(item.name)}</span>
    </div>`).join("") : '<div class="csvs-wait">Chưa có ảnh trong thư viện.</div>';
  grid.querySelectorAll(".csvs-thumb").forEach((item) => {
    item.onclick = () => {
      picked.has(item.dataset.rel) ? picked.delete(item.dataset.rel) : picked.add(item.dataset.rel);
      renderLibrary();
    };
  });
  $("#attach-stat").textContent = `${picked.size}/${library.length} ảnh`;
  $("#btn-img").textContent = picked.size ? `Ảnh mẫu (${picked.size})` : "Thêm ảnh mẫu";
  const selected = $("#selected-attachments");
  selected.innerHTML = [...picked].map((rel) => {
    const item = library.find((entry) => entry.rel === rel);
    return item ? `<div class="attachment-chip"><img src="${esc(item.url)}" alt=""><span title="${esc(item.name)}">${esc(item.name)}</span><button type="button" data-remove="${esc(rel)}" aria-label="Gỡ ${esc(item.name)}" title="Gỡ ảnh đính kèm">×</button></div>` : "";
  }).join("");
  selected.querySelectorAll("[data-remove]").forEach((button) => {
    button.onclick = () => { picked.delete(button.dataset.remove); renderLibrary(); };
  });
}

$("#btn-img").onclick = () => {
  $("#attach-panel").hidden = !$("#attach-panel").hidden;
  if (!$("#attach-panel").hidden) loadLibrary();
};
$("#btn-close").onclick = () => { $("#attach-panel").hidden = true; };
$("#btn-none").onclick = () => { picked.clear(); renderLibrary(); };
$("#btn-upload").onclick = () => $("#img-input").click();
$("#img-input").onchange = async (event) => {
  const files = [...event.target.files];
  event.target.value = "";
  if (!files.length) return;
  const body = new FormData();
  files.forEach((file) => {
    body.append("files", file);
    body.append("paths", file.webkitRelativePath || "");
  });
  try {
    const response = await fetch("/api/templates/upload", { method: "POST", body });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Không tải được ảnh");
    for (const rel of data.saved || []) picked.add(rel);
    toast(`Đã tải ${(data.saved || []).length} ảnh`, "success");
    await loadLibrary();
  } catch (error) {
    toast(error.message, "error");
  }
};

$("#btn-csv").onclick = () => $("#csv-input").click();
$("#csv-input").onchange = async (event) => {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  csvFile = null;
  const button = $("#btn-csv");
  button.disabled = true;
  button.textContent = "Đang đọc cột…";
  try {
    const body = new FormData();
    body.append("file", file);
    const response = await fetch("/api/csv-session/preview", { method: "POST", body });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Không đọc được file");
    csvFile = file;
    csvHeaders = (data.headers || []).filter(Boolean);
    const key = `csv-session-fields:${JSON.stringify(csvHeaders)}`;
    let saved;
    try { saved = JSON.parse(localStorage.getItem(key) || "null"); } catch (_) { saved = null; }
    promptFields = Array.isArray(saved?.prompts)
      ? saved.prompts.filter((h) => csvHeaders.includes(h)) : (data.suggested_prompts || []);
    const size = $("#csv-size-field");
    size.innerHTML = `<option value="__none__">Chọn cột kích thước</option>` +
      csvHeaders.map((h) => `<option value="${esc(h)}">${esc(h)}</option>`).join("");
    size.value = data.suggested_size_chart || "__none__";
    $("#csv-size-enabled").checked = false;
    size.disabled = true;
    $("#csv-file-name").textContent = file.name;
    $("#csv-row-count").textContent = `· ${data.rows} hàng · ${csvHeaders.length} cột`;
    $("#csv-field-panel").hidden = false;
    renderCsvFields();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Chọn bảng dữ liệu";
  }
};

$("#csv-size-enabled").onchange = () => {
  $("#csv-size-field").disabled = !$("#csv-size-enabled").checked;
  updateRunCount();
};
$("#csv-size-field").onchange = updateRunCount;

$("#btn-start-csv").onclick = async () => {
  if (!csvFile) return toast("Hãy chọn bảng dữ liệu", "error");
  if (!promptFields.length) return toast("Hãy chọn ít nhất một cột nội dung", "error");
  const button = $("#btn-start-csv");
  button.disabled = true;
  try {
    const sizeField = $("#csv-size-enabled").checked
      ? $("#csv-size-field").value : "__none__";
    if ($("#csv-size-enabled").checked && sizeField === "__none__") {
      throw new Error("Hãy chọn cột thông tin kích thước");
    }
    const body = new FormData();
    body.append("file", csvFile);
    promptFields.forEach((field) => body.append("prompt_fields", field));
    body.append("size_chart_field", sizeField);
    picked.forEach((rel) => body.append("templates", rel));
    body.append("skip_done", $("#skip-done").checked ? "true" : "false");
    const response = await fetch("/api/csv-session/start", { method: "POST", body });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Không chạy được");
    localStorage.setItem(`csv-session-fields:${JSON.stringify(csvHeaders)}`,
      JSON.stringify({ prompts: promptFields }));
    toast(`Đang tạo ảnh cho ${data.started} hàng`, "success");
    startPolling();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
};

$("#btn-resume").onclick = async () => {
  const button = $("#btn-resume");
  button.disabled = true;
  try {
    const response = await fetch("/api/csv-session/resume", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Không chạy tiếp được");
    toast(data.resumed ? `Chạy tiếp ${data.resumed} hàng` : data.message || "Không còn hàng dở",
          data.resumed ? "success" : "info");
    if (data.resumed) startPolling();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    refresh();
  }
};

$("#btn-stop").onclick = async () => {
  const button = $("#btn-stop");
  button.disabled = true;
  try {
    const response = await fetch("/api/jobs/stop", { method: "POST" });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.detail || "Không dừng được");
    toast("Đã dừng", "warn");
    startPolling();
  } catch (error) {
    toast(error.message || "Không dừng được", "error");
  } finally {
    button.disabled = false;
  }
};
$("#btn-refresh").onclick = () => refresh(true);

function renderWarnings(data) {
  const warnings = data.warnings || [];
  $("#warning-panel").hidden = !warnings.length;
  $("#warning-panel").textContent = warnings.length
    ? "Có vấn đề khi tạo ảnh. Hãy xem bộ bị lỗi để biết thêm." : "";
}

function statusText(status) {
  return ({pending: "Chờ", running: "Đang tạo", done: "Xong", failed: "Có lỗi",
    partial: "Chưa đủ ảnh", paused: "Tạm dừng"})[status] || "Đang chờ";
}

function renderCollections(collections, active) {
  const signature = JSON.stringify(collections.map((row) => [row.id, row.name, row.status, row.queue_position,
    row.error, row.done_count, row.total_count, row.can_regenerate_image,
    row.jobs.map((job) => [job.status, job.results, job.error, job.regen_status, job.regen_error])])) + active;
  if (signature === lastSignature) return;
  lastSignature = signature;
  $("#list").innerHTML = collections.map((row) => {
    const results = row.jobs.flatMap((job) => job.results || []);
    const canRetry = ["failed", "paused", "partial"].includes(row.status);
    const canRegenerate = !["pending", "running"].includes(row.status);
    const canDelete = canRegenerate && !row.jobs.some((job) =>
      ["pending", "running"].includes(job.regen_status));
    const progress = `${row.done_count}/${row.total_count}`;
    const status = row.status === "done" ? "" :
      ` · ${row.queue_position ? `Chờ lượt ${row.queue_position}` : statusText(row.status)}`;
    return `<div class="collection-card ${esc(row.status)}">
      <div class="collection-header"><div class="collection-title-group">
        <span class="collection-folder-name">${esc(row.name)}${row.regeneration > 1 ? ` · lần ${row.regeneration}` : ""}</span></div>
        <div class="csvs-card-actions"><span class="collection-stat-badge" title="${statusText(row.status)}">${progress}${esc(status)}</span>
          <details class="row-action-menu"><summary title="Thao tác với bộ này" aria-label="Thao tác với bộ này">⋯</summary><div>
          <button class="csvs-retry" data-cid="${esc(row.id)}" ${canRetry ? "" : "disabled"}>Sửa lỗi</button>
          <button class="csvs-regenerate" data-cid="${esc(row.id)}" ${canRegenerate ? "" : "disabled"}>↻ Tạo lại bộ</button>
          <a href="/api/jobs/zip?cid=${encodeURIComponent(row.id)}" ${results.length ? "" : 'aria-disabled="true" tabindex="-1"'}>↓ Tải ảnh</a>
          </div></details><button type="button" class="btn action-btn sm csvs-delete" data-cid="${esc(row.id)}" data-name="${esc(row.name)}" ${canDelete ? "" : "disabled"} title="Xoá bộ này" aria-label="Xoá bộ ${esc(row.name)}">Xoá bộ</button>
        </div></div>
      <div class="csvs-prompt-grid">${row.jobs.map((job) => `<div class="csvs-prompt ${esc(job.status)}"><div class="csvs-prompt-head">
        <span class="csvs-prompt-name" title="${esc(job.prompt_name)}">${esc(job.prompt_name)}</span>
        ${job.status === "done" ? '<span class="csvs-done-mark" title="Hoàn thành" aria-label="Hoàn thành">✓</span>' : `<span class="collection-stat-badge">${statusText(job.status)}</span>`}</div>
        ${(job.results || []).length ? `<div class="csvs-thumbs">${job.results.map((url, index) => `
          <div class="csvs-thumb csvs-result"><button type="button" class="csvs-preview-link" data-url="${esc(url)}" aria-label="Xem ảnh ${esc(url.split("/").pop())}">
            <img src="${esc(url)}" loading="lazy" alt=""><span>${esc(url.split("/").pop())}</span></button>
            <button type="button" class="csvs-regen-image" data-cid="${esc(row.id)}" data-jid="${esc(job.id)}" data-index="${index}" ${row.can_regenerate_image && !["pending", "running"].includes(row.status) && !["pending", "running"].includes(job.regen_status) ? "" : "disabled"} title="${row.can_regenerate_image ? "Tạo lại ảnh này" : "Ảnh này chưa thể tạo lại"}" aria-label="Tạo lại ảnh này">↻</button></div>`).join("")}</div>`
          : `<div class="csvs-wait">${job.status === "running" ? "Đang tạo…" : job.status === "failed" ? "Lỗi tạo ảnh" : "Chờ ảnh"}</div>`}
        ${job.regen_status === "pending" || job.regen_status === "running" ? `<div class="csvs-wait">Đang tạo lại…</div>` : ""}
        ${job.regen_error ? `<div class="csvs-err">${esc(friendlyError(job.regen_error))}</div>` : ""}
        ${job.error ? `<div class="csvs-err">${esc(friendlyError(job.error))}</div>` : ""}</div>`).join("")}</div>
      ${row.error && !row.jobs.some((job) => job.error) ? `<div class="csvs-err">${esc(friendlyError(row.error))}</div>` : ""}</div>`;
  }).join("");
}

$("#list").onclick = async (event) => {
  const preview = event.target.closest(".csvs-preview-link");
  if (preview) {
    openImageViewer(preview.dataset.url, preview.dataset.url.split("/").pop());
    return;
  }
  const imageButton = event.target.closest(".csvs-regen-image");
  if (imageButton) {
    if (imageButton.disabled) return;
    imageButton.disabled = true;
    try {
      const response = await fetch(`/api/csv-session/${encodeURIComponent(imageButton.dataset.cid)}/jobs/${encodeURIComponent(imageButton.dataset.jid)}/regenerate-image`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_index: Number(imageButton.dataset.index) }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Không gen lại được ảnh");
      toast("Đã xếp hàng tạo lại ảnh", "success");
      startPolling();
    } catch (error) {
      toast(error.message, "error");
      imageButton.disabled = false;
    }
    return;
  }
  const deleteButton = event.target.closest(".csvs-delete");
  if (deleteButton) {
    if (deleteButton.disabled) return;
    if (!confirm(`Xoá bộ “${deleteButton.dataset.name}” và ảnh của bộ này?\nKhông thể hoàn tác.`)) return;
    deleteButton.disabled = true;
    try {
      const response = await fetch(`/api/csv-session/${encodeURIComponent(deleteButton.dataset.cid)}`, {method: "DELETE"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Không xoá được bộ");
      toast(`Đã xoá bộ và ${data.deleted_files || 0} ảnh`, "success");
      lastSignature = "";
      await refresh();
    } catch (error) {
      toast(error.message, "error");
      deleteButton.disabled = false;
    }
    return;
  }
  const button = event.target.closest(".csvs-retry, .csvs-regenerate");
  if (!button || button.disabled) return;
  const regenerate = button.classList.contains("csvs-regenerate");
  button.disabled = true;
  try {
    const response = await fetch(regenerate
      ? `/api/collections/${encodeURIComponent(button.dataset.cid)}/regenerate`
      : `/api/csv-session/${encodeURIComponent(button.dataset.cid)}/resume`,
      { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Không chạy lại được hàng này");
    toast(regenerate ? "Đã tạo lượt mới; ảnh cũ được giữ nguyên" : "Đã xếp hàng sửa lỗi", "success");
    startPolling();
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
  }
};

async function refresh(manual = false) {
  try {
    const response = await fetch("/api/jobs");
    if (!response.ok) throw new Error("Không cập nhật được kết quả");
    const data = await response.json();
    const collections = (data.collections || []).filter((row) => row.free_sequence);
    const done = collections.filter((row) => row.status === "done").length;
    const finished = collections.filter((row) => ["done", "failed", "partial"].includes(row.status)).length;
    const images = collections.reduce((sum, row) => sum + row.jobs.reduce(
      (n, job) => n + (job.results || []).length, 0), 0);
    $("#results-panel").hidden = !collections.length;
    const canResume = !data.active && collections.some((row) =>
      ["failed", "paused", "partial"].includes(row.status));
    $("#btn-stop").disabled = !data.active;
    $("#btn-stop").hidden = !data.active;
    $("#btn-resume").disabled = !canResume;
    $("#btn-resume").hidden = !canResume;
    const zip = $("#btn-download-all");
    zip.setAttribute("aria-disabled", images ? "false" : "true");
    zip.tabIndex = images ? 0 : -1;
    renderWarnings(data);
    if (collections.length) {
      $("#stat").textContent = `${done}/${collections.length} hàng · ${images} ảnh`;
      $("#run-badge").textContent = `${done}/${collections.length} hàng`;
      $("#run-fill").style.width = `${Math.round(finished / collections.length * 100)}%`;
      $("#run-text").textContent = data.active ? `Đang chạy · còn ${collections.length - finished} hàng`
        : data.stopped ? `Đã dừng · còn ${collections.length - finished} hàng`
        : finished === collections.length ? "Đã kết thúc" : `Tạm dừng · còn ${collections.length - finished} hàng`;
      $("#run-strip").classList.toggle("is-running", !!data.active);
      renderCollections(collections, data.active);
    } else {
      lastSignature = "";
    }
    if (!data.active && polling) {
      clearInterval(polling);
      polling = null;
    }
    if (manual) toast("Đã làm mới trạng thái", "success");
  } catch (error) {
    if (manual) toast(error.message, "error");
  }
}

function startPolling() {
  if (polling) clearInterval(polling);
  polling = setInterval(refresh, 2200);
  refresh();
}

loadLibrary();
startPolling();
