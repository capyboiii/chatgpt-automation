// ==========================================================================
// Trang BIẾN THỂ: 1 ảnh gốc + 1 prompt -> N ảnh, gộp chung MỘT thư mục.
//
// Tách hẳn khỏi app.js của trang chính: hai trang không dùng chung biến nào, nên
// sửa trang này không thể làm hỏng luồng gen theo bộ đã chạy ổn.
//
// Phía sau vẫn là đúng worker pool cũ (/api/collections/generate) và đúng quy ước
// thư mục <Loại>__<Topic>__<Design>. Điểm khác duy nhất: N job cùng trỏ vào MỘT
// ảnh gốc, nên chatgpt_pool chỉ đính kèm một bản mà vẫn chờ đủ N ảnh trả về.
// ==========================================================================

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  images: [],          // toàn bộ ảnh trong thư viện (phẳng, không phân bộ)
  pickedImage: null,   // rel của ảnh gốc đang chọn
  prompts: [],
  pickedPrompts: new Set(),
  profiles: [],
  collections: [],
  polling: null,
  filter: "",
};

const _sig = {};
function changed(key, data) {
  const s = JSON.stringify(data);
  if (_sig[key] === s) return false;
  _sig[key] = s;
  return true;
}

// -------------------------------------------------------------------- toast
function showToast(msg, type = "info") {
  const box = $("#toast-container");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// -------------------------------------------------------------------- theme
const savedTheme = localStorage.getItem("theme");
if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);
$("#theme-toggle")?.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
});

// ==========================================================================
// 1. Thư viện ảnh
// ==========================================================================

async function loadImages() {
  try {
    const d = await (await fetch("/api/templates")).json();
    // Trang này không quan tâm ảnh thuộc bộ nào - chỉ cần một danh sách phẳng.
    state.images = d.items || [];
    if (state.pickedImage && !state.images.some(i => i.rel === state.pickedImage)) {
      state.pickedImage = null;
    }
    renderImages();
  } catch (e) {
    console.error("Lỗi load ảnh:", e);
  }
}

function pickedItem() {
  return state.images.find(i => i.rel === state.pickedImage) || null;
}

function markImagePicked() {
  $("#img-grid")?.querySelectorAll(".var-img").forEach(el => {
    el.classList.toggle("picked", el.dataset.rel === state.pickedImage);
  });
  const el = $("#img-badge .stat-val");
  const it = pickedItem();
  if (el) el.textContent = it ? it.name : `${state.images.length} ảnh`;
  updateRunbar();
}

function renderImages() {
  const grid = $("#img-grid");
  if (!grid) return;

  const q = state.filter.toLowerCase().trim();
  const shown = q ? state.images.filter(i => i.name.toLowerCase().includes(q))
                  : state.images;

  if (shown.length === 0) {
    _sig.img = null;
    grid.innerHTML = `<div class="empty-state">Chưa có ảnh nào. Bấm <b>Tải ảnh</b> để thêm.</div>`;
    markImagePicked();
    return;
  }

  // Chọn ảnh nào không nằm trong chữ ký: đổi lựa chọn thì sửa class tại chỗ,
  // dựng lại cả lưới sẽ làm mọi thẻ chạy lại hiệu ứng và chớp cả màn hình.
  if (!changed("img", shown.map(i => i.rel))) {
    markImagePicked();
    return;
  }

  grid.innerHTML = shown.map((it, i) => `
    <button type="button" class="var-img ${it.rel === state.pickedImage ? "picked" : ""}"
            data-rel="${esc(it.rel)}" style="--i:${Math.min(i, 14)}" title="${esc(it.name)}">
      <img src="${it.url}" loading="lazy" alt="${esc(it.name)}">
      <span class="var-img-tick">✓</span>
      <span class="var-img-name">${esc(it.name)}</span>
    </button>
  `).join("");

  grid.querySelectorAll(".var-img").forEach(el => {
    el.addEventListener("click", () => {
      // Bấm lại vào ảnh đang chọn thì bỏ chọn.
      state.pickedImage = state.pickedImage === el.dataset.rel ? null : el.dataset.rel;
      markImagePicked();
    });
  });

  markImagePicked();
}

