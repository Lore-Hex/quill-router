"use strict";

(function () {
  const CONFIG = window.__TR_FUSION__ || {};
  const API_BASE = CONFIG.apiBaseUrl || "/chat-proxy/v1";
  const ISSUE_KEY_PATH = CONFIG.issueKeyPath || "/internal/chat/issue-browser-key";
  const KEY_COOKIE = CONFIG.keyCookieName || "tr_chat_key";
  const KEY_STORAGE = "tr_chat_key";
  const HISTORY_KEY = "tr_fusion_runs_v1";

  const DEFAULT_PANEL = configList("defaultPanel");
  const DEFAULT_JUDGES = configList("defaultJudges");
  const DEFAULT_FINALS = configList("defaultFinals");

  const els = {
    form: document.querySelector("[data-fusion-form]"),
    prompt: document.querySelector("[data-fusion-prompt]"),
    preset: document.querySelector("[data-fusion-preset]"),
    maxTokens: document.querySelector("[data-fusion-max-tokens]"),
    panel: document.querySelector("[data-fusion-panel]"),
    judges: document.querySelector("[data-fusion-judges]"),
    finals: document.querySelector("[data-fusion-finals]"),
    answer: document.querySelector("[data-fusion-answer]"),
    error: document.querySelector("[data-fusion-error]"),
    meta: document.querySelector("[data-fusion-meta]"),
    code: document.querySelector("[data-fusion-code]"),
    title: document.querySelector("[data-result-title]"),
    runList: document.querySelector("[data-run-list]"),
    newRun: document.querySelector("[data-action='new-fusion']"),
    copyCode: document.querySelector("[data-action='copy-code']"),
  };

  function isSignedIn() {
    if (typeof window.hasSignedInHint === "function") return window.hasSignedInHint();
    return document.cookie
      .split(";")
      .map((c) => c.trim())
      .some((c) => c === "tr_signed_in=1");
  }

  function configList(name) {
    const value = CONFIG[name];
    if (!Array.isArray(value)) return [];
    return value.map((item) => String(item).trim()).filter(Boolean).slice(0, 8);
  }

  function openSigninModal() {
    if (typeof window.openSigninModal === "function") {
      window.openSigninModal();
      return;
    }
    const dialog = document.getElementById("signinModal");
    if (dialog && typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
  }

  async function ensureBrowserKey(forceRefresh) {
    if (!forceRefresh) {
      try {
        const existing = sessionStorage.getItem(KEY_STORAGE);
        if (existing) return existing;
      } catch (_) {}
    } else {
      try { sessionStorage.removeItem(KEY_STORAGE); } catch (_) {}
    }

    const resp = await fetch(ISSUE_KEY_PATH, { method: "POST", credentials: "same-origin" });
    if (resp.status === 302 || resp.status === 401) {
      openSigninModal();
      throw new Error("Sign in to run Synth.");
    }
    if (!resp.ok) throw new Error("Could not issue a browser API key.");
    const json = await resp.json();
    const raw = json?.data?.raw_key;
    if (!raw) throw new Error("Browser API key response was missing raw_key.");
    try { sessionStorage.setItem(KEY_STORAGE, raw); } catch (_) {}
    clearKeyCookie();
    return raw;
  }

  function clearKeyCookie() {
    for (const path of ["/", "/chat", "/fusion", "/synth"]) {
      document.cookie = `${KEY_COOKIE}=; path=${path}; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
    }
  }

  function splitModels(value) {
    return String(value || "")
      .split(/[\n,]+/g)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 8);
  }

  function buildRequest() {
    const panel = splitModels(els.panel.value);
    const judges = splitModels(els.judges.value);
    const finals = splitModels(els.finals.value);
    const maxTokens = Math.max(64, Math.min(4096, Number(els.maxTokens.value || 900)));
    return {
      model: "trustedrouter/synth",
      messages: [{ role: "user", content: els.prompt.value.trim() }],
      max_tokens: maxTokens,
      tools: [{
        type: "trustedrouter:synth",
        parameters: {
          preset: els.preset.value || "quality",
          selection_strategy: "synthesize_non_refusals",
          analysis_models: panel.length ? panel : DEFAULT_PANEL,
          judge_models: judges.length ? judges : DEFAULT_JUDGES,
          final_models: finals.length ? finals : DEFAULT_FINALS,
          max_completion_tokens: maxTokens,
        },
      }],
    };
  }

  function renderCode() {
    const request = buildRequest();
    const pretty = JSON.stringify(request, null, 2);
    els.code.textContent = `curl ${API_BASE}/chat/completions \\\n  -H "Authorization: Bearer $TRUSTEDROUTER_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${pretty.replace(/'/g, "'\\''")}'`;
  }

  function setError(message) {
    els.error.textContent = message;
    els.error.hidden = !message;
  }

  function completionText(json) {
    return json?.choices?.[0]?.message?.content || "";
  }

  function formatMeta(json, startedAt) {
    const ms = Math.max(0, Math.round(performance.now() - startedAt));
    const usage = json?.usage || {};
    const route = json?.trustedrouter?.provider || json?.provider || "synth";
    const total = usage.total_tokens || 0;
    return `${ms} ms · ${total ? `${total} tokens · ` : ""}${route}`;
  }

  async function postFusion(key, request) {
    const resp = await fetch(`${API_BASE}/chat/completions`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${key}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(request),
    });
    const text = await resp.text();
    let json = null;
    try { json = text ? JSON.parse(text) : null; } catch (_) {}
    if (resp.status === 401) {
      const fresh = await ensureBrowserKey(true);
      return postFusion(fresh, request);
    }
    if (!resp.ok) {
      const msg = json?.error?.message || text || `Synth failed with ${resp.status}`;
      throw new Error(msg);
    }
    return json;
  }

  async function runFusion(event) {
    event.preventDefault();
    setError("");
    if (!isSignedIn()) {
      openSigninModal();
      return;
    }
    const request = buildRequest();
    if (!request.messages[0].content) {
      setError("Enter a prompt first.");
      return;
    }
    els.answer.textContent = "Running panel, judge, and final synthesis...";
    els.answer.classList.add("loading");
    els.title.textContent = "Running";
    els.meta.textContent = "";
    const startedAt = performance.now();
    try {
      const key = await ensureBrowserKey(false);
      const json = await postFusion(key, request);
      const output = completionText(json);
      if (!output) throw new Error("Synth returned an empty response.");
      els.answer.textContent = output;
      els.answer.classList.remove("loading");
      els.title.textContent = "Completed";
      els.meta.textContent = formatMeta(json, startedAt);
      saveRun({ prompt: request.messages[0].content, output, created_at: new Date().toISOString() });
    } catch (err) {
      els.answer.classList.remove("loading");
      els.title.textContent = "Error";
      els.answer.textContent = "Synth did not complete.";
      setError(err?.message || "Synth failed.");
    }
  }

  function loadHistory() {
    try {
      const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) {
      return [];
    }
  }

  function saveRun(run) {
    const history = [run, ...loadHistory()].slice(0, 20);
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch (_) {}
    renderHistory(history);
  }

  function renderHistory(history) {
    if (!els.runList) return;
    if (!history.length) {
      els.runList.innerHTML = '<div class="fusion-empty">No runs yet.</div>';
      return;
    }
    els.runList.innerHTML = "";
    for (const run of history) {
      const button = document.createElement("button");
      button.className = "fusion-run-card";
      button.type = "button";
      button.innerHTML = `<strong>${escapeHtml(run.prompt)}</strong><span>${new Date(run.created_at).toLocaleString()}</span>`;
      button.addEventListener("click", () => {
        els.prompt.value = run.prompt;
        els.answer.textContent = run.output;
        els.title.textContent = "Loaded";
        els.meta.textContent = "Loaded from local history";
        renderCode();
      });
      els.runList.appendChild(button);
    }
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function resetForm() {
    els.prompt.value = "";
    els.answer.textContent = "Sign in, enter a prompt, then run Synth.";
    els.title.textContent = "Ready";
    els.meta.textContent = "";
    setError("");
    renderCode();
  }

  function init() {
    els.panel.value = DEFAULT_PANEL.join("\n");
    els.judges.value = DEFAULT_JUDGES.join(", ");
    els.finals.value = DEFAULT_FINALS.join(", ");
    renderHistory(loadHistory());
    renderCode();
    els.form.addEventListener("submit", runFusion);
    els.newRun.addEventListener("click", resetForm);
    els.copyCode.addEventListener("click", async () => {
      renderCode();
      try { await navigator.clipboard.writeText(els.code.textContent); } catch (_) {}
    });
    for (const input of [els.prompt, els.preset, els.maxTokens, els.panel, els.judges, els.finals]) {
      input.addEventListener("input", renderCode);
      input.addEventListener("change", renderCode);
    }
  }

  init();
})();
