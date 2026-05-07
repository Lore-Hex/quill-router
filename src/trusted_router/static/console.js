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

function initConsole() {
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!target)
      return;
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
