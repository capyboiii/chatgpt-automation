// ==========================================================================
// Mockup Forge — Clean & Fast Frontend Logic
// ==========================================================================

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  profiles: [],
  templates: [],
  selected: new Set(),
  filterQuery: "",
  prompts: [],
  promptId: null,
  selectedPrompts: new Set(),
  selectedFleetProfiles: new Set(),
  polling: null,
  jobs: [],
  collections: [],
  stagedQueue: [],
  fleet: {},
  runProfile: null,
  batches: [],
  currentBatchIndex: 0,
  resumeMode: false,
  isStopped: false,
};

// ---- Toast Notification ----
function showToast(message, type = "info") {
  const container = $("#toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(15px)";
    toast.style.transition = "all 0.2s ease";
    setTimeout(() => toast.remove(), 200);
  }, 2800);
}

// ---- Theme Switcher ----
(function initTheme() {
  const saved = localStorage.getItem("mf-theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);

  $("#theme-toggle")?.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("mf-theme", next);
  });
})();

function esc(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// ==========================================================================
// 0. Profiles & Accounts
// ==========================================================================

async function loadProfiles() {
  try {
    const res = await fetch("/api/profiles");
    const d = await res.json();
    state.profiles = d.profiles || [];
    $("#pool-info").textContent = `${state.profiles.length} tài khoản`;
    const tabEl = $("#total-tabs .stat-val") || $("#total-tabs");
    if (tabEl) tabEl.textContent = `${state.profiles.length} tài khoản`;


    renderProfiles();
  } catch (err) {
    console.error("Lỗi load profiles:", err);
  }
}

// Toggle add profile form
function renderProfiles() {
  const list = $("#profiles-list");
  if (!list) return;

  if (state.profiles.length === 0) {
    list.innerHTML = `<div class="empty-state" style="grid-column: 1 / -1; padding:14px; margin:0;">Chưa có tài khoản. Bấm "Thêm tài khoản" để kết nối.</div>`;
    return;
  }

  list.innerHTML = state.profiles.map(p => {
    const isOnline = p.exists && !p.login_open;
    const isLogging = p.login_open;
    const statusDotClass = isLogging ? "active-logging" : (isOnline ? "online" : "");

    return `
      <div class="pcard" data-name="${esc(p.name)}">
        <div class="pcard-top">
          <span class="status-dot ${statusDotClass}"></span>
          <span class="pcard-name">${esc(p.name)}</span>
          <span class="pcard-status-pill ${isLogging ? "warn" : (isOnline ? "online" : "")}">
            ${isLogging ? "Đang mở" : (isOnline ? "Sẵn sàng" : "Chưa kết nối")}
          </span>
          <button class="icon-action danger" data-delp="${esc(p.name)}" title="Xoá tài khoản">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"></path><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"></path></svg>
          </button>
        </div>
        <div class="pcard-email" title="${esc(p.email || "")}">${esc(p.email || "—")}</div>
        <button class="btn ${isLogging ? "primary confirm" : (isOnline ? "action-btn" : "primary")} sm btn-login-step" data-login="${esc(p.name)}" data-stage="${isLogging ? "confirm" : "idle"}">
          ${isLogging ? "Đã xong" : (isOnline ? "Đăng nhập lại" : "Đăng nhập")}
        </button>
      </div>
    `;
  }).join("");

  list.querySelectorAll("[data-delp]").forEach(b => {
    b.addEventListener("click", async () => {
      const name = b.dataset.delp;
      if (!confirm(`Xoá tài khoản "${name}"?
Toàn bộ dữ liệu đăng nhập (thư mục profile) cũng bị xoá.`)) return;
      const r = await (await fetch(`/api/profiles/${name}`, { method: "DELETE" })).json();
      if (r.dir_removed === false) {
        showToast(`Đã xoá ${name} nhưng chưa xoá được thư mục (Chrome còn mở?)`, "error");
      } else {
        showToast(`Đã xoá ${name} và thư mục profile`, "info");
      }
      loadProfiles();
    });
  });
  list.querySelectorAll("[data-login]").forEach(b => {
    b.addEventListener("click", () => handleLoginFlow(b));
  });
}

async function handleLoginFlow(btn) {
  const name = btn.dataset.login;
  if (btn.dataset.stage === "confirm") {
    btn.innerHTML = `<span class="spin-ring sm"></span>`;
    btn.disabled = true;
    try {
      const r = await (await fetch(`/api/profiles/${name}/login/close`, { method: "POST" })).json();
      if (r.logged_in) {
        showToast(`Tài khoản "${name}" đã kết nối!`, "success");
      } else {
        showToast(`Chưa thấy đăng nhập trên "${name}".`, "error");
      }
    } catch (e) {
      showToast(`Lỗi: ${e.message}`, "error");
    }
    loadProfiles();
    return;
  }

  openLoginModal(name, btn);
}

// ---- Đăng nhập hàng loạt: dán nhiều dòng, tool chạy lần lượt ---------------
const BULK_LABEL = {
  pending: "Chờ", running: "Đang đăng nhập…", done: "Xong",
  failed: "Lỗi", needs_human: "Cần bạn xác minh",
};

function renderBulkList(items) {
  const box = $("#bm-list");
  if (!box) return;
  if (!items || !items.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = items.map(it => `
    <div class="bulk-row">
      <span class="bulk-email" title="${esc(it.email)}">${esc(it.email)}</span>
      <span class="bulk-prof">${esc(it.profile)}</span>
      <span class="bulk-badge ${it.status}">${BULK_LABEL[it.status] || it.status}</span>
    </div>
    ${it.error ? `<div class="quota-reason">${esc(it.error)}</div>` : ""}
  `).join("");
}

function openBulkModal() {
  const m = $("#bulk-modal");
  if (!m) return;
  $("#bm-creds").value = "";
  renderBulkList(null);
  $("#bm-go").disabled = false;
  m.hidden = false;
  $("#bm-creds").focus();
}

function closeBulkModal() {
  const m = $("#bulk-modal");
  if (m) m.hidden = true;
  if (state.bulkPoll) {
    clearInterval(state.bulkPoll);
    state.bulkPoll = null;
  }
}

$("#open-bulk-login")?.addEventListener("click", openBulkModal);
$("#bm-close")?.addEventListener("click", closeBulkModal);
$("#bm-cancel")?.addEventListener("click", closeBulkModal);
$("#bulk-modal .acc-modal-backdrop")?.addEventListener("click", closeBulkModal);

$("#bm-go")?.addEventListener("click", async () => {
  const creds = $("#bm-creds").value.trim();
  if (!creds) {
    showToast("Dán danh sách tài khoản vào ô trên đã.", "error");
    return;
  }
  $("#bm-go").disabled = true;
  try {
    const res = await fetch("/api/profiles/bulk-login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ creds })
    });
    const d = await res.json();
    if (!res.ok) {
      showToast(d.detail || "Không bắt đầu được", "error");
      $("#bm-go").disabled = false;
      return;
    }
    $("#bm-creds").value = "";      // xoá credential khỏi màn hình ngay
    renderBulkList(d.items);
    if (d.skipped?.length) {
      showToast(`Bỏ qua ${d.skipped.length} dòng sai định dạng: ${d.skipped.join(", ")}`, "error");
    }
    showToast(`Bắt đầu đăng nhập ${d.started} tài khoản`, "info");
  } catch (e) {
    showToast(`Lỗi kết nối: ${e.message}`, "error");
    $("#bm-go").disabled = false;
    return;
  }

  if (state.bulkPoll) clearInterval(state.bulkPoll);
  state.bulkPoll = setInterval(async () => {
    try {
      const d = await (await fetch("/api/profiles/bulk-login/status")).json();
      renderBulkList(d.items);
      if (!d.active) {
        clearInterval(state.bulkPoll);
        state.bulkPoll = null;
        const ok = (d.items || []).filter(x => x.status === "done").length;
        showToast(`Đăng nhập xong ${ok}/${(d.items || []).length} tài khoản`,
                  ok ? "success" : "error");
        $("#bm-go").disabled = false;
        loadProfiles();
      }
    } catch (e) { /* vòng sau hỏi lại */ }
  }, 2000);
});

// ---- Đăng nhập: tự động (dán credential) hoặc tự làm tay --------------------
function openLoginModal(name, btn) {
  state.loginTarget = { name, btn };
  const m = $("#login-modal");
  if (!m) return;
  $("#lm-profile").textContent = `"${name}"`;
  $("#lm-creds").value = "";
  const st = $("#lm-status");
  st.hidden = true;
  st.textContent = "";
  $("#lm-go").disabled = false;
  const mb = $("#lm-manual");
  mb.disabled = false;
  mb.textContent = "Tự đăng nhập";
  delete mb.dataset.mode;
  m.hidden = false;
  $("#lm-creds").focus();
}

function closeLoginModal() {
  const m = $("#login-modal");
  if (m) m.hidden = true;
  if (state.loginPoll) {
    clearInterval(state.loginPoll);
    state.loginPoll = null;
  }
}

$("#lm-close")?.addEventListener("click", closeLoginModal);
$("#login-modal .acc-modal-backdrop")?.addEventListener("click", closeLoginModal);

// Tự đăng nhập thủ công: y như trước, mở cửa sổ rồi bấm "Xong"
$("#lm-manual")?.addEventListener("click", async (ev) => {
  const t = state.loginTarget;
  if (!t) return;

  // Sau khi tự động hỏng, nút này đổi vai thành "Tôi đã đăng nhập xong"
  const btn = ev.currentTarget;
  if (btn.dataset.mode === "close") {
    btn.disabled = true;
    try {
      const r = await (await fetch(`/api/profiles/${t.name}/login/close`,
                                   { method: "POST" })).json();
      showToast(r.logged_in ? `Tài khoản "${t.name}" đã kết nối!`
                            : `Vẫn chưa thấy đăng nhập trên "${t.name}".`,
                r.logged_in ? "success" : "error");
    } catch (e) {
      showToast(`Lỗi: ${e.message}`, "error");
    }
    btn.textContent = "Tự đăng nhập";
    delete btn.dataset.mode;
    closeLoginModal();
    loadProfiles();
    return;
  }

  closeLoginModal();
  try {
    await fetch(`/api/profiles/${t.name}/login`, { method: "POST" });
    if (t.btn) {
      t.btn.textContent = "Xong";
      t.btn.dataset.stage = "confirm";
      t.btn.classList.add("confirm");
    }
    showToast(`Đã mở Chrome. Đăng nhập xong bấm "Xong".`, "info");
    loadProfiles();
  } catch (e) {
    showToast(`Không thể mở Chrome: ${e.message}`, "error");
  }
});

const LOGIN_PHASE_TEXT = {
  starting: "Đang mở Chrome…",
  email: "Đang nhập email…",
  password: "Đang nhập mật khẩu…",
  "2fa": "Đang nhập mã 2FA…",
  captcha: "⚠️ ChatGPT đòi xác minh người thật — bấm giúp trong cửa sổ Chrome, tool sẽ tự chạy tiếp.",
  done: "Xong!",
};

$("#lm-go")?.addEventListener("click", async () => {
  const t = state.loginTarget;
  if (!t) return;
  const creds = $("#lm-creds").value.trim();
  if (!creds) {
    showToast("Dán email | mật khẩu | 2FA vào ô trên đã.", "error");
    return;
  }
  const st = $("#lm-status");
  $("#lm-go").disabled = true;
  $("#lm-manual").disabled = true;
  st.hidden = false;
  st.textContent = "Đang mở Chrome…";

  try {
    const res = await fetch(`/api/profiles/${t.name}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ creds })
    });
    if (!res.ok) {
      const err = await res.json();
      st.textContent = err.detail || "Không mở được cửa sổ đăng nhập.";
      $("#lm-go").disabled = false;
      $("#lm-manual").disabled = false;
      return;
    }
  } catch (e) {
    st.textContent = `Lỗi kết nối: ${e.message}`;
    $("#lm-go").disabled = false;
    return;
  }
  $("#lm-creds").value = "";        // xoá khỏi màn hình ngay khi đã gửi đi

  if (state.loginPoll) clearInterval(state.loginPoll);
  state.loginPoll = setInterval(async () => {
    try {
      const d = await (await fetch(`/api/profiles/${t.name}/login/status`)).json();
      st.textContent = d.needs_human
        ? LOGIN_PHASE_TEXT.captcha
        : (LOGIN_PHASE_TEXT[d.phase] || "Đang xử lý…");

      if (d.phase === "failed" && d.open) {
        // Tự động hỏng nhưng cửa sổ Chrome vẫn mở -> mời người dùng làm nốt bằng tay
        clearInterval(state.loginPoll);
        state.loginPoll = null;
        st.textContent = (d.error || "Đăng nhập tự động không xong.")
          + " Cửa sổ Chrome vẫn mở — bạn đăng nhập nốt rồi bấm nút bên dưới.";
        $("#lm-go").disabled = true;
        const mb = $("#lm-manual");
        mb.disabled = false;
        mb.textContent = "Tôi đã đăng nhập xong";
        mb.dataset.mode = "close";
        loadProfiles();
        return;
      }

      if (d.logged_in === true) {
        clearInterval(state.loginPoll);
        state.loginPoll = null;
        showToast(`Tài khoản "${t.name}" đã kết nối!`, "success");
        closeLoginModal();
        loadProfiles();
      } else if (!d.open && d.logged_in === false) {
        clearInterval(state.loginPoll);
        state.loginPoll = null;
        st.textContent = d.error || "Đăng nhập không thành công.";
        $("#lm-go").disabled = false;
        $("#lm-manual").disabled = false;
        loadProfiles();
      }
    } catch (e) {
      /* mạng chớp nháy - vòng sau hỏi lại */
    }
  }, 2000);
});

// ==========================================================================
// 1. Templates & Upload Center
// ==========================================================================

async function loadTemplates() {
  try {
    const res = await fetch("/api/templates");
    const d = await res.json();
    state.templates = d.items || [];
    state.selected = new Set([...state.selected].filter(n => state.templates.some(t => t.name === n)));
    renderTemplates();
  } catch (err) {
    console.error("Lỗi load templates:", err);
  }
}

function renderTemplates() {
  const grid = $("#tpl-grid");
  const empty = $("#tpl-empty");
  const toolbar = $("#gallery-toolbar");

  if (!grid) return;

  const count = state.templates.length;
  if (count === 0) {
    grid.innerHTML = "";
    empty.hidden = false;
    if (toolbar) toolbar.hidden = true;
    const tplEl = $("#tpl-count .stat-val") || $("#tpl-count");
    if (tplEl) tplEl.textContent = `0 đã chọn`;
    updateRunMatrix();
    return;
  }

  empty.hidden = true;
  if (toolbar) toolbar.hidden = false;

  const q = state.filterQuery.toLowerCase().trim();
  const displayed = q ? state.templates.filter(t => t.name.toLowerCase().includes(q)) : state.templates;

  grid.innerHTML = displayed.map(t => {
    const isSelected = state.selected.has(t.name);
    return `
      <div class="tpl-card ${isSelected ? "selected" : ""}" data-name="${esc(t.name)}" title="Click để ${isSelected ? "bỏ chọn" : "chọn"} ảnh">
        <div class="tpl-img-box">
          <span class="tpl-badge-select">
            ${isSelected ? `✓` : `+`}
          </span>
          <button type="button" class="tpl-btn-preview" data-preview="${esc(t.url)}" data-cap="${esc(t.name)}" onclick="event.stopPropagation(); window.openLightbox('${esc(t.url)}', '${esc(t.name)}');" title="Xem ảnh lớn">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
          </button>
          <button type="button" class="tpl-btn-del" data-del="${esc(t.name)}" title="Xoá ảnh này">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6 6 18"></path><path d="m6 6 12 12"></path></svg>
          </button>
          <img src="${t.url}" loading="lazy" alt="${esc(t.name)}">
        </div>
        <div class="tpl-card-footer">
          <span class="tpl-card-name" title="${esc(t.name)}">${esc(t.name)}</span>
        </div>
      </div>
    `;
  }).join("");

  grid.querySelectorAll(".tpl-card").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-del]") || e.target.closest("[data-preview]")) return;
      const name = card.dataset.name;
      if (state.selected.has(name)) state.selected.delete(name);
      else state.selected.add(name);
      renderTemplates();
    });
  });

  grid.querySelectorAll("[data-del]").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const name = btn.dataset.del;
      await fetch(`/api/templates/${name}`, { method: "DELETE" });
      state.selected.delete(name);
      loadTemplates();
      showToast(`Đã xoá ${name}`, "info");
    });
  });

  grid.querySelectorAll("[data-preview]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      window.openLightbox(btn.dataset.preview, btn.dataset.cap);
    });
  });

  const tplEl = $("#tpl-count .stat-val") || $("#tpl-count");
  if (tplEl) tplEl.textContent = `${state.selected.size} / ${state.templates.length} đã chọn`;
  
  const allSelected = state.templates.length > 0 && state.selected.size === state.templates.length;
  const selectAllChk = $("#select-all");
  if (selectAllChk) selectAllChk.checked = allSelected;

  updateRunMatrix();
}

$("#tpl-search")?.addEventListener("input", (e) => {
  state.filterQuery = e.target.value;
  renderTemplates();
});

$("#select-all")?.addEventListener("change", (e) => {
  if (e.target.checked) {
    state.selected = new Set(state.templates.map(t => t.name));
  } else {
    state.selected.clear();
  }
  renderTemplates();
});

$("#clear-templates")?.addEventListener("click", async () => {
  if (!confirm(`Xoá tất cả ${state.templates.length} ảnh template?`)) return;
  await fetch("/api/templates", { method: "DELETE" });
  state.selected.clear();
  loadTemplates();
  showToast("Đã xoá hết ảnh", "info");
});

const dropzone = $("#dropzone");
const fileInput = $("#file-input");
const folderInput = $("#folder-input");

fileInput?.addEventListener("change", () => uploadFiles(fileInput.files));
$("#pick-folder")?.addEventListener("click", () => folderInput?.click());
folderInput?.addEventListener("change", () => {
  uploadFiles(folderInput.files);
  folderInput.value = "";
});

["dragover", "dragenter"].forEach(ev => {
  dropzone?.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.add("drag");
  });
});

dropzone?.addEventListener("dragleave", () => {
  dropzone.classList.remove("drag");
});

async function getFilesFromDataTransfer(dataTransfer) {
  const files = [];
  const items = dataTransfer.items;
  
  if (items && items.length > 0 && items[0].webkitGetAsEntry) {
    const queue = [];
    for (let i = 0; i < items.length; i++) {
      const entry = items[i].webkitGetAsEntry();
      if (entry) queue.push(entry);
    }
    
    async function readEntry(entry) {
      if (entry.isFile) {
        return new Promise((resolve) => {
          entry.file((file) => {
            files.push(file);
            resolve();
          }, () => resolve());
        });
      } else if (entry.isDirectory) {
        const dirReader = entry.createReader();
        const readEntries = () => new Promise((resolve) => {
          dirReader.readEntries(async (entries) => {
            if (entries.length === 0) return resolve();
            for (const subEntry of entries) {
              await readEntry(subEntry);
            }
            await readEntries();
            resolve();
          }, () => resolve());
        });
        await readEntries();
      }
    }
    
    for (const entry of queue) {
      await readEntry(entry);
    }
  } else if (dataTransfer.files) {
    for (let i = 0; i < dataTransfer.files.length; i++) {
      files.push(dataTransfer.files[i]);
    }
  }
  return files;
}

dropzone?.addEventListener("drop", async (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag");
  const files = await getFilesFromDataTransfer(e.dataTransfer);
  uploadFiles(files);
});

async function uploadFiles(fileList) {
  const files = [...fileList].filter(f => f.type.startsWith("image/") || /\.(png|jpe?g|webp)$/i.test(f.name));
  if (!files.length) {
    showToast("Không có file ảnh hợp lệ", "error");
    return;
  }

  const fd = new FormData();
  files.forEach(f => fd.append("files", f));

  showToast(`Đang tải lên ${files.length} ảnh...`, "info");
  try {
    await fetch("/api/templates/upload", { method: "POST", body: fd });
    if (fileInput) fileInput.value = "";
    showToast(`Đã tải lên ${files.length} ảnh`, "success");
    loadTemplates();
  } catch (err) {
    showToast("Lỗi khi tải ảnh", "error");
  }
}


// ==========================================================================
// 2. Prompt Studio
// ==========================================================================

const promptForm = $("#prompt-form");

async function loadPrompts() {
  try {
    const res = await fetch("/api/prompts");
    const d = await res.json();
    state.prompts = d.items || [];
    
    // Giữ các prompt đã chọn còn tồn tại
    const validIds = new Set(state.prompts.map(p => p.id));
    for (const id of state.selectedPrompts) {
      if (!validIds.has(id)) state.selectedPrompts.delete(id);
    }
    if (state.selectedPrompts.size === 0 && state.prompts.length > 0) {
      state.selectedPrompts.add(state.prompts[0].id);
      state.promptId = state.prompts[0].id;
    }
    renderPrompts();
  } catch (err) {
    console.error("Lỗi load prompts:", err);
  }
}

$("#clear-prompts")?.addEventListener("click", async () => {
  const n = state.prompts.length;
  if (!n) return;
  if (!confirm(`Xoá toàn bộ ${n} prompt?`)) return;
  try {
    const r = await (await fetch("/api/prompts", { method: "DELETE" })).json();
    state.promptId = null;
    state.selectedPrompts?.clear?.();
    showToast(`Đã xoá ${r.deleted} prompt`, "info");
    loadPrompts();
  } catch (e) {
    showToast(`Lỗi: ${e.message}`, "error");
  }
});

// ---- Nhập prompt hàng loạt từ CSV ------------------------------------------
// CSV chỉ chứa PHẦN THIẾT KẾ; server bọc nó vào khuôn RULES trong
// data/prompt_template.txt. Cột TOPIC + Style dùng để đặt tên thư mục ảnh.
$("#btn-import-csv")?.addEventListener("click", () => $("#csv-input")?.click());

$("#csv-input")?.addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  e.target.value = "";
  if (!file) return;

  const btn = $("#btn-import-csv");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Đang nhập…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/prompts/import-csv", { method: "POST", body: fd });
    const d = await res.json();
    if (!res.ok) {
      showToast(d.detail || "Không nhập được CSV", "error");
    } else {
      showToast(`Đã nhập ${d.added} prompt` +
                (d.skipped ? `, bỏ qua ${d.skipped} dòng trống` : ""), "success");
      loadPrompts();
    }
  } catch (err) {
    showToast(`Lỗi: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});

function renderPrompts() {
  const list = $("#prompt-list");
  const badge = $("#prompt-select-badge");
  const badgeCount = $("#prompt-select-count");
  const selAllBtn = $("#btn-select-all-prompts");

  if (badge && badgeCount) {
    const n = state.selectedPrompts.size;
    if (n > 0) {
      badge.hidden = false;
      badgeCount.textContent = `${n} đã chọn`;
    } else {
      badge.hidden = true;
    }
  }

  if (selAllBtn && state.prompts.length > 0) {
    const all = state.selectedPrompts.size === state.prompts.length;
    selAllBtn.textContent = all ? "Bỏ chọn" : "Chọn tất cả";
  }

  if (!list) return;

  if (state.prompts.length === 0) {
    list.innerHTML = `<div class="empty-state" style="padding:14px; margin:0;">Chưa có Prompt. Bấm "Tạo Prompt" để thêm.</div>`;
    updateRunMatrix();
    return;
  }

  list.innerHTML = state.prompts.map(p => {
    const isSelected = state.selectedPrompts.has(p.id);
    return `
      <div class="prompt-card prompt-item ${isSelected ? "selected" : ""}" data-id="${p.id}" title="Click để chọn/bỏ chọn">
        <span class="prompt-card-chk"></span>
        <div class="prompt-content">
          <div class="prompt-title">${esc(p.name)}</div>
          <div class="prompt-snippet">${esc(p.text)}</div>
          <div class="prompt-meta">
            ${p.design ? `${esc(p.topic || p.name)} · ${esc(p.design)} · ` : ""}${p.text.length} ký tự
            ${p.text.length > 120 ? `<button type="button" class="prompt-more" data-more="${p.id}">Xem thêm</button>` : ""}
          </div>
        </div>
        <div class="prompt-actions">
          <button class="icon-action" data-edit="${p.id}" title="Sửa">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"></path></svg>
          </button>
          <button class="icon-action danger" data-delprompt="${p.id}" title="Xoá">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"></path><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"></path></svg>
          </button>
        </div>
      </div>
    `;
  }).join("");

  list.querySelectorAll("[data-more]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const card = btn.closest(".prompt-card");
      const open = card.classList.toggle("expanded");
      btn.textContent = open ? "Thu gọn" : "Xem thêm";
    });
  });

  list.querySelectorAll(".prompt-card").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-edit]") || e.target.closest("[data-delprompt]")
          || e.target.closest("[data-more]")) return;
      const id = card.dataset.id;
      if (state.selectedPrompts.has(id)) {
        if (state.selectedPrompts.size > 1) {
          state.selectedPrompts.delete(id);
        } else {
          // nếu chỉ còn 1 mà bấm vào thì giữ nguyên
        }
      } else {
        state.selectedPrompts.add(id);
      }
      state.promptId = [...state.selectedPrompts][0] || null;
      renderPrompts();
    });
  });

  list.querySelectorAll("[data-edit]").forEach(btn => {
    btn.addEventListener("click", () => openPromptForm(btn.dataset.edit));
  });

  list.querySelectorAll("[data-delprompt]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.delprompt;
      if (!confirm("Xoá Prompt này?")) return;
      await fetch(`/api/prompts/${id}`, { method: "DELETE" });
      state.selectedPrompts.delete(id);
      showToast("Đã xoá Prompt", "info");
      loadPrompts();
    });
  });

  updateRunMatrix();
}

