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
  polling: null,
  jobs: [],
  showAddProfile: false,
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
$("#toggle-profiles-view")?.addEventListener("click", () => {
  state.showAddProfile = !state.showAddProfile;
  const form = $("#add-profile");
  const btnText = $("#profiles-toggle-text");
  if (form) form.hidden = !state.showAddProfile;
  if (btnText) btnText.textContent = state.showAddProfile ? "Thu gọn" : "Thêm tài khoản";
});

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
          <div class="pcard-identity">
            <span class="status-dot ${statusDotClass}"></span>
            <span class="pcard-name" title="${esc(p.name)}">${esc(p.name)}</span>
            <span class="pcard-status-pill ${isLogging ? "warn" : (isOnline ? "online" : "")}">
              ${isLogging ? "Đang mở Chrome" : (isOnline ? "Đã sẵn sàng" : "Chưa kết nối")}
            </span>
          </div>
          <button class="icon-action danger" data-delp="${esc(p.name)}" title="Xoá tài khoản này">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"></path><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"></path></svg>
          </button>
        </div>

        <div class="pcard-bottom">

          <button class="btn ${isLogging ? "primary confirm" : (isOnline ? "action-btn" : "primary")} sm btn-login-step" data-login="${esc(p.name)}" data-stage="${isLogging ? "confirm" : "idle"}">
            ${isLogging ? `✓ Đã đăng nhập xong` : (isOnline ? "🔄 Đăng nhập lại" : "🔑 Đăng nhập ChatGPT")}
          </button>
        </div>
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

  try {
    await fetch(`/api/profiles/${name}/login`, { method: "POST" });
    btn.textContent = "Xong";
    btn.dataset.stage = "confirm";
    btn.classList.add("confirm");
    showToast(`Đã mở Chrome. Đăng nhập xong bấm "Xong".`, "info");
    loadProfiles();
  } catch (e) {
    showToast(`Không thể mở Chrome: ${e.message}`, "error");
  }
}

$("#add-profile")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#ap-name").value.trim();
  if (!name) return;

  try {
    const res = await fetch("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name })
    });
    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || "Không thể tạo tài khoản", "error");
      return;
    }
    $("#ap-name").value = "";
    $("#add-profile").hidden = true;
    state.showAddProfile = false;
    $("#profiles-toggle-text").textContent = "Thêm tài khoản";
    showToast(`Đã thêm ${name}`, "success");
    loadProfiles();
  } catch (err) {
    showToast("Lỗi kết nối máy chủ", "error");
  }
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
    
    if (!state.promptId && state.prompts.length > 0) {
      state.promptId = state.prompts[0].id;
    } else if (!state.prompts.some(p => p.id === state.promptId)) {
      state.promptId = null;
    }
    renderPrompts();
  } catch (err) {
    console.error("Lỗi load prompts:", err);
  }
}