$("#img-search")?.addEventListener("input", (e) => {
  state.filter = e.target.value;
  renderImages();
});

const fileInput = $("#file-input");
$("#pick-files")?.addEventListener("click", () => fileInput?.click());
fileInput?.addEventListener("change", () => uploadImages(fileInput.files));

async function uploadImages(list) {
  const files = [...list].filter(f => /\.(png|jpe?g|webp)$/i.test(f.name));
  if (!files.length) return showToast("Không có file ảnh hợp lệ", "error");

  const fd = new FormData();
  files.forEach(f => {
    fd.append("files", f);
    fd.append("paths", f.webkitRelativePath || "");
  });
  showToast(`Đang tải lên ${files.length} ảnh...`, "info");
  try {
    await fetch("/api/templates/upload", { method: "POST", body: fd });
    fileInput.value = "";
    showToast(`Đã tải lên ${files.length} ảnh`, "success");
    loadImages();
  } catch (e) {
    showToast("Lỗi khi tải ảnh", "error");
  }
}

// ==========================================================================
// 2. Prompt
// ==========================================================================

async function loadPrompts() {
  try {
    const d = await (await fetch("/api/prompts")).json();
    state.prompts = d.prompts || d.items || [];
    renderPrompts();
  } catch (e) {
    console.error("Lỗi load prompts:", e);
  }
}

function markPromptPicked() {
  $("#prompt-list")?.querySelectorAll(".prompt-card").forEach(card => {
    card.classList.toggle("selected", state.pickedPrompts.has(card.dataset.id));
  });
  const badge = $("#prompt-badge");
  const cnt = $("#prompt-badge-count");
  const n = state.pickedPrompts.size;
  if (badge) badge.hidden = n === 0;
  if (cnt && n) cnt.textContent = `${n} prompt`;
  updateRunbar();
}

function renderPrompts() {
  const list = $("#prompt-list");
  if (!list) return;

  if (state.prompts.length === 0) {
    list.innerHTML = `<div class="empty-state">Chưa có prompt. Tạo ở <a href="/">trang chính</a>.</div>`;
    return;
  }

  if (!changed("prompt", state.prompts.map(p => [p.id, p.name, p.text]))) {
    markPromptPicked();
    return;
  }

  list.innerHTML = state.prompts.map((p, i) => `
    <div class="prompt-card prompt-item ${state.pickedPrompts.has(p.id) ? "selected" : ""}"
         data-id="${esc(p.id)}" style="--i:${Math.min(i, 14)}">
      <span class="prompt-index">${i + 1}</span>
      <span class="prompt-card-chk"></span>
      <div class="prompt-content">
        <div class="prompt-title">${esc(p.name)}</div>
        <div class="prompt-snippet">${esc(p.text)}</div>
      </div>
    </div>
  `).join("");

  list.querySelectorAll(".prompt-card").forEach(card => {
    card.addEventListener("click", () => {
      const id = card.dataset.id;
      if (state.pickedPrompts.has(id)) state.pickedPrompts.delete(id);
      else state.pickedPrompts.add(id);
      markPromptPicked();
    });
  });

  markPromptPicked();
}

// ==========================================================================
// 3. Tài khoản (chỉ để đếm, luôn chạy trên tất cả)
// ==========================================================================

async function loadProfiles() {
  try {
    const d = await (await fetch("/api/profiles")).json();
    state.profiles = d.profiles || [];
    const el = $("#pool-info");
    if (el) el.textContent = `${state.profiles.length} tài khoản`;
  } catch (e) { /* vòng sau hỏi lại */ }
}

function allProfileNames() {
  const ready = state.profiles.filter(p => p.exists).map(p => p.name);
  return ready.length ? ready : state.profiles.map(p => p.name);
}

// ==========================================================================
// 4. Chạy
// ==========================================================================

function variantCount() {
  const n = parseInt($("#var-count")?.value, 10);
  return Number.isFinite(n) && n > 0 ? Math.min(n, 10) : 2;
}