$("#btn-select-all-prompts")?.addEventListener("click", () => {
  if (state.prompts.length === 0) return;
  const all = state.selectedPrompts.size === state.prompts.length;
  if (all) {
    // Chỉ giữ lại 1 prompt đầu tiên
    state.selectedPrompts = new Set([state.prompts[0].id]);
  } else {
    // Chọn tất cả prompt
    state.selectedPrompts = new Set(state.prompts.map(p => p.id));
  }
  state.promptId = [...state.selectedPrompts][0] || null;
  renderPrompts();
});

$("#new-prompt")?.addEventListener("click", () => openPromptForm(null));
$("#pf-cancel")?.addEventListener("click", () => { promptForm.hidden = true; });
$("#pf-close-btn")?.addEventListener("click", () => { promptForm.hidden = true; });

function openPromptForm(id) {
  const p = id ? state.prompts.find(x => x.id === id) : null;
  $("#pf-id").value = p ? p.id : "";
  $("#pf-title").textContent = p ? "Sửa Prompt" : "Tạo Prompt Mới";
  $("#pf-name").value = p ? p.name : "";
  $("#pf-text").value = p ? p.text : "";

  promptForm.hidden = false;
  $("#pf-name").focus();
}

promptForm?.addEventListener("keydown", (e) => {
  if (e.key === "Escape") promptForm.hidden = true;
  else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") promptForm.requestSubmit();
});

promptForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#pf-id").value;
  const name = $("#pf-name").value.trim();
  const text = $("#pf-text").value.trim();

  const body = JSON.stringify({ name, text });
  const opt = {
    method: id ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body
  };

  try {
    const res = await fetch(id ? `/api/prompts/${id}` : "/api/prompts", opt);
    const saved = await res.json();
    promptForm.hidden = true;
    state.promptId = saved.id;
    showToast("Đã lưu Prompt", "success");
    loadPrompts();
  } catch (err) {
    showToast("Lỗi khi lưu Prompt", "error");
  }
});


// ==========================================================================
// 3. Queue Drawer & Staging Logic
// ==========================================================================

function renderStagedQueue() {
  const queueCount = state.stagedQueue.length;
  const badgeRunbar = $("#runbar-queue-badge");
  const badgeDrawer = $("#queue-count-badge");
  const body = $("#queue-drawer-body");
  const summary = $("#queue-summary-text");
  const startBtn = $("#btn-start-queue");

  if (badgeRunbar) badgeRunbar.textContent = queueCount;
  if (badgeDrawer) badgeDrawer.textContent = queueCount;

  let totalMockups = 0;
  state.stagedQueue.forEach(c => {
    totalMockups += (c.templates || []).length;
  });

  if (summary) {
    summary.textContent = queueCount === 0
      ? "Chưa có Collection nào trong hàng đợi"
      : `${queueCount} Collections · ${totalMockups} ảnh mockup`;
  }

  if (startBtn) {
    startBtn.disabled = (queueCount === 0);
  }

  if (!body) return;

  if (queueCount === 0) {
    body.innerHTML = `
      <div class="queue-empty-box">
        <div style="font-size: 1.4rem; margin-bottom: 6px;">📥</div>
        <div><b>Hàng đợi hiện đang trống</b></div>
        <div style="font-size: 0.78rem; color: var(--muted); margin-top: 4px;">
          Hãy chọn ảnh template và prompt trên giao diện rồi bấm <b>"+ Thêm vào hàng đợi"</b> để xếp vào đây.
        </div>
      </div>
    `;
    return;
  }

  body.innerHTML = state.stagedQueue.map((item, idx) => {
    const tCount = (item.templates || []).length;
    return `
      <div class="staged-card" data-idx="${idx}">
        <div class="staged-left">
          <span class="staged-index">#${idx + 1}</span>
          <div class="staged-info">
            <div class="staged-name">📁 ${esc(item.prompt_name || item.name || "Collection")}</div>
            <div class="staged-meta">
              <b>${tCount}</b> templates (${esc((item.templates || []).slice(0, 3).join(", "))}${tCount > 3 ? ` +${tCount - 3}` : ""})
            </div>
          </div>
        </div>
        <button type="button" class="staged-del-btn" data-del-staged="${idx}" title="Xoá Collection này khỏi hàng đợi">✕</button>
      </div>
    `;
  }).join("");

  body.querySelectorAll("[data-del-staged]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = parseInt(btn.dataset.delStaged);
      state.stagedQueue.splice(idx, 1);
      renderStagedQueue();
      updateRunMatrix();
      showToast("Đã xoá Collection khỏi hàng đợi", "info");
    });
  });
}

