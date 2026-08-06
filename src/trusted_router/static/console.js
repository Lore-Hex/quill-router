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

function copyTargetText(target) {
  if (target.hasAttribute("data-copy-lines")) {
    return Array.from(target.children, (line) => line.textContent.trim())
      .join("\n\n")
      .trim();
  }
  return target.textContent.trim();
}

function templateCopyText(target, secret) {
  return copyTargetText(target).replaceAll("YOUR_TRUSTEDROUTER_API_KEY", secret);
}

function postActivationEvent(event) {
  fetch("/analytics/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event }),
    credentials: "same-origin",
    keepalive: true,
  }).catch(() => {
    /* Acquisition telemetry is best-effort and must never block setup. */
  });
}

function microdollarsDisplay(value) {
  let amount = 0n;
  try {
    amount = BigInt(value || 0);
  } catch {
    amount = 0n;
  }
  const sign = amount < 0n ? "-" : "";
  const absolute = amount < 0n ? -amount : amount;
  const dollars = absolute / 1000000n;
  const fraction = String(absolute % 1000000n).padStart(6, "0");
  return `${sign}$${dollars}.${fraction}`;
}

function completionText(payload) {
  const content = payload?.choices?.[0]?.message?.content;
  if (typeof content === "string")
    return content.trim();
  if (Array.isArray(content)) {
    return content
      .filter((part) => part && (part.type === "text" || part.type === "output_text"))
      .map((part) => part.text || "")
      .join("")
      .trim();
  }
  return "";
}

function completionMetadata(payload, response) {
  const usage = payload?.usage && typeof payload.usage === "object" ? payload.usage : {};
  const providerUsage = usage.provider_usage && typeof usage.provider_usage === "object"
    ? usage.provider_usage
    : {};
  const trusted = payload?.trustedrouter && typeof payload.trustedrouter === "object"
    ? payload.trustedrouter
    : {};
  const routing = trusted.routing && typeof trusted.routing === "object"
    ? trusted.routing
    : {};
  return {
    model: response.headers.get("x-trustedrouter-served-model")
      || routing.selected_model
      || providerUsage.selected_model
      || payload?.model
      || "trustedrouter/cheap",
    provider: response.headers.get("x-trustedrouter-provider")
      || routing.selected_provider
      || providerUsage.selected_provider
      || payload?.provider
      || "Selected automatically",
    costMicrodollars: usage.total_cost_microdollars
      ?? usage.cost_microdollars
      ?? providerUsage.total_cost_microdollars
      ?? providerUsage.cost_microdollars
      ?? 0,
  };
}

function activationErrorCopy(status) {
  if (status === 401) {
    return {
      title: "This key was not accepted",
      message: "Create a new API key, then run the check again.",
      action: "/console/api-keys#new-api-key",
      actionLabel: "Create a new key",
    };
  }
  if (status === 402) {
    return {
      title: "Credits are required",
      message: "Add credits, then return here to run the live request.",
      action: "/console/credits",
      actionLabel: "Add credits",
    };
  }
  if (status === 429) {
    return {
      title: "The request was rate limited",
      message: "Wait a moment, then run the check again.",
    };
  }
  return {
    title: "The live request did not complete",
    message: "Your key is safe. Try again while TrustedRouter selects another healthy route.",
  };
}

function showActivationError(flow, status) {
  const copy = activationErrorCopy(status);
  const container = flow.querySelector("[data-call-error]");
  const result = flow.querySelector("[data-call-result]");
  if (result)
    result.hidden = true;
  if (!container)
    return;
  container.querySelector("[data-call-error-title]").textContent = copy.title;
  container.querySelector("[data-call-error-message]").textContent = copy.message;
  const action = container.querySelector("[data-call-error-action]");
  if (action) {
    action.hidden = !copy.action;
    if (copy.action) {
      action.href = copy.action;
      action.textContent = copy.actionLabel;
    }
  }
  container.hidden = false;
}

function markActivationComplete(flow) {
  const steps = flow.querySelectorAll(".activation-progress li");
  if (steps[1]) {
    steps[1].classList.remove("current");
    steps[1].classList.add("complete");
  }
  if (steps[2])
    steps[2].classList.add("current");
}