function updateRunbar() {
  const btn = $("#run-btn");
  const txt = $("#run-btn-text");
  const msg = $("#run-msg");
  const pill = $("#status-pill");
  const st = $("#status-state");
  const running = !!state.polling;

  const stopBtn = $("#btn-stop");
  if (stopBtn) stopBtn.hidden = !running;

  if (running) {
    if (pill) pill.className = "runbar-status-pill running";
    if (st) st.textContent = "Đang chạy";
    if (btn) btn.disabled = true;
    if (txt) txt.textContent = "Đang gen...";
    if (msg) msg.textContent = "";
    return;
  }

  const it = pickedItem();
  const nP = state.pickedPrompts.size;
  const n = variantCount();
  const ok = !!it && nP > 0;

  if (pill) pill.className = `runbar-status-pill ${ok ? "selected" : ""}`;
  if (st) st.textContent = ok ? "Đã chọn" : "Sẵn sàng";
  if (btn) btn.disabled = !ok;

  if (!it) {
    if (txt) txt.textContent = "Tạo biến thể";
    if (msg) msg.textContent = "Chọn 1 ảnh gốc";
  } else if (nP === 0) {
    if (txt) txt.textContent = "Tạo biến thể";
    if (msg) msg.textContent = "Chọn ít nhất 1 prompt";
  } else {
    if (txt) txt.textContent = nP === 1 ? `Tạo ${n} ảnh` : `Tạo ${nP} bộ × ${n} ảnh`;
    if (msg) msg.innerHTML = `<b>${esc(it.name)}</b> · ${nP} prompt · ${n} ảnh/bộ`;
  }
}

$("#var-count")?.addEventListener("input", updateRunbar);

// Khuôn ghi đè số lượng ảnh, ghim vào CUỐI prompt.
//
// Khuôn prompt CHUNG vẫn áp dụng nguyên vẹn: nội dung prompt lưu trong
// state.json đã được gói bằng nó từ lúc nhập CSV, nên mọi luật về nền, phong
// cách, chất lượng... đều còn nguyên. Vấn đề là khuôn chung có luật "1 mockup =
// 1 image", mà trang này chỉ đính đúng MỘT ảnh và cần N kết quả -> phải có một
// đoạn ghi đè đứng SAU CÙNG.
//
// Đoạn đó nằm ở data/prompt_template_variants.txt (sửa được ngay trên trang này)
// chứ không gõ cứng trong JS - để khi ChatGPT trả sai số lượng thì chỉnh câu chữ
// là xong, không phải sửa code.
let variantsTpl = "";

async function loadVariantsTpl() {
  try {
    const d = await (await fetch("/api/prompt-template?kind=variants")).json();
    const t = d.template || "";
    // CHỐT AN TOÀN: server cũ chưa biết tham số `kind` sẽ trả về KHUÔN CHUNG.
    // Ghim nguyên khuôn chung vào cuối prompt là prompt bị lặp gấp đôi và luật
    // "1 mockup = 1 image" quay lại - hỏng đúng thứ đoạn này sinh ra để sửa.
    // Khuôn biến thể bắt buộc có {N}, không có nghĩa là lấy nhầm file.
    if (!t.includes("{N}")) {
      variantsTpl = "";
      console.warn("Khuôn biến thể không hợp lệ (thiếu {N}) - bỏ qua. "
                 + "Server có thể đang chạy bản cũ, hãy khởi động lại.");
      return;
    }
    variantsTpl = t;
  } catch (e) {
    variantsTpl = "";
  }
}

function variantSuffix(n) {
  if (!variantsTpl.trim()) return "";
  return "\n\n" + variantsTpl.replaceAll("{N}", String(n));
}