function toggleQueueDrawer(forceState) {
  const drawer = $("#queue-drawer");
  const backdrop = $("#queue-drawer-backdrop");
  if (!drawer) return;

  const isHidden = drawer.hidden;
  const nextHidden = (typeof forceState === "boolean") ? !forceState : !isHidden;

  drawer.hidden = nextHidden;
  if (backdrop) backdrop.hidden = nextHidden;
}

$("#btn-toggle-queue")?.addEventListener("click", () => toggleQueueDrawer());
$("#btn-close-drawer")?.addEventListener("click", () => toggleQueueDrawer(false));
$("#queue-drawer-backdrop")?.addEventListener("click", () => toggleQueueDrawer(false));

$("#btn-clear-staging")?.addEventListener("click", () => {
  if (state.stagedQueue.length === 0) return;
  state.stagedQueue = [];
  renderStagedQueue();
  updateRunMatrix();
  showToast("Đã dọn sạch hàng đợi chờ", "info");
});

// Thêm bộ template + prompt hiện tại vào hàng đợi
async function addCurrentToQueue() {
  const tplCount = state.selected.size;
  const promptCount = state.selectedPrompts.size;

  if (tplCount === 0) {
    showToast("Vui lòng chọn ít nhất 1 ảnh template!", "error");
    return;
  }
  if (promptCount === 0) {
    showToast("Vui lòng chọn ít nhất 1 prompt!", "error");
    return;
  }

  const isRunning = !!state.polling;

  // Nếu hệ thống ĐANG CHẠY: gửi trực tiếp vào active worker pool!
  if (isRunning) {
    const colsToAdd = [];
    for (const pid of state.selectedPrompts) {
      const p = state.prompts.find(x => x.id === pid);
      if (!p) continue;
      colsToAdd.push({
        name: p.name,
        prompt: p.text,
        prompt_name: p.name,
        templates: [...state.selected]
      });
    }

    try {
      const res = await fetch("/api/collections/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ collections: colsToAdd })
      });

      if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || "Lỗi nối hàng đợi", "error");
        return;
      }

      const d = await res.json();
      showToast(`⚡ Đã nối ${d.started_collections} Collection vào hàng đợi đang gen!`, "success");
      
      const resPanel = $("#results-panel");
      if (resPanel) {
        resPanel.hidden = false;
        resPanel.scrollIntoView({ behavior: "smooth" });
      }
    } catch (err) {
      showToast("Lỗi kết nối", "error");
    }
    return;
  }

  // Nếu hệ thống CHƯA CHẠY: đóng gói vào state.stagedQueue
  let added = 0;
  for (const pid of state.selectedPrompts) {
    const p = state.prompts.find(x => x.id === pid);
    if (!p) continue;
    state.stagedQueue.push({
      id: "stg_" + Math.random().toString(36).substring(2, 9),
      name: p.name,
      prompt_id: p.id,
      prompt: p.text,
      prompt_name: p.name,
      templates: [...state.selected]
    });
    added++;
  }

  renderStagedQueue();
  updateRunMatrix();
  showToast(`Đã thêm ${added} Collection vào Hàng Đợi (${state.stagedQueue.length} bộ đang chờ)`, "success");
}

