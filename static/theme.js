// One persisted theme for the workspace and every standalone/embedded mode.
(() => {
  const key = "mf-theme";
  const read = () => {
    try { return localStorage.getItem(key) === "dark" ? "dark" : "light"; }
    catch { return "light"; }
  };
  const apply = (theme) => {
    const value = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = value;
    document.documentElement.style.colorScheme = value;
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.setAttribute("aria-label", value === "light" ? "Chuyển sang chế độ tối" : "Chuyển sang chế độ sáng");
      button.setAttribute("aria-pressed", String(value === "light"));
      const label = button.querySelector(".theme-label");
      if (label) label.textContent = value === "light" ? "Chế độ sáng" : "Chế độ tối";
    }
  };
  apply(read());
  document.addEventListener("DOMContentLoaded", () => {
    apply(read());
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      apply(next);
      try { localStorage.setItem(key, next); } catch { /* Session-only theme. */ }
    });
  });
  window.addEventListener("storage", (event) => {
    if (event.key === key) apply(read());
  });
})();
