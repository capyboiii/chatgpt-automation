// Trang CSV: nhập CSV là chạy luôn. Mỗi dòng = một phiên chat gửi nguyên văn
// PROMPT, ảnh trả về lưu vào designs/<TOPIC>__<Style>/1.png, 2.png...
// Tách hẳn khỏi app.js - không đụng gì tới trang chính.

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let polling = null;
let lastSig = "";

function toast(msg, type = "info") {
  const box = $("#toast-container");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

async function loadProfiles() {
  try {
    const d = await (await fetch("/api/profiles")).json();
    $("#pool-info").textContent = `${(d.profiles || []).length} tài khoản`;
  } catch (e) { /* bỏ qua */ }
}

// ---------------------------------------------------------------- chạy
$("#btn-csv").addEventListener("click", () => $("#csv-input").click());

$("#csv-input").addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  e.target.value = "";
  if (!file) return;

  const btn = $("#btn-csv");
  btn.disabled = true;
  btn.textContent = "Đang gửi…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/csvrun/start", { method: "POST", body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || "không chạy được");
    const extra = [d.skipped_done ? `bỏ ${d.skipped_done} dòng đã có ảnh` : "",
                   d.skipped_empty ? `${d.skipped_empty} dòng trống` : ""]
                  .filter(Boolean).join(", ");
    toast(`Bắt đầu ${d.started} dòng trên ${d.profiles.length} tài khoản`
          + (extra ? ` (${extra})` : ""), "success");
    startPolling();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Chọn CSV & chạy";
  }
});

$("#btn-stop").addEventListener("click", async () => {
  try {
    await fetch("/api/jobs/stop", { method: "POST" });
    toast("Đã dừng khẩn cấp", "warn");
  } catch (e) {
    toast("Không dừng được", "error");
  }
});

// ---------------------------------------------------------------- dải trạng thái
function renderStrip(cols, active, stopped) {
  const total = cols.length;
  const done = cols.filter(c => c.status === "done").length;
  const finished = cols.filter(c => ["done", "failed"].includes(c.status)).length;
  const images = cols.reduce((n, c) => n + ((c.jobs || [])[0]?.results || []).length, 0);

  $("#stat").textContent = `${images} ảnh · ${done}/${total} bộ`;
  $("#run-badge").textContent = `${done}/${total} bộ`;
  $("#run-fill").style.width = total ? `${Math.round(finished / total * 100)}%` : "0%";

  const left = total - finished;
  $("#run-text").textContent = active ? `Đang chạy · còn ${left} bộ`
    : stopped ? `Đã dừng · ${left} bộ chưa xong`
    : left === 0 ? "Xong tất cả"
    : `Tạm dừng · ${left} bộ chưa xong`;
  $("#run-strip").classList.toggle("is-running", !!active);
}

function renderFleet(fleet, exhausted) {
  const exh = new Set((exhausted || []).map(x => x.profile));
  $("#fleet-chips").innerHTML = Object.keys(fleet || {}).map(p => {
    const info = fleet[p] || {};
    const isExh = exh.has(p) || info.status === "exhausted";
    const busy = info.status === "busy";
    const text = isExh ? "hết lượt"
      : busy ? `<b>${esc(info.collection_name || "đang gen")}</b>`
      : info.status === "starting" ? "đang mở Chrome…"
      : info.status === "error" ? `lỗi: ${esc(info.error || "")}`
      : "rảnh";
    return `<div class="fleet-chip ${isExh ? "exhausted" : (busy ? "busy" : "")}">
        <span class="status-dot ${isExh ? "exhausted" : (busy ? "online" : "")}"></span>
        <span class="fleet-chip-name">${esc(p)}</span>
        <span class="fleet-chip-status">${text}</span>
      </div>`;
  }).join("");
}

// ---------------------------------------------------------------- danh sách bộ
function render(cols) {
  const list = $("#list");
  const sig = JSON.stringify(cols.map(c => [c.id, c.status, c.error,
    (c.jobs || []).map(j => [j.status, j.results])]));
  if (sig === lastSig) return;          // không đổi thì khỏi vẽ lại, đỡ chớp
  lastSig = sig;

  list.innerHTML = cols.map(c => {
    const j = (c.jobs || [])[0] || {};
    const results = j.results || [];
    const label = { done: "Xong", running: "Đang chạy", failed: "Lỗi",
                    paused: "Tạm dừng" }[c.status] || "Chờ";
    const cls = c.status === "done" ? "done"
              : c.status === "running" ? "running"
              : c.status === "failed" ? "partial" : "";
    return `
      <div class="collection-card ${cls}">
        <div class="collection-header">
          <div class="collection-title-group" style="flex-direction:column;align-items:flex-start;gap:2px">
            <span class="collection-folder-name">${esc(c.name)}</span>
            <span class="csv-folder">designs/${esc(j.folder || "")}</span>
          </div>
          <div class="collection-actions">
            <span class="collection-stat-badge ${cls}">
              ${results.length ? `${results.length} ảnh · ` : ""}${label}
            </span>
            ${results.length ? `
            <a href="/api/jobs/zip?cid=${esc(c.id)}" class="icon-action" title="Tải bộ này (.zip)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
            </a>` : ""}
          </div>
        </div>
        ${results.length ? `
          <div class="csv-thumbs">
            ${results.map(u => `
              <a class="csv-thumb" href="${esc(u)}" target="_blank" rel="noopener">
                <img src="${esc(u)}" loading="lazy" alt="">
                <span>${esc(u.split("/").pop())}</span>
              </a>`).join("")}
          </div>`
          : `<div class="csv-wait">${c.status === "running" ? "ChatGPT đang vẽ…" : ""}</div>`}
        ${c.error ? `<div class="csv-err">${esc(c.error)}</div>` : ""}
      </div>`;
  }).join("");
}

function startPolling() {
  if (polling) clearInterval(polling);
  const tick = async () => {
    try {
      const d = await (await fetch("/api/jobs")).json();
      const cols = (d.collections || []).filter(c => c.free);
      $("#results-panel").hidden = cols.length === 0;
      if (cols.length) {
        renderStrip(cols, d.active, d.stopped);
        renderFleet(d.fleet, d.exhausted);
        render(cols);
      }
      $("#btn-stop").hidden = !d.active;
      if (!d.active) {
        clearInterval(polling);
        polling = null;
      }
    } catch (e) { /* vòng sau hỏi lại */ }
  };
  tick();
  polling = setInterval(tick, 2200);
}

loadProfiles();
startPolling();          // mở trang giữa lúc đang chạy thì bám theo luôn