$("#btn-add-queue")?.addEventListener("click", addCurrentToQueue);
$("#btn-start-queue")?.addEventListener("click", () => {
  toggleQueueDrawer(false);
  openAccModal();
});

// ==========================================================================
// 4. Run Bar State & Modal
// ==========================================================================

function updateRunMatrix() {
  const tplCount = state.selected.size;
  const promptCount = state.selectedPrompts.size;
  const queueCount = state.stagedQueue.length;

  const statusPill = $("#runbar-status-pill");
  const statusState = $("#runbar-status-state");
  const mainMsg = $("#runbar-main-msg");
  const runBtn = $("#run-btn");
  const runBtnText = $("#run-btn-text");
  const addQueueBtn = $("#btn-add-queue");
  const isRunning = !!state.polling;

  const stopBtn = $("#btn-emergency-stop");
  const stopResultsBtn = $("#btn-stop-results");
  if (isRunning) {
    if (stopBtn) stopBtn.hidden = false;
    if (stopResultsBtn) stopResultsBtn.hidden = false;
  } else {
    if (stopBtn) stopBtn.hidden = true;
    if (stopResultsBtn) stopResultsBtn.hidden = true;
  }

  const canAdd = (tplCount > 0 && promptCount > 0);
  if (addQueueBtn) addQueueBtn.disabled = !canAdd;

  if (isRunning) {
    if (statusPill && statusState) {
      statusPill.className = "runbar-status-pill running";
      statusState.textContent = "Đang chạy";
    }

    if (addQueueBtn) {
      addQueueBtn.classList.add("is-enqueue");
      addQueueBtn.innerHTML = `<span>+ Nối hàng đợi</span>`;
    }
    if (runBtn) {
      runBtn.disabled = true;
      runBtn.classList.remove("is-enqueue");
    }
    if (runBtnText) runBtnText.textContent = "Đang gen...";

    if (canAdd) {
      if (mainMsg) mainMsg.innerHTML = `Đã chọn: <b>${tplCount}</b> ảnh · <b>${promptCount}</b> prompt (bấm + Nối hàng đợi)`;
    } else {
      if (mainMsg) mainMsg.innerHTML = `ChatGPT đang sinh mockup... Bạn có thể bấm <b>Dừng khẩn cấp</b> bất cứ lúc nào`;
    }
    return;
  }

  if (addQueueBtn) {
    addQueueBtn.classList.remove("is-enqueue");
    addQueueBtn.innerHTML = `<span>+ Thêm hàng đợi</span>`;
  }
  if (runBtn) runBtn.classList.remove("is-enqueue");

  if (queueCount > 0) {
    if (statusPill && statusState) {
      statusPill.className = "runbar-status-pill selected";
      statusState.textContent = `${queueCount} bộ chờ`;
    }
    if (runBtn) runBtn.disabled = false;
    if (runBtnText) runBtnText.textContent = `▶ Chạy ${queueCount} bộ`;
    if (canAdd) {
      if (mainMsg) mainMsg.innerHTML = `Đã chọn thêm: <b>${tplCount}</b> ảnh · <b>${promptCount}</b> prompt`;
    } else {
      if (mainMsg) mainMsg.innerHTML = `Sẵn sàng chạy <b>${queueCount}</b> Collection trong hàng đợi`;
    }
    return;
  }

  // queueCount === 0
  if (!canAdd) {
    if (statusPill && statusState) {
      statusPill.className = "runbar-status-pill";
      statusState.textContent = "Sẵn sàng";
    }
    if (runBtn) runBtn.disabled = true;
    if (runBtnText) runBtnText.textContent = "Tạo Mockup";
    if (tplCount === 0) {
      if (mainMsg) mainMsg.textContent = `Chọn ít nhất 1 ảnh template`;
    } else {
      if (mainMsg) mainMsg.textContent = `Chọn ít nhất 1 prompt`;
    }
  } else {
    if (statusPill && statusState) {
      statusPill.className = "runbar-status-pill selected";
      statusState.textContent = "Đã chọn";
    }
    if (runBtn) runBtn.disabled = false;
    const activePrompt = state.prompts.find(p => state.selectedPrompts.has(p.id));
    const pName = activePrompt ? activePrompt.name : "Prompt";
    if (promptCount === 1) {
      if (runBtnText) runBtnText.textContent = `Tạo Mockup (${tplCount})`;
      if (mainMsg) mainMsg.innerHTML = `<b>${tplCount}</b> ảnh · <b>${esc(pName)}</b>`;
    } else {
      if (runBtnText) runBtnText.textContent = `Tạo ${promptCount} bộ`;
      if (mainMsg) mainMsg.innerHTML = `<b>${tplCount}</b> ảnh · <b>${promptCount}</b> prompt`;
    }
  }
}

// ---- Chọn tài khoản rồi mới gen -------------------------------------------
function openAccModal(isResume = false, remainingCount = 0) {
  const box = $("#acc-modal-list");
  const title = $("#acc-modal-title");
  const note = $("#acc-modal-note");
  const goBtn = $("#acc-modal-go");
  if (!box) return;

  if (state.profiles.length === 0) {
    showToast("Chưa có tài khoản nào. Thêm tài khoản trước đã.", "error");
    return;
  }

  state.resumeMode = Boolean(isResume);

  const queueCount = state.stagedQueue.length;
  const promptCount = Math.max(1, state.selectedPrompts.size);
  const totalCollections = queueCount > 0 ? queueCount : promptCount;
  const isBulk = totalCollections > 1 || isResume;

  if (isResume) {
    if (title) title.textContent = `Chạy tiếp ${remainingCount} bộ chưa hoàn thành`;
    if (note) note.innerHTML = `Hệ thống sẽ chạy tiếp <b>${remainingCount} Collections</b> còn thiếu, tự động bỏ qua các ảnh đã có trên đĩa.`;
    if (goBtn) goBtn.textContent = `Bắt đầu chạy tiếp (${remainingCount} bộ)`;
  } else if (queueCount > 0) {
    if (title) title.textContent = `Chọn tài khoản chạy (${queueCount} Collections)`;
    if (note) note.innerHTML = `Hệ thống sẽ chạy <b>${queueCount} Collections</b> trong hàng đợi song song qua các tài khoản được chọn.`;
    if (goBtn) goBtn.textContent = `Bắt đầu chạy ${queueCount} Collections`;
  } else if (!isBulk) {
    // Single mode: 1 Collection duy nhất
    if (title) title.textContent = "Chọn tài khoản để gen";
    if (note) note.innerHTML = "Cả lượt gen chạy trên <b>1 tài khoản, trong 1 phiên chat</b> để bộ mockup đồng nhất design.";
    if (goBtn) goBtn.textContent = "Bắt đầu gen (1 Collection)";

    const last = localStorage.getItem("genProfile");
    const names = state.profiles.map(p => p.name);
    state.genProfile = names.includes(last) ? last : names[0];

    box.innerHTML = state.profiles.map(p => {
      const ready = p.exists && !p.login_open;
      return `
        <label class="acc-pick ${p.name === state.genProfile ? "selected" : ""}" data-acc="${esc(p.name)}">
          <input type="radio" name="acc-pick" value="${esc(p.name)}" ${p.name === state.genProfile ? "checked" : ""}>
          <span class="acc-pick-name">${esc(p.name)}</span>
          <span class="acc-pick-state ${ready ? "ok" : "warn"}">
            ${p.login_open ? "Đang mở Chrome" : (ready ? "Đã đăng nhập" : "Chưa kết nối")}
          </span>
        </label>`;
    }).join("");

    box.querySelectorAll(".acc-pick").forEach(el => {
      el.addEventListener("click", () => {
        state.genProfile = el.dataset.acc;
        box.querySelectorAll(".acc-pick").forEach(x => x.classList.remove("selected"));
        el.classList.add("selected");
        el.querySelector("input").checked = true;
      });
    });
    $("#acc-modal").hidden = false;
    return;
  } else {
    // Bulk Collections mode
    if (title) title.textContent = `Chọn đội tài khoản (${totalCollections} Collections)`;
    if (note) note.innerHTML = `Hệ thống sẽ tạo <b>${totalCollections} Collections</b> song song qua các tài khoản được chọn.`;
    if (goBtn) goBtn.textContent = `Bắt đầu tạo ${totalCollections} Collections (${state.selectedFleetProfiles.size || state.profiles.length} tài khoản)`;
  }

  if (state.selectedFleetProfiles.size === 0) {
    state.profiles.forEach(p => {
      if (p.exists) state.selectedFleetProfiles.add(p.name);
    });
  }

  const allChecked = state.profiles.length > 0 && state.profiles.every(p => state.selectedFleetProfiles.has(p.name));

  let html = `
    <label class="acc-pick ${allChecked ? "selected" : ""}" id="fleet-select-all" style="border-style:dashed;">
      <input type="checkbox" ${allChecked ? "checked" : ""}>
      <span class="acc-pick-name">Chọn tất cả tài khoản</span>
      <span class="acc-pick-state ok">${state.selectedFleetProfiles.size}/${state.profiles.length}</span>
    </label>
    <hr class="notice-sep">
  `;

  html += state.profiles.map(p => {
    const ready = p.exists && !p.login_open;
    const isChecked = state.selectedFleetProfiles.has(p.name);
    return `
      <label class="acc-pick ${isChecked ? "selected" : ""}" data-fleet-acc="${esc(p.name)}">
        <input type="checkbox" value="${esc(p.name)}" ${isChecked ? "checked" : ""}>
        <span class="acc-pick-name">${esc(p.name)}</span>
        <span class="acc-pick-state ${ready ? "ok" : "warn"}">
          ${p.login_open ? "Đang mở Chrome" : (ready ? "Sẵn sàng" : "Chưa kết nối")}
        </span>
      </label>`;
  }).join("");

  box.innerHTML = html;

  const selectAllEl = box.querySelector("#fleet-select-all");
  selectAllEl?.addEventListener("click", (e) => {
    e.preventDefault();
    const nextCheck = state.selectedFleetProfiles.size !== state.profiles.length;
    if (nextCheck) {
      state.profiles.forEach(p => state.selectedFleetProfiles.add(p.name));
    } else {
      state.selectedFleetProfiles.clear();
    }
    openAccModal();
  });

  box.querySelectorAll("[data-fleet-acc]").forEach(el => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      const acc = el.dataset.fleetAcc;
      if (state.selectedFleetProfiles.has(acc)) {
        state.selectedFleetProfiles.delete(acc);
      } else {
        state.selectedFleetProfiles.add(acc);
      }
      openAccModal();
    });
  });

  $("#acc-modal").hidden = false;
}

