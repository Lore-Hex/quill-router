// Run before CSS to avoid a light flash on reload. Only the theme is persisted.
(() => {
  let theme;
  try { theme = localStorage.getItem("lightningrouter-theme"); } catch {}
  document.documentElement.dataset.theme = theme === "dark" || theme === "light"
    ? theme : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
})();
