const modes = ["mockup", "csv-session"];
const frames = Object.fromEntries(modes.map((mode) => [mode,
  document.getElementById(`frame-${mode}`)]));
let activeMode = "";
let accountsPending = false;

function resizeFrame(frame) {
  if (!frame || frame.hidden) return;
  try {
    const doc = frame.contentDocument;
    if (!doc?.body) return;
    const height = Math.max(doc.body.scrollHeight, doc.documentElement.scrollHeight);
    if (height) frame.style.height = `${Math.max(620, height + 12)}px`;
  } catch (error) {
    // Nội dung luôn cùng origin; nếu trang con đang điều hướng thì đo ở lượt sau.
  }
}

function selectMode(mode, updateHash = true) {
  if (!modes.includes(mode)) mode = "csv-session";
  activeMode = mode;
  for (const name of modes) {
    const selected = name === mode;
    const frame = frames[name];
    frame.hidden = !selected;
    document.getElementById(`tab-${name}`).setAttribute("aria-selected", String(selected));
    document.getElementById(`tab-${name}`).tabIndex = selected ? 0 : -1;
    if (selected && !frame.src) frame.src = frame.dataset.src;
    if (selected) setTimeout(() => resizeFrame(frame), 100);
  }
  if (updateHash && location.hash !== `#${mode}`) history.replaceState(null, "", `#${mode}`);
}

function openAccounts() {
  accountsPending = true;
  selectMode("mockup");
  const doc = frames.mockup.contentDocument;
  if (doc?.readyState !== "complete") return;
  const button = doc.getElementById("open-accounts");
  if (button) {
    button.click();
    accountsPending = false;
  }
}

for (const [mode, frame] of Object.entries(frames)) {
  frame.addEventListener("load", () => {
    try {
      const doc = frame.contentDocument;
      const style = doc.createElement("style");
      style.textContent = `html,body{min-height:0!important;background:transparent!important}
        header.topbar{display:none!important}
        main.app-container{max-width:none!important;margin:0!important;padding:0!important;gap:14px!important}`;
      doc.head.appendChild(style);
      resizeFrame(frame);
      new ResizeObserver(() => resizeFrame(frame)).observe(doc.body);
      if (mode === "mockup" && accountsPending) openAccounts();
    } catch (error) {
      document.getElementById("workspace-alert").textContent =
        "Không nhúng được chế độ này. Hãy làm mới trang.";
      document.getElementById("workspace-alert").hidden = false;
    }
  });
}

document.querySelectorAll(".mode-tab").forEach((button) => {
  button.addEventListener("click", () => selectMode(button.dataset.mode));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const next = (modes.indexOf(activeMode) + (event.key === "ArrowRight" ? 1 : -1) + modes.length) % modes.length;
    selectMode(modes[next]);
    document.getElementById(`tab-${modes[next]}`).focus();
  });
});

document.getElementById("btn-accounts").onclick = openAccounts;

async function refreshGlobal() {
  const alert = document.getElementById("workspace-alert");
  try {
    const jobsResponse = await fetch("/api/jobs");
    if (!jobsResponse.ok) throw new Error("Không cập nhật được. Hãy thử lại.");
    const data = await jobsResponse.json();
    const collections = data.collections || [];
    const waiting = collections.filter((row) => row.status === "pending").length;
    const running = collections.filter((row) => row.status === "running").length;
    const done = collections.filter((row) => row.status === "done").length;
    document.getElementById("summary-run").innerHTML = data.active
      ? "<strong>● Đang chạy</strong>" : data.stopped ? "<strong>■ Đã dừng</strong>" : "<strong>○ Đang rảnh</strong>";
    document.getElementById("summary-queue").textContent =
      waiting || running ? `${done}/${collections.length} bộ xong · ${running} đang tạo · ${waiting} chờ lượt`
        : `${done}/${collections.length} bộ xong`;
    const messages = data.warnings || [];
    alert.textContent = messages.length ? "Có vấn đề khi tạo ảnh. Hãy xem bộ bị lỗi để biết thêm." : "";
    alert.hidden = !messages.length;
  } catch (error) {
    alert.textContent = "Không cập nhật được trạng thái. Hãy thử tải lại trang.";
    alert.hidden = false;
  }
}

window.addEventListener("hashchange", () => selectMode(location.hash.slice(1), false));
selectMode(location.hash.slice(1), false);
refreshGlobal();
setInterval(refreshGlobal, 2500);
setInterval(() => resizeFrame(frames[activeMode]), 1500);