async function startRun() {
  const it = pickedItem();
  if (!it || state.pickedPrompts.size === 0) return;

  if (!variantsTpl) await loadVariantsTpl();   // chưa nạp kịp thì nạp ngay

  const profiles = allProfileNames();
  if (profiles.length === 0) {
    return showToast("Chưa có tài khoản nào. Thêm ở trang chính.", "error");
  }

  const n = variantCount();
  // N job cùng trỏ vào MỘT ảnh gốc -> cùng một thư mục đích, khác tên file.
  // Đây chính là chỗ "gộp vào 1 folder" mà không phải thêm quy ước nào mới.
  const cols = [];
  for (const id of state.pickedPrompts) {
    const p = state.prompts.find(x => x.id === id);
    if (!p) continue;
    cols.push({
      name: `${p.name}_Var_${String(n).padStart(2, "0")}`,
      prompt: p.text + variantSuffix(n),
      prompt_name: p.name,
      templates: Array(n).fill(it.rel),
      topic: p.topic || p.name,
      design: p.design || "Design",
    });
  }

  try {
    const res = await fetch("/api/collections/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ collections: cols, profiles, skip_done: false }),
    });
    const d = await res.json();
    if (!res.ok) return showToast(d.detail || "Lỗi tạo biến thể", "error");

    showToast(`Bắt đầu ${d.started_collections} bộ × ${n} ảnh`, "success");
    $("#results-panel").hidden = false;
    $("#results-panel").scrollIntoView({ behavior: "smooth" });
    startPolling();
  } catch (e) {
    showToast("Lỗi kết nối", "error");
  }
}

$("#run-btn")?.addEventListener("click", () => { if (!state.polling) startRun(); });

$("#btn-stop")?.addEventListener("click", async () => {
  try {
    await fetch("/api/jobs/stop", { method: "POST" });
    showToast("Đã dừng khẩn cấp", "warn");
  } catch (e) {
    showToast("Không dừng được", "error");
  }
});

// ==========================================================================
// 4b. Sửa khuôn ghi đè số lượng
// ==========================================================================

function tplStatus(msg, bad) {
  const el = $("#vt-status");
  if (!el) return;
  el.hidden = !msg;
  el.textContent = msg || "";
  el.style.color = bad ? "var(--err)" : "";
}

$("#btn-var-tpl")?.addEventListener("click", async () => {
  await loadVariantsTpl();
  $("#vt-text").value = variantsTpl;
  tplStatus("");
  $("#vt-modal").hidden = false;
  $("#vt-text").focus();
});

const closeVtModal = () => { $("#vt-modal").hidden = true; };
$("#vt-close")?.addEventListener("click", closeVtModal);
$("#vt-cancel")?.addEventListener("click", closeVtModal);

$("#vt-save")?.addEventListener("click", async () => {
  const text = $("#vt-text").value;
  if (!text.includes("{N}")) {
    return tplStatus("Khuôn phải chứa {N} - chỗ điền số ảnh cần tạo.", true);
  }
  const btn = $("#vt-save");
  btn.disabled = true;
  try {
    const res = await fetch("/api/prompt-template?kind=variants", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template: text }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || "lưu thất bại");
    variantsTpl = text;
    showToast("Đã lưu khuôn biến thể", "success");
    closeVtModal();
  } catch (e) {
    tplStatus(String(e.message || e), true);
  } finally {
    btn.disabled = false;
  }
});

// ==========================================================================
// 5. Theo dõi kết quả
// ==========================================================================

function jobCard(j) {
  const done = j.status === "done" && j.result_url;
  const running = j.status === "running";
  const failed = j.status === "failed";
  const cls = done ? "done" : (running ? "running" : (failed ? "failed" : "pending"));
  const name = String(j.template_name || "").split("/").pop();

  return `
    <div class="job-card ${cls}" ${failed && j.error ? `title="${esc(j.error)}"` : ""}>
      <div class="job-preview-wrap">
        ${done
          ? `<img class="job-mockup-img" src="${j.result_url}" alt="${esc(name)}" loading="lazy">`
          : `<div class="job-slot-empty">${running ? `<span class="spin-ring"></span>`
             : (failed ? `<span class="job-err">✕</span>` : "")}</div>`}
        ${done ? `<a href="${j.result_url}" download class="job-dl" title="Tải ảnh này">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
          </a>` : ""}
      </div>
      <div class="job-footer"><span class="job-filename" title="${esc(name)}">${esc(name)}</span></div>
    </div>`;
}