async function runFirstActivationCall(button) {
  const flow = button.closest("[data-first-call-flow]");
  if (!flow || button.disabled)
    return;
  const keySource = document.getElementById(flow.dataset.keySource || "");
  const apiKey = keySource ? copyTargetText(keySource) : "";
  if (!apiKey) {
    showActivationError(flow, 401);
    return;
  }

  const endpoint = flow.dataset.endpoint;
  const label = button.querySelector("[data-run-label]");
  const error = flow.querySelector("[data-call-error]");
  if (error)
    error.hidden = true;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  if (label)
    label.textContent = "Routing live request...";
  postActivationEvent("first_call_started");

  const started = performance.now();
  let status = 0;
  try {
    const requestId = window.crypto?.randomUUID
      ? window.crypto.randomUUID()
      : `first-call-${Date.now()}`;
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
        "Idempotency-Key": `welcome-${requestId}`,
      },
      body: JSON.stringify({
        model: "trustedrouter/cheap",
        messages: [{ role: "user", content: "Reply with exactly PONG." }],
        temperature: 0,
        max_tokens: 8,
        stream: false,
      }),
    });
    status = response.status;
    if (!response.ok)
      throw new Error("request_failed");
    const payload = await response.json();
    const output = completionText(payload);
    if (!output)
      throw new Error("empty_response");
    const metadata = completionMetadata(payload, response);
    const elapsed = Math.max(1, Math.round(performance.now() - started));
    const result = flow.querySelector("[data-call-result]");
    result.querySelector("[data-result-output]").textContent = output;
    result.querySelector("[data-result-model]").textContent = metadata.model;
    result.querySelector("[data-result-provider]").textContent = metadata.provider;
    result.querySelector("[data-result-latency]").textContent = `${elapsed.toLocaleString()} ms`;
    result.querySelector("[data-result-cost]").textContent = microdollarsDisplay(
      metadata.costMicrodollars,
    );
    result.hidden = false;
    markActivationComplete(flow);
    if (label)
      label.textContent = "Test passed";
    button.classList.add("activation-run-success");
  } catch {
    postActivationEvent("first_call_failed");
    showActivationError(flow, status);
    button.disabled = false;
    if (label)
      label.textContent = "Try the live request again";
  } finally {
    button.removeAttribute("aria-busy");
  }
}

function selectSetupTab(tab) {
  const tablist = tab.closest("[role=tablist]");
  if (!tablist)
    return;
  const panelId = tab.dataset.setupTab;
  tablist.querySelectorAll("[data-setup-tab]").forEach((candidate) => {
    const selected = candidate === tab;
    candidate.setAttribute("aria-selected", String(selected));
    candidate.tabIndex = selected ? 0 : -1;
  });
  const scope = tablist.parentElement;
  scope.querySelectorAll("[data-setup-panel]").forEach((panel) => {
    panel.hidden = panel.id !== panelId;
  });
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
  if (window.location.hash === "#new-api-key") {
    const panel = document.getElementById("new-api-key");
    if (panel) {
      panel.open = true;
      window.setTimeout(() => panel.querySelector('input[name="name"]')?.focus(), 100);
    }
  }
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
    const runButton = target.closest('[data-action="run-first-call"]');
    if (runButton) {
      event.preventDefault();
      runFirstActivationCall(runButton);
      return;
    }
    const setupTab = target.closest("[data-setup-tab]");
    if (setupTab) {
      event.preventDefault();
      selectSetupTab(setupTab);
      return;
    }
    const templateButton = target.closest("[data-copy-template-target]");
    if (templateButton) {
      event.preventDefault();
      const templateId = templateButton.getAttribute("data-copy-template-target");
      const sourceId = templateButton.getAttribute("data-secret-source");
      const template = templateId ? document.getElementById(templateId) : null;
      const source = sourceId ? document.getElementById(sourceId) : null;
      const secret = source ? copyTargetText(source) : "";
      const value = template && secret ? templateCopyText(template, secret) : "";
      if (!value) {
        setCopyStatus(templateButton, "No key to copy.", true);
        return;
      }
      copyText(value)
        .then(() => setCopyStatus(templateButton, "Copied to clipboard.", false))
        .catch(() => setCopyStatus(
          templateButton,
          "Select the setup and copy it manually.",
          true,
        ));
      return;
    }
    const button = target.closest("[data-copy-secret]");
    if (!button)
      return;
    event.preventDefault();
    const secretId = button.getAttribute("data-copy-secret");
    const secret = secretId ? document.getElementById(secretId) : null;
    const value = secret ? copyTargetText(secret) : "";
    if (!value) {
      setCopyStatus(button, "No key to copy.", true);
      return;
    }
    copyText(value)
      .then(() => setCopyStatus(button, "Copied to clipboard.", false))
      .catch(() => setCopyStatus(button, "Select the key and copy it manually.", true));
  });

  document.addEventListener("keydown", (event) => {
    const tab = event.target.closest?.("[data-setup-tab]");
    if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
      return;
    const tabs = Array.from(tab.closest("[role=tablist]").querySelectorAll("[data-setup-tab]"));
    const current = tabs.indexOf(tab);
    let next = current;
    if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
    if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    event.preventDefault();
    selectSetupTab(tabs[next]);
    tabs[next].focus();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initConsole);
} else {
  initConsole();
}