function closeAccModal() {
  const m = $("#acc-modal");
  if (m) m.hidden = true;
}

$("#acc-modal-close")?.addEventListener("click", closeAccModal);
$("#acc-modal-cancel")?.addEventListener("click", closeAccModal);
$("#acc-modal .acc-modal-backdrop")?.addEventListener("click", closeAccModal);

$("#run-btn")?.addEventListener("click", () => {
  const queueCount = state.stagedQueue.length;
  if (queueCount === 0 && (state.selected.size === 0 || state.selectedPrompts.size === 0)) return;
  openAccModal();
});

$("#acc-modal-go")?.addEventListener("click", async () => {
  if (state.resumeMode) {
    const profiles = [...state.selectedFleetProfiles];
    if (profiles.length === 0) {
      showToast("Cần chọn ít nhất 1 tài khoản tham gia!", "error");
      return;
    }
    closeAccModal();
    state.resumeMode = false;
    state.isStopped = false;

    try {
      const res = await fetch("/api/collections/resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profiles: profiles })
      });

      if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || "Lỗi khi chạy tiếp các bộ", "error");
        return;
      }

      const d = await res.json();
      showToast(`▶ Bắt đầu chạy tiếp ${d.started_collections || d.resumed || 0} bộ còn lại!`, "success");

      const resPanel = $("#results-panel");
      if (resPanel) {
        resPanel.hidden = false;
        resPanel.scrollIntoView({ behavior: "smooth" });
      }

      startPolling();
    } catch (err) {
      showToast("Lỗi kết nối khi chạy tiếp", "error");
    }
    return;
  }

  const queueCount = state.stagedQueue.length;
  const isDirect = (queueCount === 0);

  if (isDirect && (state.selected.size === 0 || state.selectedPrompts.size === 0)) return;

  const promptCount = Math.max(1, state.selectedPrompts.size);
  const isBulk = queueCount > 1 || (isDirect && promptCount > 1);

  if (queueCount > 0) {
    const profiles = [...state.selectedFleetProfiles];
    closeAccModal();
    state.isStopped = false;

    const payload = {
      profiles: profiles.length ? profiles : state.profiles.map(p => p.name),
      collections: state.stagedQueue,
      skip_done: $("#skip-done")?.checked !== false
    };

    try {
      const res = await fetch("/api/collections/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || "Lỗi chạy hàng đợi", "error");
        return;
      }

      const d = await res.json();
      state.stagedQueue = [];
      renderStagedQueue();
      updateRunMatrix();
      showToast(`Bắt đầu chạy ${d.started_collections} Collections trên ${d.profiles.length} tài khoản!`, "success");

      const resPanel = $("#results-panel");
      if (resPanel) {
        resPanel.hidden = false;
        resPanel.scrollIntoView({ behavior: "smooth" });
      }

      startPolling();
    } catch (err) {
      showToast("Lỗi kết nối", "error");
    }
  } else if (!isBulk) {
    // Direct single
    if (!state.genProfile) return;
    localStorage.setItem("genProfile", state.genProfile);
    closeAccModal();
    state.isStopped = false;

    const payload = {
      templates: [...state.selected],
      prompt_id: [...state.selectedPrompts][0],
      profile: state.genProfile
    };

    try {
      const res = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || "Lỗi tạo mockup", "error");
        return;
      }

      const d = await res.json();
      showToast(`Bắt đầu tạo ${d.started} mockup trên "${d.profile}"`, "success");

      const resPanel = $("#results-panel");
      if (resPanel) {
        resPanel.hidden = false;
        resPanel.scrollIntoView({ behavior: "smooth" });
      }

      startPolling();
    } catch (err) {
      showToast("Lỗi kết nối", "error");
    }
  } else {
    // Direct bulk
    const profiles = [...state.selectedFleetProfiles];
    if (profiles.length === 0) {
      showToast("Cần chọn ít nhất 1 tài khoản tham gia!", "error");
      return;
    }
    closeAccModal();
    state.isStopped = false;

    const payload = {
      profiles: profiles,
      templates: [...state.selected],
      prompt_ids: [...state.selectedPrompts],
      count: 1,
      skip_done: $("#skip-done")?.checked !== false
    };

    try {
      const res = await fetch("/api/collections/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || "Lỗi tạo chiến dịch", "error");
        return;
      }

      const d = await res.json();
      const skipMsg = d.skipped_jobs
        ? ` (bỏ qua ${d.skipped_jobs} ảnh đã có${d.skipped_collections ? `, ${d.skipped_collections} bộ trọn vẹn` : ""})`
        : "";
      showToast(`Bắt đầu: ${d.started_collections} collection trên ${d.profiles.length} tài khoản${skipMsg}`, "success");

      const resPanel = $("#results-panel");
      if (resPanel) {
        resPanel.hidden = false;
        resPanel.scrollIntoView({ behavior: "smooth" });
      }

      startPolling();
    } catch (err) {
      showToast("Lỗi kết nối", "error");
    }
  }
});

function updateBatchMonitor(isRunning) {
  const card = $("#batch-progress-card");
  if (!card) return;
  const cols = state.collections || [];
  if (cols.length === 0) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const total = cols.length;
  const done = cols.filter(c => c.status === "done" || (c.done_count && c.done_count >= c.total_count)).length;
  const remaining = total - done;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  const badge = $("#batch-badge");
  const text = $("#batch-status-text");
  const fill = $("#batch-subtrack-fill");
  const resumeBtn = $("#btn-resume-batch");

  if (badge) badge.textContent = `${done} / ${total} Bộ`;
  if (fill) fill.style.width = `${pct}%`;

  if (isRunning) {
    if (text) text.textContent = `⚡ Đang gen: Hoàn thành ${done}/${total} bộ (${pct}%). Còn ${remaining} bộ...`;
    if (resumeBtn) resumeBtn.hidden = true;
  } else if (state.isStopped) {
    if (text) text.textContent = `🛑 Đã dừng khẩn cấp: ${done}/${total} bộ hoàn thành. Còn ${remaining} bộ chưa xong.`;
    if (resumeBtn) {
      resumeBtn.hidden = (remaining === 0);
      resumeBtn.textContent = `▶ Chạy tiếp ${remaining} bộ còn lại`;
    }
  } else if (remaining === 0) {
    if (text) text.textContent = `🎉 Hoàn thành xuất sắc 100% (${total}/${total} bộ)!`;
    if (resumeBtn) resumeBtn.hidden = true;
  } else {
    const hasExhausted = (state.exhaustedProfiles && state.exhaustedProfiles.length > 0);
    if (hasExhausted) {
      if (text) text.textContent = `⚠️ Hết lượt/token tài khoản! Đã tạo ${done}/${total} bộ. Còn ${remaining} bộ cần chạy tiếp.`;
    } else {
      if (text) text.textContent = `Tiến độ đợt: Đã tạo ${done}/${total} bộ. Còn ${remaining} bộ chưa xong (có thể chạy tiếp).`;
    }
    if (resumeBtn) {
      resumeBtn.hidden = false;
      resumeBtn.textContent = `▶ Chạy tiếp ${remaining} bộ còn lại`;
    }
  }
}

