"use strict";

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.left = "-9999px";
  document.body.appendChild(input);
  input.select();
  try {
    document.execCommand("copy");
  } finally {
    input.remove();
  }
}

function setCopyStatus(button, message, isError) {
  const statusId = button.getAttribute("aria-describedby");
  const status = statusId ? document.getElementById(statusId) : null;
  if (status) {
    status.textContent = message;
    status.classList.toggle("error", Boolean(isError));
  }
  button.textContent = isError ? "Copy" : "Copied";
  if (!isError) {
    window.setTimeout(() => {
      button.textContent = "Copy";
      if (status && status.textContent === message) {
        status.textContent = "";
      }
    }, 2200);
  }
}

// ── Theme toggle ────────────────────────────────────────────────────
// Mirrors the marketing chrome (static/dashboard.js). Dark is the default
// (no data-theme attribute); a stored "light" preference is applied as
// document.documentElement.dataset.theme = "light". The inline script in
// console/_layout.html applies the saved theme before the stylesheets load
// to avoid a flash-of-light; these helpers drive the runtime toggle.
const THEME_KEY = "tr-theme";

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function updateThemeToggleGlyph() {
  const dark = currentTheme() === "dark";
  document.querySelectorAll('[data-action="toggle-theme"]').forEach((el) => {
    el.textContent = "◐";
    el.setAttribute("aria-pressed", String(!dark));
    el.setAttribute("aria-label", dark ? "Switch to paper theme" : "Switch to dark theme");
    el.setAttribute("title", dark ? "Switch to paper theme" : "Switch to dark theme");
  });
}

function applyStoredTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem(THEME_KEY);
  } catch {
    stored = null;
  }
  if (stored === "light") {
    document.documentElement.dataset.theme = "light";
  } else {
    delete document.documentElement.dataset.theme;
  }
  updateThemeToggleGlyph();
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  if (next === "light") {
    document.documentElement.dataset.theme = "light";
  } else {
    delete document.documentElement.dataset.theme;
  }
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    /* persistence is best-effort */
  }
  updateThemeToggleGlyph();
}

function initConsole() {
  applyStoredTheme();
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!target)
      return;
    const themeToggle = target.closest('[data-action="toggle-theme"]');
    if (themeToggle) {
      event.preventDefault();
      toggleTheme();
      return;
    }
    const newKeyButton = target.closest('[data-action="open-new-key"]');
    if (newKeyButton) {
      event.preventDefault();
      const panel = document.getElementById("new-api-key");
      if (panel) {
        panel.open = true;
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
        const input = panel.querySelector('input[name="name"]');
        if (input) {
          window.setTimeout(() => input.focus(), 250);
        }
      }
      return;
    }
    const button = target.closest("[data-copy-secret]");
    if (!button)
      return;
    event.preventDefault();
    const secretId = button.getAttribute("data-copy-secret");
    const secret = secretId ? document.getElementById(secretId) : null;
    const value = secret ? secret.textContent.trim() : "";
    if (!value) {
      setCopyStatus(button, "No key to copy.", true);
      return;
    }
    copyText(value)
      .then(() => setCopyStatus(button, "Copied to clipboard.", false))
      .catch(() => setCopyStatus(button, "Select the key and copy it manually.", true));
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initConsole);
} else {
  initConsole();
}