function renderCollections(cols) {
  const box = $("#collections-container");
  if (!box) return;

  let total = 0, done = 0;
  cols.forEach(c => { total += c.total_count || 0; done += c.done_count || 0; });
  const stat = $("#results-stat .stat-val");
  if (stat) stat.textContent = `${done}/${total} ảnh · ${cols.length} bộ`;
  const fill = $("#run-fill");
  if (fill) fill.style.width = total ? `${Math.round(done / total * 100)}%` : "0%";

  if (!changed("cols", cols.map(c => [c.id, c.status, c.done_count,
        (c.jobs || []).map(j => [j.id, j.status, j.result_url])]))) return;

  box.innerHTML = cols.map(c => {
    const cls = c.status === "done" ? "done"
              : (c.status === "running" ? "running"
              : (c.status === "partial" ? "partial" : ""));
    const label = c.status === "done" ? "Hoàn thành"
                : (c.status === "running" ? "Đang chạy"
                : (c.status === "partial" ? "Một phần" : "Chờ"));
    return `
      <div class="collection-card ${cls}">
        <div class="collection-header">
          <div class="collection-title-group">
            <span class="collection-folder-name">${esc(c.name)}</span>
          </div>
          <div class="collection-actions">
            <span class="collection-stat-badge ${cls}">${c.done_count}/${c.total_count} · ${label}</span>
            <a href="/api/jobs/zip?cid=${esc(c.id)}" class="icon-action" title="Tải bộ này (.zip)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
            </a>
          </div>
        </div>
        <div class="results-grid">${(c.jobs || []).map(jobCard).join("")}</div>
      </div>`;
  }).join("");
}

function renderFleet(fleet, exhausted) {
  const chips = $("#fleet-chips");
  if (!chips) return;
  const names = Object.keys(fleet || {});
  const exh = new Set((exhausted || []).map(x => x.profile));
  chips.innerHTML = names.map(p => {
    const info = fleet[p] || {};
    const isExh = exh.has(p) || info.status === "exhausted";
    const busy = info.status === "busy";
    const text = isExh ? "hết lượt"
               : (busy ? esc(info.collection_name || "đang gen")
               : (info.status === "starting" ? "đang mở Chrome…" : "rảnh"));
    return `<div class="fleet-chip ${isExh ? "exhausted" : (busy ? "busy" : "")}">
        <span class="status-dot ${isExh ? "exhausted" : (busy ? "online" : "")}"></span>
        <span class="fleet-chip-name">${esc(p)}</span>
        <span class="fleet-chip-status">${text}</span>
      </div>`;
  }).join("");
}

function startPolling() {
  if (state.polling) clearInterval(state.polling);

  const poll = async () => {
    try {
      const d = await (await fetch("/api/jobs")).json();
      state.collections = d.collections || [];
      if (state.collections.length) {
        $("#results-panel").hidden = false;
        $("#run-strip").hidden = false;
        renderCollections(state.collections);

        const total = state.collections.length;
        const done = state.collections.filter(c => c.status === "done").length;
        const badge = $("#run-badge");
        const text = $("#run-text");
        if (badge) badge.textContent = `${done}/${total} bộ`;
        if (text) text.textContent = d.active ? `Đang chạy · còn ${total - done} bộ`
                                              : (done === total ? "Xong tất cả" : "Tạm dừng");
        $("#run-strip").classList.toggle("is-running", !!d.active);
      }
      renderFleet(d.fleet, d.exhausted);

      if (!d.active) {
        clearInterval(state.polling);
        state.polling = null;
        updateRunbar();
      }
    } catch (e) { /* vòng sau hỏi lại */ }
  };

  poll();
  state.polling = setInterval(poll, 2200);
  updateRunbar();
}

// ==========================================================================
loadImages();
loadPrompts();
loadProfiles();
loadVariantsTpl();
setInterval(loadProfiles, 15000);
// Mở trang giữa lúc đang chạy dở thì bám vào theo dõi luôn.
fetch("/api/jobs").then(r => r.json()).then(d => { if (d.active) startPolling(); })
  .catch(() => {});
updateRunbar();