async function triggerEmergencyStop() {
  try {
    const res = await fetch("/api/jobs/stop", { method: "POST" });
    const d = await res.json();
    state.isStopped = true;
    showToast(`🛑 Đã dừng khẩn cấp! (Dừng tại: ${d.stopped_at || "tiến trình"})`, "warn");

    const stopBtn = $("#btn-emergency-stop");
    const stopResultsBtn = $("#btn-stop-results");
    if (stopBtn) stopBtn.hidden = true;
    if (stopResultsBtn) stopResultsBtn.hidden = true;

    if (state.polling) {
      clearInterval(state.polling);
      state.polling = null;
    }

    const resJobs = await fetch("/api/jobs");
    const dj = await resJobs.json();
    state.jobs = dj.jobs || [];
    state.collections = dj.collections || [];
    if (state.collections.length > 0) renderCollections(state.collections);
    else renderResults(state.jobs);

    updateRunMatrix();
    updateBatchMonitor(false);
  } catch (err) {
    showToast("Lỗi khi gửi lệnh dừng", "error");
  }
}


// ==========================================================================
// 4. Results & Fleet Polling
// ==========================================================================

function startPolling() {
  if (state.polling) clearInterval(state.polling);

  const poll = async () => {
    try {
      const res = await fetch("/api/jobs");
      const d = await res.json();
      state.jobs = d.jobs || [];
      state.collections = d.collections || [];
      state.fleet = d.fleet || {};
      state.runProfile = d.profile || null;
      state.exhaustedProfiles = d.exhausted || [];
      renderNotices(d.exhausted || [], d.warnings || []);

      renderFleetBar(d.fleet, d.exhausted || []);

      if (state.collections && state.collections.length > 0) {
        renderCollections(state.collections);
      } else {
        renderResults(state.jobs);
      }

      const hasCollections = state.collections && state.collections.length > 0;
      const isColsRunning = hasCollections && state.collections.some(c => ["pending", "running"].includes(c.status));
      const hasJobs = state.jobs && state.jobs.length > 0;
      const isJobsRunning = hasJobs && state.jobs.some(j => ["pending", "running"].includes(j.status));
      const hasWork = hasCollections || hasJobs;

      const isRunning = hasWork ? (isColsRunning || isJobsRunning) : Boolean(d.active);

      const stopBtn = $("#btn-emergency-stop");
      const stopResultsBtn = $("#btn-stop-results");
      if (isRunning) {
        if (stopBtn) stopBtn.hidden = false;
        if (stopResultsBtn) stopResultsBtn.hidden = false;
      } else {
        if (stopBtn) stopBtn.hidden = true;
        if (stopResultsBtn) stopResultsBtn.hidden = true;
      }

      updateBatchMonitor(isRunning);

      if (d.stopped) {
        clearInterval(state.polling);
        state.polling = null;
        state.isStopped = true;
        updateRunMatrix();
        updateBatchMonitor(false);
        return;
      }

      if (!isRunning) {
        clearInterval(state.polling);
        state.polling = null;
        updateRunMatrix();
        updateBatchMonitor(false);
        showToast("Đã hoàn thành!", "success");
      }
    } catch (err) {
      console.error("Lỗi poll jobs:", err);
    }
  };

  poll();
  state.polling = setInterval(poll, 2200);
  updateRunMatrix();
}

function renderFleetBar(fleet, exhausted) {
  const bar = $("#fleet-status-bar");
  const chips = $("#fleet-chips");
  if (!bar || !chips) return;

  const profiles = Object.keys(fleet || {});
  if (profiles.length === 0) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;

  const exhMap = new Map((exhausted || []).map(x => [x.profile, x.reason]));

  chips.innerHTML = profiles.map(p => {
    const info = fleet[p] || {};
    const isExh = exhMap.has(p) || info.status === "exhausted";
    const isBusy = info.status === "busy";
    const statusClass = isExh ? "exhausted" : (isBusy ? "busy" : "");

    let statusText = "Rảnh rỗi";
    if (isExh) {
      statusText = "⛔ Hết lượt tạo ảnh";
    } else if (isBusy) {
      statusText = `⚡ Đang gen: <b>${esc(info.collection_name || info.prompt_name || "Collection")}</b>`;
    } else if (info.status === "starting") {
      statusText = "Đang mở Chrome...";
    } else if (info.status === "error") {
      statusText = `Lỗi: ${esc(info.error || "")}`;
    }

    return `
      <div class="fleet-chip ${statusClass}">
        <span class="status-dot ${isExh ? "exhausted" : (isBusy ? "online" : "")}"></span>
        <span class="fleet-chip-name">${esc(p)}:</span>
        <span class="fleet-chip-status">${statusText}</span>
      </div>
    `;
  }).join("");
}

function renderCollections(cols) {
  const container = $("#collections-container");
  const singleGrid = $("#results-grid");
  const stat = $("#results-stat .stat-val") || $("#results-stat");
  const fill = $("#jobs-progress-fill");

  if (singleGrid) singleGrid.innerHTML = "";

  let totalJobs = 0;
  let doneJobs = 0;
  cols.forEach(c => {
    totalJobs += c.total_count || 0;
    doneJobs += c.done_count || 0;
  });

  if (stat) {
    stat.textContent = `${doneJobs} / ${totalJobs} hoàn thành (${cols.length} Collections)`;
  }
  if (fill) {
    const pct = totalJobs > 0 ? Math.round((doneJobs / totalJobs) * 100) : 0;
    fill.style.width = `${pct}%`;
  }

  if (!container) return;

  container.innerHTML = cols.map(c => {
    const isDone = c.status === "done";
    const isRunning = c.status === "running";
    const isPartial = c.status === "partial";
    const statusBadgeClass = isDone ? "done" : (isRunning ? "running" : (isPartial ? "partial" : ""));
    const statusText = isDone ? "Hoàn thành" : (isRunning ? "Đang chạy" : (isPartial ? "Hoàn thành một phần" : "Chờ"));

    return `
      <div class="collection-card ${statusBadgeClass}">
        <div class="collection-header">
          <div class="collection-title-group">
            <span class="collection-folder-name">📁 ${esc(c.name)}</span>
            <span class="collection-prompt-tag">💡 ${esc(c.prompt_name)}</span>
            ${c.worker ? `<span class="collection-worker-tag">👤 ${esc(c.worker)}</span>` : ""}
          </div>
          <div class="collection-actions">
            <span class="collection-stat-badge ${statusBadgeClass}">
              ${c.done_count} / ${c.total_count} ảnh (${statusText})
            </span>
            <a href="/api/jobs/zip?cid=${c.id}" class="btn action-btn sm" title="Tải trọn bộ folder ${esc(c.name)}">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
              Tải bộ này (.zip)
            </a>
          </div>
        </div>

        <div class="results-grid">
          ${(c.jobs || []).map(j => {
            const isDone = j.status === "done" && j.result_url;
            const isRunning = j.status === "running";
            const isFailed = j.status === "failed";
            return `
              <div class="job-card">
                <div class="job-preview-wrap">
                  ${isDone ? `
                    <img class="job-mockup-img" src="${j.result_url}" alt="Mockup" onclick="window.openLightbox('${j.result_url}', '${esc(j.template_name)}')">
                  ` : `
                    <div class="job-slot-empty">
                      ${isRunning ? `<span class="spin-ring"></span><span class="job-empty-lbl">Đang tạo...</span>`
                        : (isFailed ? `<span class="job-err" title="${esc(j.error || "")}">❌ Lỗi</span>` : `<span class="job-empty-lbl">Chờ...</span>`)}
                    </div>
                  `}
                </div>

                <div class="job-footer">
                  <div class="job-info-text">
                    <span class="job-filename" title="${esc(j.template_name)}">${esc(j.template_name)}</span>
                    ${j.worker ? `<span class="job-worker" title="Tài khoản/tab đang xử lý">${esc(j.worker)}</span>` : ""}
                  </div>

                  <div style="display:flex; align-items:center; gap:6px;">
                    <span class="job-status-badge ${j.status}">
                      ${isRunning ? `Đang tạo` : (isDone ? `Xong` : (isFailed ? `Lỗi` : `Chờ`))}
                    </span>
                    ${isDone ? `
                      <a href="${j.result_url}" download class="icon-action" title="Tải về">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                      </a>
                    ` : ""}
                  </div>
                </div>
              </div>
            `;
          }).join("")}
        </div>
      </div>
    `;
  }).join("");
}