function renderPrompts() {
  const list = $("#prompt-list");
  if (!list) return;

  if (state.prompts.length === 0) {
    list.innerHTML = `<div class="empty-state" style="padding:14px; margin:0;">Chưa có Prompt. Bấm "Tạo Prompt" để thêm.</div>`;
    updateRunMatrix();
    return;
  }

  list.innerHTML = state.prompts.map(p => {
    const isSelected = p.id === state.promptId;
    return `
      <div class="prompt-card ${isSelected ? "selected" : ""}" data-id="${p.id}" title="Click để chọn">
        <span class="prompt-radio"></span>
        <div class="prompt-content">
          <div class="prompt-title">${esc(p.name)}</div>
          <div class="prompt-snippet">${esc(p.text)}</div>
          <div class="prompt-meta">
            ${p.text.length} ký tự
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
      e.stopPropagation();                       // đừng đổi prompt đang chọn
      const card = btn.closest(".prompt-card");
      const open = card.classList.toggle("expanded");
      btn.textContent = open ? "Thu gọn" : "Xem thêm";
    });
  });

  list.querySelectorAll(".prompt-card").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-edit]") || e.target.closest("[data-delprompt]")
          || e.target.closest("[data-more]")) return;
      state.promptId = card.dataset.id;
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
      showToast("Đã xoá Prompt", "info");
      loadPrompts();
    });
  });

  updateRunMatrix();
}

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
// 3. Clean Run Bar
// ==========================================================================

function updateRunMatrix() {
  const tplCount = state.selected.size;
  const activePrompt = state.prompts.find(p => p.id === state.promptId);

  const mainMsg = $("#runbar-main-msg");
  const runBtn = $("#run-btn");
  const runBtnText = $("#run-btn-text");

  if (state.polling) {
    if (mainMsg) mainMsg.textContent = `Đang xử lý tạo mockup...`;
    if (runBtnText) runBtnText.textContent = "Đang chạy";
    if (runBtn) runBtn.disabled = true;
    return;
  }

  if (tplCount === 0) {
    if (mainMsg) mainMsg.textContent = `Chọn ít nhất 1 ảnh template`;
    if (runBtnText) runBtnText.textContent = "Tạo Mockup";
    if (runBtn) runBtn.disabled = true;
  } else if (!activePrompt) {
    if (mainMsg) mainMsg.textContent = `Chọn 1 prompt`;
    if (runBtnText) runBtnText.textContent = "Tạo Mockup";
    if (runBtn) runBtn.disabled = true;
  } else {
    if (mainMsg) mainMsg.innerHTML = `Đã chọn: <b>${tplCount}</b> ảnh · <b>${esc(activePrompt.name)}</b>`;
    if (runBtnText) runBtnText.textContent = `Tạo ${tplCount} Mockup`;
    if (runBtn) runBtn.disabled = false;
  }
}

// ---- Chọn tài khoản rồi mới gen -------------------------------------------
function openAccModal() {
  const box = $("#acc-modal-list");
  if (!box) return;

  if (state.profiles.length === 0) {
    showToast("Chưa có tài khoản nào. Thêm tài khoản trước đã.", "error");
    return;
  }
  // ưu tiên tài khoản dùng lần trước, nếu nó vẫn còn
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
}

function closeAccModal() {
  const m = $("#acc-modal");
  if (m) m.hidden = true;
}

$("#acc-modal-close")?.addEventListener("click", closeAccModal);
$("#acc-modal-cancel")?.addEventListener("click", closeAccModal);
$("#acc-modal .acc-modal-backdrop")?.addEventListener("click", closeAccModal);

$("#run-btn")?.addEventListener("click", () => {
  if (state.selected.size === 0 || !state.promptId) return;
  openAccModal();
});

$("#acc-modal-go")?.addEventListener("click", async () => {
  if (state.selected.size === 0 || !state.promptId || !state.genProfile) return;
  localStorage.setItem("genProfile", state.genProfile);
  closeAccModal();

  const payload = {
    templates: [...state.selected],
    prompt_id: state.promptId,
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
});


// ==========================================================================
// 4. Results
// ==========================================================================

function startPolling() {
  if (state.polling) clearInterval(state.polling);

  const poll = async () => {
    try {
      const res = await fetch("/api/jobs");
      const d = await res.json();
      state.jobs = d.jobs || [];
      state.runProfile = d.profile || null;
      renderNotices(d.exhausted || [], d.warnings || []);

      renderResults(state.jobs);

      const isRunning = d.active || state.jobs.some(j => ["pending", "running"].includes(j.status));
      if (!isRunning) {
        clearInterval(state.polling);
        state.polling = null;
        updateRunMatrix();
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

// Băng cảnh báo: hết lượt tạo ảnh (chặn) + cảnh báo nhẹ (vd: mức suy nghĩ)
function renderNotices(quota, warnings) {
  const box = document.getElementById("quota-banner");
  if (!box) return;
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
        <div class="job-compare-view">
          <div class="job-thumb-box">
            <span class="thumb-tag">Gốc</span>
            <img src="${j.template_url}" alt="${esc(j.template_name)}" onclick="window.openLightbox('${j.template_url}', '${esc(j.template_name)}')">
          </div>
          <div class="job-arrow-divider">→</div>
          ${isDone ? `
            <div class="job-thumb-box">
              <span class="thumb-tag success">Mockup</span>
              <img src="${j.result_url}" alt="Result" onclick="window.openLightbox('${j.result_url}', '${esc(j.template_name)}')">
            </div>
          ` : `
            <div class="job-slot-empty">
              ${isRunning ? `<span class="spin-ring"></span>`
                : (isFailed ? `<span class="job-err" title="${esc(j.error || "")}">Lỗi</span>` : `Chờ…`)}
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
  renderResults([]);
  const resPanel = $("#results-panel");
  if (resPanel) resPanel.hidden = true;
});


// ==========================================================================
// 5. Lightbox Modal (Robust Functionality)
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
    if (d.jobs && d.jobs.length > 0) {
      state.jobs = d.jobs;
      renderResults(state.jobs);
      if (d.active || state.jobs.some(j => ["pending", "running"].includes(j.status))) {
        startPolling();
      }
    }
  } catch (err) {}
}

init();