// Băng cảnh báo: hết lượt tạo ảnh (chặn) + cảnh báo nhẹ (vd: mức suy nghĩ)
function renderNotices(quota, warnings) {
  const box = document.getElementById("quota-banner");
  if (!box) return;
  warnings = (warnings || []).filter(w => !w.includes("DOM ChatGPT không còn") && !w.includes("data-message-author-role"));
  if (!quota.length && !warnings.length) {
    box.hidden = true;
    box.innerHTML = "";
    state.quotaSeen = new Set();
    state.warnSeen = new Set();
    return;
  }

  state.quotaSeen = state.quotaSeen || new Set();
  quota.forEach(x => {
    if (!state.quotaSeen.has(x.profile)) {
      state.quotaSeen.add(x.profile);
      showToast(`Tài khoản "${x.profile}" đã hết lượt tạo ảnh`, "error");
    }
  });
  state.warnSeen = state.warnSeen || new Set();
  warnings.forEach(w => {
    if (!state.warnSeen.has(w)) {
      state.warnSeen.add(w);
      showToast(w, "error");
    }
  });

  const parts = [];
  if (quota.length) {
    parts.push(`
      <b>Hết lượt tạo ảnh:</b> ${quota.map(x => esc(x.profile)).join(", ")}.
      Các job còn lại đã chuyển sang tài khoản khác — thêm tài khoản hoặc chờ ChatGPT reset.
      <div class="quota-reason">${esc(quota[0].reason || "")}</div>`);
  }
  if (warnings.length) {
    parts.push(`<b>Lưu ý:</b>
      <div class="quota-reason">${warnings.map(esc).join("<br>")}</div>`);
  }
  box.hidden = false;
  box.innerHTML = parts.join('<hr class="notice-sep">');
}

function renderResults(jobs) {
  const container = $("#collections-container");
  if (container) container.innerHTML = "";

  const grid = $("#results-grid");
  const stat = $("#results-stat .stat-val") || $("#results-stat");
  const fill = $("#jobs-progress-fill");
  const resPanel = $("#results-panel");

  if (jobs.length > 0 && resPanel) {
    resPanel.hidden = false;
  }

  const doneCount = jobs.filter(j => j.status === "done").length;
  const total = jobs.length;

  if (stat) {
    stat.textContent = `${doneCount} / ${total} hoàn thành`
      + (state.runProfile ? ` · ${state.runProfile}` : "");
  }

  if (fill) {
    const pct = total > 0 ? Math.round((doneCount / total) * 100) : 0;
    fill.style.width = `${pct}%`;
  }

  if (!grid) return;

  grid.innerHTML = jobs.map(j => {
    const isDone = j.status === "done" && j.result_url;
    const isRunning = j.status === "running";
    const isFailed = j.status === "failed";

    return `
      <div class="job-card">
        <div class="job-preview-wrap">
          ${isDone ? `
            <img class="job-mockup-img" src="${j.result_url}" alt="Mockup" onclick="window.openLightbox('${j.result_url}', '${esc(j.template_name)}')">
          ` : `
            <div class="job-slot-empty">
              ${isRunning ? `<span class="spin-ring"></span><span class="job-empty-lbl">Đang tạo...</span>`
                : (isFailed ? `<span class="job-err" title="${esc(j.error || "")}">❌ Lỗi</span>` : `<span class="job-empty-lbl">Chờ...</span>`)}
            </div>
          `}
        </div>

        <div class="job-footer">
          <div class="job-info-text">
            <span class="job-filename" title="${esc(j.template_name)}">${esc(j.template_name)}</span>
            ${j.worker ? `<span class="job-worker" title="Tài khoản/tab đang xử lý">${esc(j.worker)}</span>` : ""}
          </div>

          <div style="display:flex; align-items:center; gap:6px;">
            <span class="job-status-badge ${j.status}">
              ${isRunning ? `Đang tạo` : (isDone ? `Xong` : (isFailed ? `Lỗi` : `Chờ`))}
            </span>
            ${isDone ? `
              <a href="${j.result_url}" download class="icon-action" title="Tải về">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
              </a>
            ` : ""}
          </div>
        </div>
      </div>
    `;
  }).join("");
}

$("#btn-clear-jobs")?.addEventListener("click", async () => {
  await fetch("/api/jobs", { method: "DELETE" });
  state.jobs = [];
  state.collections = [];
  renderResults([]);
  const container = $("#collections-container");
  if (container) container.innerHTML = "";
  const bar = $("#fleet-status-bar");
  if (bar) bar.hidden = true;
  const resPanel = $("#results-panel");
  if (resPanel) resPanel.hidden = true;
});


// ==========================================================================
// 5. Lightbox Modal
// ==========================================================================

window.openLightbox = function(url, caption = "") {
  const lightbox = document.getElementById("lightbox-modal");
  const lightboxImg = document.getElementById("lightbox-img");
  const lightboxCap = document.getElementById("lightbox-caption");
  const lightboxDl = document.getElementById("lightbox-download");
  
  if (!lightbox || !lightboxImg) return;
  lightboxImg.src = url;
  if (lightboxCap) lightboxCap.textContent = caption;
  if (lightboxDl) {
    lightboxDl.href = url;
    lightboxDl.download = caption || "mockup.png";
  }
  lightbox.removeAttribute("hidden");
  lightbox.style.display = "flex";
};

window.closeLightbox = function() {
  const lightbox = document.getElementById("lightbox-modal");
  if (lightbox) {
    lightbox.setAttribute("hidden", "");
    lightbox.style.display = "none";
  }
};

document.getElementById("lightbox-close")?.addEventListener("click", window.closeLightbox);
document.querySelector("#lightbox-modal .lightbox-backdrop")?.addEventListener("click", window.closeLightbox);

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    window.closeLightbox();
  }
});

// Init
async function init() {
  await Promise.all([loadProfiles(), loadTemplates(), loadPrompts()]);
  try {
    const res = await fetch("/api/jobs");
    const d = await res.json();
    state.jobs = d.jobs || [];
    state.collections = d.collections || [];
    state.fleet = d.fleet || {};

    const hasCollections = state.collections && state.collections.length > 0;
    const isColsRunning = hasCollections && state.collections.some(c => ["pending", "running"].includes(c.status));
    const hasJobs = state.jobs && state.jobs.length > 0;
    const isJobsRunning = hasJobs && state.jobs.some(j => ["pending", "running"].includes(j.status));
    const hasWork = hasCollections || hasJobs;
    const isRunning = hasWork ? (isColsRunning || isJobsRunning) : Boolean(d.active);

    if (hasCollections) {
      renderCollections(state.collections);
      renderFleetBar(d.fleet, d.exhausted || []);
      const resPanel = $("#results-panel");
      if (resPanel) resPanel.hidden = false;
      updateBatchMonitor(isRunning);
      if (isRunning) startPolling();
    } else if (hasJobs) {
      renderResults(state.jobs);
      const resPanel = $("#results-panel");
      if (resPanel) resPanel.hidden = false;
      if (isRunning) startPolling();
    }
  } catch (err) {}
  $("#btn-emergency-stop")?.addEventListener("click", triggerEmergencyStop);
  $("#btn-stop-results")?.addEventListener("click", triggerEmergencyStop);
  $("#btn-resume-batch")?.addEventListener("click", () => {
    const cols = state.collections || [];
    const done = cols.filter(c => c.status === "done" || (c.done_count && c.done_count >= c.total_count)).length;
    const remaining = cols.length - done;
    openAccModal(true, remaining);
  });
}

init();
