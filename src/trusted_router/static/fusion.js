"use strict";

(function () {
  const CONFIG = window.__TR_FUSION__ || {};
  const API_BASE = CONFIG.apiBaseUrl || "/chat-proxy/v1";
  const CATALOG_BASE = CONFIG.catalogBaseUrl || "/v1";
  const ISSUE_KEY_PATH = CONFIG.issueKeyPath || "/internal/chat/issue-browser-key";
  const KEY_COOKIE = CONFIG.keyCookieName || "tr_chat_key";
  const KEY_STORAGE = "tr_chat_key";
  const HISTORY_KEY = "tr_fusion_runs_v1";
  const DETAIL_LAYOUT_KEY = "tr_fusion_detail_layout_v1";

  const DEFAULT_PANEL = configList("defaultPanel");
  const BUDGET_PANEL = configList("budgetPanel");
  const FRONTIER_PANEL = configList("frontierPanel");
  const DEFAULT_JUDGES = configList("defaultJudges");
  const DEFAULT_FINALS = configList("defaultFinals");
  const SUGGESTED_MODELS = uniqueModels([
    ...configList("suggestedModels"),
    ...DEFAULT_PANEL,
    ...BUDGET_PANEL,
    ...FRONTIER_PANEL,
    ...DEFAULT_JUDGES,
    ...DEFAULT_FINALS,
  ]);
  const MODEL_SET_KEYS = ["panel", "judges", "finals"];
  const modelSets = {
    panel: [],
    judges: DEFAULT_JUDGES.slice(),
    finals: DEFAULT_FINALS.slice(),
  };
  const PICKER_FILTERS = {
    cheap: false,
    vision: false,
    tools: false,
    open: false,
    us: false,
    eu: false,
  };
  let MODELS = [];
  let MODELS_LOADING = false;
  let pickerEl = null;
  let pickerTargetSet = "panel";
  let pickerQuery = "";
  let detailLayout = "stacked";

  const els = {
    form: document.querySelector("[data-fusion-form]"),
    prompt: document.querySelector("[data-fusion-prompt]"),
    synthesisPrompt: document.querySelector("[data-fusion-synthesis-prompt]"),
    preset: document.querySelector("[data-fusion-preset]"),
    maxTokens: document.querySelector("[data-fusion-max-tokens]"),
    answer: document.querySelector("[data-fusion-answer]"),
    details: document.querySelector("[data-fusion-details]"),
    error: document.querySelector("[data-fusion-error]"),
    meta: document.querySelector("[data-fusion-meta]"),
    code: document.querySelector("[data-fusion-code]"),
    title: document.querySelector("[data-result-title]"),
    runList: document.querySelector("[data-run-list]"),
    newRun: document.querySelector("[data-action='new-fusion']"),
    copyCode: document.querySelector("[data-action='copy-code']"),
    detailLayoutToggle: document.querySelector("[data-action='toggle-fusion-detail-layout']"),
    presetHelp: document.querySelector("[data-fusion-preset-help]"),
    modelCards: Object.fromEntries(MODEL_SET_KEYS.map((key) => [key, document.querySelector(`[data-fusion-model-cards="${key}"]`)])),
  };

  const PRESET_COPY = {
    quality: "Runs a broader open-model panel for harder prompts. Higher latency and cost.",
    budget: "Runs a smaller, faster panel for quick checks. Lower latency and cost.",
    frontier: "Runs frontier commercial models for highest capability. Highest latency and cost.",
  };

  function isSignedIn() {
    if (isLocalDemo()) return true;
    if (typeof window.hasSignedInHint === "function") return window.hasSignedInHint();
    return document.cookie
      .split(";")
      .map((c) => c.trim())
      .some((c) => c === "tr_signed_in=1");
  }

  function isLocalDemo() {
    const host = window.location.hostname;
    return (host === "127.0.0.1" || host === "localhost") &&
      new URLSearchParams(window.location.search).get("demo") === "1";
  }

  function configList(name) {
    const value = CONFIG[name];
    if (!Array.isArray(value)) return [];
    return value.map((item) => String(item).trim()).filter(Boolean).slice(0, 8);
  }

  function uniqueModels(items) {
    const seen = new Set();
    const out = [];
    for (const raw of items) {
      const id = String(raw || "").trim();
      if (!id || seen.has(id)) continue;
      seen.add(id);
      out.push(id);
    }
    return out;
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
    if (isLocalDemo()) return "sk-tr-local-demo";
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
    if (Array.isArray(value)) return uniqueModels(value).slice(0, 8);
    return String(value || "")
      .split(/[\n,]+/g)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 8);
  }

  function buildRequest() {
    const panel = splitModels(modelSets.panel);
    const judges = splitModels(modelSets.judges);
    const finals = splitModels(modelSets.finals);
    const maxTokens = Math.max(64, Math.min(8192, Number(els.maxTokens.value || 2048)));
    const parameters = {
      preset: els.preset.value || "budget",
      selection_strategy: "synthesize_non_refusals",
      analysis_models: panel.length ? panel : presetPanel(els.preset.value || "budget"),
      judge_models: judges.length ? judges : DEFAULT_JUDGES,
      final_models: finals.length ? finals : DEFAULT_FINALS,
      max_completion_tokens: maxTokens,
    };
    const synthesisPrompt = String(els.synthesisPrompt?.value || "").trim();
    if (synthesisPrompt) {
      parameters.synthesis_prompt = synthesisPrompt;
    }
    return {
      model: "trustedrouter/synth",
      stream: true,
      stream_options: { include_usage: true },
      messages: [{ role: "user", content: els.prompt.value.trim() }],
      max_tokens: maxTokens,
      tools: [{
        type: "trustedrouter:synth",
        parameters,
      }],
    };
  }

  function presetPanel(preset) {
    if (preset === "frontier" && FRONTIER_PANEL.length) return FRONTIER_PANEL;
    if (preset === "budget" && BUDGET_PANEL.length) return BUDGET_PANEL;
    return DEFAULT_PANEL;
  }

  async function loadModels() {
    if (MODELS_LOADING || MODELS.length > 0) return;
    MODELS_LOADING = true;
    try {
      if (window.TrustedRouterModelCatalog) {
        MODELS = await window.TrustedRouterModelCatalog.loadModels(CATALOG_BASE);
      }
    } catch (err) {
      console.warn("synth: model catalog load failed:", err);
    } finally {
      MODELS_LOADING = false;
      renderModelPicker();
    }
  }

  function findModel(id) {
    return MODELS.find((model) => model.id === id) || null;
  }

  function applyPreset(preset) {
    modelSets.panel = presetPanel(preset).slice();
    renderModelSet("panel");
    if (els.presetHelp) els.presetHelp.textContent = PRESET_COPY[preset] || "";
    renderCode();
  }

  function modelLabel(id) {
    const model = findModel(id);
    if (model?.name) return model.name;
    const parts = String(id || "").split("/");
    return prettifyModelId(parts[1] || parts[0] || "model");
  }

  function modelProvider(id) {
    if (window.TrustedRouterModelCatalog) return window.TrustedRouterModelCatalog.providerFromModelId(id);
    return String(id || "").split("/")[0] || "provider";
  }

  function renderAllModelSets() {
    MODEL_SET_KEYS.forEach(renderModelSet);
  }

  function renderModelSet(key) {
    const root = els.modelCards[key];
    if (!root) return;
    const models = modelSets[key] || [];
    root.innerHTML = "";
    models.forEach((id, idx) => {
      root.appendChild(makeModelPill(key, id, idx));
    });
    if (models.length < 8) {
      const add = document.createElement("button");
      add.type = "button";
      add.className = "chat-model-add";
      add.dataset.fusionOpenPicker = key;
      add.textContent = "+ Add model";
      root.appendChild(add);
    }
  }

  function makeModelPill(key, id, idx) {
    const model = findModel(id);
    const label = model?.name || modelLabel(id);
    const provider = modelProvider(id);
    const wrap = document.createElement("div");
    wrap.className = "chat-model-pill-wrap";
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "chat-model-pill";
    pill.dataset.fusionOpenPicker = key;
    pill.dataset.index = String(idx);
    pill.innerHTML = [
      providerAvatar(provider),
      `<span class="chat-model-pill-name">${escapeHtml(label)}</span>`,
      provider ? `<span class="chat-model-pill-provider" title="Provider: ${escapeHtml(provider)}">${escapeHtml(provider)}</span>` : "",
      '<span class="chat-model-pill-caret">▾</span>',
    ].join("");
    wrap.appendChild(pill);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "chat-model-pill-close";
    close.dataset.fusionModelRemove = key;
    close.dataset.index = String(idx);
    close.title = "Remove this model";
    close.setAttribute("aria-label", `Remove ${id}`);
    close.textContent = "×";
    wrap.appendChild(close);
    return wrap;
  }

  function providerAvatar(provider) {
    const label = String(provider || "?").slice(0, 2).toUpperCase();
    return `<span class="chat-avatar chat-avatar-pill" style="background:#1f2a44;color:#dbeafe">${escapeHtml(label)}</span>`;
  }

  function addModel(key, value) {
    const id = String(value || "").trim();
    if (!id) return false;
    const current = modelSets[key] || [];
    if (current.includes(id)) return false;
    if (current.length >= 8) {
      setError("Use at most eight models in each Synth list.");
      return false;
    }
    modelSets[key] = [...current, id];
    renderModelSet(key);
    renderCode();
    return true;
  }

  function removeModel(key, idx) {
    const current = modelSets[key] || [];
    modelSets[key] = current.filter((_, i) => i !== idx);
    renderModelSet(key);
    renderCode();
  }

  function resetModelSet(key) {
    if (key === "panel") {
      modelSets.panel = presetPanel(els.preset.value || "budget").slice();
    } else if (key === "judges") {
      modelSets.judges = DEFAULT_JUDGES.slice();
    } else if (key === "finals") {
      modelSets.finals = DEFAULT_FINALS.slice();
    }
    renderModelSet(key);
    renderCode();
  }

  function renderModelPicker() {
    if (!pickerEl) return;
    const list = pickerEl.querySelector(".chat-model-picker-list");
    if (!list) return;
    list.innerHTML = "";
    const q = pickerQuery.toLowerCase();
    let filtered = MODELS.filter((model) => {
      if (model.internal_only) return false;
      if (q && !model.id.toLowerCase().includes(q) && !(model.name || "").toLowerCase().includes(q)) {
        return false;
      }
      const caps = model.capabilities || [];
      if (PICKER_FILTERS.vision && !caps.includes("vision")) return false;
      if (PICKER_FILTERS.tools && !caps.includes("tools") && !caps.includes("tool_use")) return false;
      if (PICKER_FILTERS.open && !model.open_weights) return false;
      if (PICKER_FILTERS.us && !model.us_provider_available) return false;
      if (PICKER_FILTERS.eu && !model.eu_focused_provider_available) return false;
      return true;
    });
    if (PICKER_FILTERS.cheap) {
      filtered = filtered
        .slice()
        .sort((a, b) => (a.total_per_m || 0) - (b.total_per_m || 0))
        .slice(0, 30);
    } else {
      filtered = filtered.slice(0, 300);
    }

    const queryMatched = MODELS.filter((model) => {
      if (model.internal_only) return false;
      if (!q) return true;
      return model.id.toLowerCase().includes(q) || (model.name || "").toLowerCase().includes(q);
    });
    const counts = { cheap: queryMatched.length, vision: 0, tools: 0, open: 0, us: 0, eu: 0 };
    queryMatched.forEach((model) => {
      const caps = model.capabilities || [];
      if (caps.includes("vision")) counts.vision++;
      if (caps.includes("tools") || caps.includes("tool_use")) counts.tools++;
      if (model.open_weights) counts.open++;
      if (model.us_provider_available) counts.us++;
      if (model.eu_focused_provider_available) counts.eu++;
    });
    for (const key of ["cheap", "vision", "tools", "open", "us", "eu"]) {
      const count = pickerEl.querySelector(`[data-count="${key}"]`);
      const chip = pickerEl.querySelector(`.chat-picker-filter[data-filter="${key}"]`);
      if (count) count.textContent = counts[key] > 0 ? `(${counts[key]})` : "";
      if (chip) chip.hidden = counts[key] === 0;
    }

    const activeIds = new Set(modelSets[pickerTargetSet] || []);
    const renderedIds = new Set();
    if (!q) {
      const popularRows = [];
      for (const id of SUGGESTED_MODELS) {
        if (renderedIds.has(id)) continue;
        const real = findModel(id);
        if (real && filtered.includes(real)) {
          popularRows.push(real);
        } else if (
          !PICKER_FILTERS.cheap &&
          !PICKER_FILTERS.vision &&
          !PICKER_FILTERS.tools &&
          !PICKER_FILTERS.open &&
          !PICKER_FILTERS.us &&
          !PICKER_FILTERS.eu
        ) {
          popularRows.push({ id, name: prettifyModelId(id), capabilities: [], free: false, _stub: true });
        }
      }
      if (popularRows.length) {
        const header = document.createElement("div");
        header.className = "chat-model-picker-group";
        header.textContent = "Popular";
        list.appendChild(header);
        popularRows.forEach((model) => {
          list.appendChild(makePickerRow(model, activeIds));
          renderedIds.add(model.id);
        });
      }
    }

    const grouped = new Map();
    for (const model of filtered) {
      if (renderedIds.has(model.id)) continue;
      const provider = modelProvider(model.id);
      if (!grouped.has(provider)) grouped.set(provider, []);
      grouped.get(provider).push(model);
    }
    Array.from(grouped.keys()).sort().forEach((provider) => {
      const header = document.createElement("div");
      header.className = "chat-model-picker-group";
      header.textContent = provider;
      list.appendChild(header);
      grouped.get(provider).forEach((model) => {
        list.appendChild(makePickerRow(model, activeIds));
      });
    });

    if (list.childElementCount === 0) {
      const empty = document.createElement("div");
      empty.className = "chat-model-picker-empty";
      empty.innerHTML = q
        ? `<div class="chat-model-picker-empty-title">No models match "${escapeHtml(q)}"</div><div class="chat-model-picker-empty-hint">Try a shorter query or clear the chip filters.</div>`
        : '<div class="chat-model-picker-empty-title">No models match the active filters</div><div class="chat-model-picker-empty-hint">Click a chip again to disable it.</div>';
      list.appendChild(empty);
    }
  }

  function makePickerRow(model, activeIds) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "chat-model-row";
    if (activeIds.has(model.id)) row.classList.add("is-active-model");
    const provider = modelProvider(model.id);
    row.innerHTML = `
      <div class="chat-model-row-main">
        ${providerAvatar(provider)}
        <div class="chat-model-row-text">
          <div class="chat-model-row-name">${escapeHtml(model.name || model.id)}</div>
          <div class="chat-model-row-provider">${escapeHtml(provider || model.id)}</div>
        </div>
      </div>
      <div class="chat-model-row-meta">
        ${model.input_per_m != null ? `<span>$${model.input_per_m.toFixed(2)}/M in</span>` : ""}
        ${model.output_per_m != null ? `<span>$${model.output_per_m.toFixed(2)}/M out</span>` : ""}
        ${model.context_length ? `<span>${(model.context_length / 1000).toFixed(0)}k ctx</span>` : ""}
        ${model.free ? '<span class="chat-tag chat-tag-free">Free</span>' : ""}
        ${model.open_weights ? '<span class="chat-tag chat-tag-open">Open weights</span>' : ""}
        ${model.us_provider_available ? '<span class="chat-tag chat-tag-region">US</span>' : ""}
        ${model.eu_focused_provider_available ? '<span class="chat-tag chat-tag-region">EU</span>' : ""}
        ${(model.capabilities || []).includes("vision") ? '<span class="chat-tag chat-tag-vision">Vision</span>' : ""}
        ${(model.capabilities || []).includes("tools") || (model.capabilities || []).includes("tool_use") ? '<span class="chat-tag chat-tag-tools">Tools</span>' : ""}
        ${activeIds.has(model.id) ? '<span class="chat-tag chat-tag-active">In use</span>' : ""}
      </div>
    `;
    row.addEventListener("click", () => {
      if (addModel(pickerTargetSet, model.id)) closeModelPicker();
    });
    return row;
  }

  function prettifyModelId(id) {
    const tail = String(id || "").split("/").pop() || "";
    return tail.replace(/[-_.]/g, " ").split(" ").map((word) => word ? word[0].toUpperCase() + word.slice(1) : "").join(" ").trim();
  }

  function openModelPicker(key) {
    pickerTargetSet = key || "panel";
    pickerQuery = "";
    if (pickerEl) return;
    pickerEl = document.createElement("div");
    pickerEl.className = "chat-model-picker";
    pickerEl.innerHTML = `
      <div class="chat-model-picker-backdrop" data-close></div>
      <div class="chat-model-picker-panel">
        <input type="text" class="chat-model-picker-search" placeholder="Search models..." autofocus>
        <div class="chat-model-picker-filters">
          <button type="button" class="chat-picker-filter" data-filter="cheap" title="Sort ascending by price">Cheap <span class="chat-picker-filter-count" data-count="cheap"></span></button>
          <button type="button" class="chat-picker-filter" data-filter="vision" title="Models with image-input support">Vision <span class="chat-picker-filter-count" data-count="vision"></span></button>
          <button type="button" class="chat-picker-filter" data-filter="tools" title="Models with tool/function-call support">Tools <span class="chat-picker-filter-count" data-count="tools"></span></button>
          <button type="button" class="chat-picker-filter" data-filter="open" title="Pure open-weight model or orchestration">Open weights <span class="chat-picker-filter-count" data-count="open"></span></button>
          <button type="button" class="chat-picker-filter" data-filter="us" title="Models with at least one US-based provider route">US <span class="chat-picker-filter-count" data-count="us"></span></button>
          <button type="button" class="chat-picker-filter" data-filter="eu" title="Models with at least one EU-focused provider route">EU <span class="chat-picker-filter-count" data-count="eu"></span></button>
        </div>
        <div class="chat-model-picker-list"></div>
        <div class="chat-model-picker-footer">
          <span><kbd>↑↓</kbd> navigate</span>
          <span><kbd>↵</kbd> select</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    `;
    document.body.appendChild(pickerEl);
    pickerEl.querySelector("[data-close]").addEventListener("click", closeModelPicker);
    const input = pickerEl.querySelector(".chat-model-picker-search");
    input.addEventListener("input", () => {
      pickerQuery = input.value;
      renderModelPicker();
    });
    pickerEl.querySelectorAll(".chat-picker-filter").forEach((button) => {
      const key = button.dataset.filter;
      if (PICKER_FILTERS[key]) button.classList.add("is-on");
      button.addEventListener("click", () => {
        PICKER_FILTERS[key] = !PICKER_FILTERS[key];
        button.classList.toggle("is-on", PICKER_FILTERS[key]);
        renderModelPicker();
      });
    });
    renderModelPicker();
    loadModels();
    document.addEventListener("keydown", pickerKeyHandler);
    window.setTimeout(() => input.focus(), 0);
  }

  function closeModelPicker() {
    if (!pickerEl) return;
    pickerEl.remove();
    pickerEl = null;
    document.removeEventListener("keydown", pickerKeyHandler);
  }

  function pickerKeyHandler(event) {
    if (!pickerEl) return;
    if (event.key === "Escape") {
      closeModelPicker();
      return;
    }
    const rows = pickerEl.querySelectorAll(".chat-model-row");
    if (!rows.length) return;
    let activeIdx = Array.from(rows).findIndex((row) => row.classList.contains("is-keyboard-active"));
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlightPickerRow(rows, activeIdx < 0 ? 0 : Math.min(activeIdx + 1, rows.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlightPickerRow(rows, activeIdx <= 0 ? 0 : activeIdx - 1);
    } else if (event.key === "Enter" && activeIdx >= 0) {
      event.preventDefault();
      rows[activeIdx].click();
    }
  }

  function highlightPickerRow(rows, idx) {
    rows.forEach((row, i) => row.classList.toggle("is-keyboard-active", i === idx));
    rows[idx]?.scrollIntoView({ block: "nearest" });
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
    const direct = json?.choices?.[0]?.message?.content || "";
    if (String(direct).trim()) return direct;
    return finalVisibleAnswer(synthDetails(json));
  }

  function synthDetails(json) {
    return json?.trustedrouter?.synth || null;
  }

  function finalVisibleAnswer(details) {
    const attempts = Array.isArray(details?.final_attempts) ? details.final_attempts : [];
    for (let idx = attempts.length - 1; idx >= 0; idx -= 1) {
      const visible = attempts[idx]?.visible_answer || attempts[idx]?.raw_output || "";
      if (String(visible).trim()) return visible;
    }
    const fallback = details?.final?.visible_answer || details?.final?.raw_output || "";
    return String(fallback).trim() ? fallback : "";
  }

  function formatMeta(json, startedAt) {
    const ms = Math.max(0, Math.round(performance.now() - startedAt));
    const usage = json?.usage || {};
    const route = json?.trustedrouter?.provider || json?.provider || "synth";
    const total = usage.total_tokens || 0;
    return `${ms} ms · ${total ? `${total} tokens · ` : ""}${route}`;
  }

  function loadDetailLayout() {
    try {
      const saved = localStorage.getItem(DETAIL_LAYOUT_KEY);
      if (saved === "side-by-side" || saved === "stacked") return saved;
    } catch (_) {}
    return "stacked";
  }

  function saveDetailLayout() {
    try { localStorage.setItem(DETAIL_LAYOUT_KEY, detailLayout); } catch (_) {}
  }

  function applyDetailLayout() {
    if (!els.details) return;
    const sideBySide = detailLayout === "side-by-side";
    els.details.classList.toggle("is-side-by-side", sideBySide);
    if (els.detailLayoutToggle) {
      els.detailLayoutToggle.setAttribute("aria-pressed", sideBySide ? "true" : "false");
      els.detailLayoutToggle.textContent = sideBySide ? "Stacked" : "Side-by-side";
    }
  }

  function toggleDetailLayout() {
    detailLayout = detailLayout === "side-by-side" ? "stacked" : "side-by-side";
    saveDetailLayout();
    applyDetailLayout();
  }

  async function postFusion(key, request) {
    if (isLocalDemo()) return demoFusionResponse(request);
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

  async function postFusionStream(key, request, callbacks) {
    if (isLocalDemo()) return demoFusionStream(request, callbacks);
    const resp = await fetch(`${API_BASE}/chat/completions`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${key}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ...request, stream: true }),
    });
    if (resp.status === 401) {
      const fresh = await ensureBrowserKey(true);
      return postFusionStream(fresh, request, callbacks);
    }
    if (!resp.ok) {
      const text = await resp.text();
      let json = null;
      try { json = text ? JSON.parse(text) : null; } catch (_) {}
      const msg = json?.error?.message || text || `Synth failed with ${resp.status}`;
      throw new Error(msg);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let output = "";
    let usage = null;
    const details = emptyStreamDetails(request);
    callbacks?.details?.(details);
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const lines = chunk.split("\n").map((line) => line.trim()).filter(Boolean);
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === "[DONE]") continue;
          let ev = null;
          try { ev = JSON.parse(payload); } catch (_) { continue; }
          const delta = ev.choices?.[0]?.delta || null;
          if (delta && typeof delta.content === "string") {
            output += delta.content;
            callbacks?.output?.(output);
          }
          const thinkingText = (delta && typeof delta.reasoning_content === "string" ? delta.reasoning_content : "") ||
            (delta && typeof delta.reasoning === "string" ? delta.reasoning : "") ||
            (delta && typeof delta.thinking === "string" ? delta.thinking : "");
          if (thinkingText) {
            appendStreamDetailDelta(details, {
              stage: "final",
              index: 0,
              model: ev.model || details.selected_model,
              delta_type: "thinking_delta",
              text: thinkingText,
            });
            callbacks?.details?.(details);
          }
          if (ev.usage) usage = ev.usage;
          const synthEvent = ev.trustedrouter?.synth || null;
          if (synthEvent) {
            applySynthStreamEvent(details, synthEvent);
            callbacks?.details?.(details);
          }
        }
      }
    }
    return {
      id: "chatcmpl_synth_stream",
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: "trustedrouter/synth",
      choices: [{ index: 0, message: { role: "assistant", content: output }, finish_reason: "stop" }],
      usage: usage || {},
      trustedrouter: { synth: details },
    };
  }

  async function demoFusionResponse(request) {
    await new Promise((resolve) => window.setTimeout(resolve, 350));
    const panel = request.tools?.[0]?.parameters?.analysis_models || [];
    return {
      id: "chatcmpl_demo_synth",
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: "trustedrouter/synth",
      choices: [{
        index: 0,
        message: {
          role: "assistant",
          content: "Demo Synth answer. The selected panel ran, the judge picked useful non-refusals, and the final model produced this answer.",
        },
        finish_reason: "stop",
      }],
      usage: { prompt_tokens: 42, completion_tokens: 64, total_tokens: 106 },
      trustedrouter: {
        synth: {
          preset: els.preset.value || "budget",
          selected_model: (modelSets.finals || [])[0] || "z-ai/glm-5.2",
          panel: panel.map((model, idx) => ({
            model,
            finish_reason: "stop",
            visible_answer: `Demo visible answer from ${model}.`,
            raw_output: `<think>Demo raw thinking from ${model}: compare the prompt, note tradeoffs, answer directly.</think>\nDemo visible answer from ${model}.`,
            thinking: [{ text: `Demo raw thinking block from ${model}.`, signature: `demo_sig_${idx + 1}` }],
            input_tokens: 12,
            output_tokens: 18,
          })),
          judge: {
            model: (modelSets.judges || [])[0] || "moonshotai/kimi-k2.6",
            visible_answer: "{\"final_guidance\":\"Use the most complete non-refusal answer.\"}",
            input_tokens: 30,
            output_tokens: 16,
          },
          final_attempts: [{
            model: (modelSets.finals || [])[0] || "z-ai/glm-5.2",
            visible_answer: "Demo Synth answer.",
            input_tokens: 24,
            output_tokens: 32,
          }],
          note: "Local demo mode. No request was sent to providers.",
        },
      },
    };
  }

  async function demoFusionStream(request, callbacks) {
    const details = emptyStreamDetails(request);
    const panel = request.tools?.[0]?.parameters?.analysis_models || [];
    callbacks?.details?.(details);
    for (let idx = 0; idx < panel.length; idx += 1) {
      const model = panel[idx];
      applySynthStreamEvent(details, { event: "panel.started", stage: "panel", index: idx, model });
      callbacks?.details?.(details);
      await sleep(70);
      applySynthStreamEvent(details, { event: "panel.thinking_delta", stage: "panel", index: idx, model, delta_type: "thinking_delta", text: `Demo raw thinking from ${model}: compare options. ` });
      callbacks?.details?.(details);
      await sleep(60);
      applySynthStreamEvent(details, { event: "panel.text_delta", stage: "panel", index: idx, model, delta_type: "text_delta", text: `Demo visible answer from ${model}.` });
      callbacks?.details?.(details);
      applySynthStreamEvent(details, {
        event: "panel.done",
        stage: "panel",
        index: idx,
        model,
        detail: {
          model,
          finish_reason: "stop",
          visible_answer: `Demo visible answer from ${model}.`,
          raw_output: `Demo visible answer from ${model}.`,
          thinking: [{ text: `Demo raw thinking from ${model}: compare options.`, signature: `demo_sig_${idx + 1}` }],
          input_tokens: 12,
          output_tokens: 18,
        },
      });
      callbacks?.details?.(details);
    }
    const judgeModel = (modelSets.judges || [])[0] || "moonshotai/kimi-k2.6";
    applySynthStreamEvent(details, { event: "judge.started", stage: "judge", index: 0, model: judgeModel });
    applySynthStreamEvent(details, { event: "judge.thinking_delta", stage: "judge", index: 0, model: judgeModel, delta_type: "thinking_delta", text: "Judge compares non-refusals and picks the most useful evidence. " });
    callbacks?.details?.(details);
    await sleep(80);
    const finalModel = (modelSets.finals || [])[0] || "z-ai/glm-5.2";
    const forceEmpty = new URLSearchParams(window.location.search).get("demo_empty") === "1";
    let output = "";
    if (!forceEmpty) {
      for (const part of ["Demo Synth answer. ", "The panel streamed raw thinking, ", "then the synthesizer returned this answer."]) {
        await sleep(90);
        output += part;
        callbacks?.output?.(output);
      }
    }
    applySynthStreamEvent(details, {
      event: "final.done",
      stage: "final",
      index: 0,
      model: finalModel,
      detail: {
        model: finalModel,
        visible_answer: output,
        thinking: [{ text: "Final synthesizer demo thinking.", signature: "demo_final_sig" }],
        input_tokens: 24,
        output_tokens: 32,
      },
    });
    callbacks?.details?.(details);
    return {
      id: "chatcmpl_demo_synth_stream",
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: "trustedrouter/synth",
      choices: [{ index: 0, message: { role: "assistant", content: output }, finish_reason: "stop" }],
      usage: { prompt_tokens: 42, completion_tokens: 64, total_tokens: 106 },
      trustedrouter: { synth: details },
    };
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
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
    renderDetails(null);
    const startedAt = performance.now();
    let latestDetails = null;
    let latestOutput = "";
    try {
      const key = await ensureBrowserKey(false);
      const json = await postFusionStream(key, request, {
        output: (text) => {
          latestOutput = text || latestOutput;
          els.answer.textContent = text || "Streaming final synthesis...";
          els.answer.classList.remove("loading");
        },
        details: (details) => {
          latestDetails = details || latestDetails;
          renderDetails(details);
        },
      });
      const details = synthDetails(json) || latestDetails;
      const output = completionText(json) || latestOutput;
      els.answer.textContent = output;
      renderDetails(details);
      els.answer.classList.remove("loading");
      els.meta.textContent = formatMeta(json, startedAt);
      if (!String(output).trim()) {
        els.title.textContent = "Needs review";
        els.answer.textContent = "No visible final answer returned. Raw panel, judge, and synthesizer traces are preserved below.";
        setError("Synth returned an empty final answer. The stream traces were kept so you can inspect which stage failed.");
        return;
      }
      els.title.textContent = "Completed";
      saveRun({
        prompt: request.messages[0].content,
        synthesis_prompt: request.tools?.[0]?.parameters?.synthesis_prompt || "",
        output,
        created_at: new Date().toISOString(),
      });
    } catch (err) {
      els.answer.classList.remove("loading");
      els.title.textContent = "Error";
      if (latestOutput) {
        els.answer.textContent = latestOutput;
      } else {
        els.answer.textContent = "Synth did not complete. Partial traces are preserved below if the gateway returned any.";
      }
      if (latestDetails) {
        latestDetails.note = [latestDetails.note, `Synth error: ${err?.message || "unknown error"}`].filter(Boolean).join(" ");
        renderDetails(latestDetails);
      }
      setError(`Synth did not complete: ${err?.message || "Synth failed."}`);
    }
  }

  function emptyStreamDetails(request) {
    const params = request.tools?.[0]?.parameters || {};
    return {
      preset: params.preset || "budget",
      selection_strategy: params.selection_strategy || "synthesize_non_refusals",
      selected_model: (params.final_models || [])[0] || "",
      panel: [],
      judge_attempts: [],
      final_attempts: [],
      note: "Streaming raw thinking and output as providers return it.",
    };
  }

  function applySynthStreamEvent(details, event) {
    if (!details || !event) return;
    if (event.preset) details.preset = event.preset;
    if (event.selection_strategy) details.selection_strategy = event.selection_strategy;
    if (event.detail) {
      const item = streamDetailItem(details, event.stage, Number(event.index || 0), event.model);
      Object.assign(item, event.detail);
      if (event.stage === "judge") details.judge = item;
      if (event.stage === "final") details.selected_model = item.model || details.selected_model;
      return;
    }
    if (event.error) {
      const item = streamDetailItem(details, event.stage || "final", Number(event.index || 0), event.model);
      item.finish_reason = "error";
      item.error = event.error;
      return;
    }
    appendStreamDetailDelta(details, event);
  }

  function appendStreamDetailDelta(details, event) {
    const stage = event.stage || "final";
    const item = streamDetailItem(details, stage, Number(event.index || 0), event.model);
    const type = event.delta_type || (event.event || "").split(".").pop();
    if (type === "thinking_delta" || type === "thinking") {
      item.thinking = Array.isArray(item.thinking) ? item.thinking : [{ text: "" }];
      if (!item.thinking[0]) item.thinking[0] = { text: "" };
      item.thinking[0].text = (item.thinking[0].text || "") + (event.text || "");
    } else if (type === "signature_delta") {
      item.thinking = Array.isArray(item.thinking) ? item.thinking : [{ text: "" }];
      if (!item.thinking[0]) item.thinking[0] = { text: "" };
      item.thinking[0].signature = (item.thinking[0].signature || "") + (event.signature || "");
    } else if (type === "text_delta" || type === "text") {
      item.raw_output = (item.raw_output || "") + (event.text || "");
      item.visible_answer = (item.visible_answer || "") + (event.text || "");
    }
  }

  function streamDetailItem(details, stage, index, model) {
    const key = stage === "judge" ? "judge_attempts" : stage === "final" ? "final_attempts" : "panel";
    details[key] = Array.isArray(details[key]) ? details[key] : [];
    if (!details[key][index]) {
      details[key][index] = { model: model || "unknown model", visible_answer: "", raw_output: "", thinking: [] };
    }
    if (model) details[key][index].model = model;
    if (stage === "judge") details.judge = details[key][index];
    return details[key][index];
  }

  function renderDetails(details) {
    if (!els.details) return;
    if (!details) {
      els.details.hidden = true;
      els.details.innerHTML = "";
      return;
    }
    const viewState = captureDetailViewState();
    const panel = Array.isArray(details.panel) ? details.panel : [];
    const judge = details.judge || null;
    const finalAttempts = Array.isArray(details.final_attempts) ? details.final_attempts : [];
    const pieces = [];
    pieces.push('<details open data-detail-section="panel"><summary>Panel raw thinking and output</summary><div class="fusion-detail-list">');
    if (!panel.length) {
      pieces.push('<p class="fusion-muted">No panel details returned.</p>');
    }
    panel.forEach((item, idx) => {
      pieces.push(renderDetailCard(item, `Panel ${idx + 1}`, `panel-${idx}`));
    });
    pieces.push("</div></details>");
    if (judge) {
      pieces.push('<details open data-detail-section="judge"><summary>Judge raw thinking and output</summary><div class="fusion-detail-list">');
      pieces.push(renderDetailCard(judge, "Judge", "judge-0"));
      pieces.push("</div>");
      pieces.push("</details>");
    }
    if (finalAttempts.length) {
      pieces.push('<details open data-detail-section="final"><summary>Final synthesizer raw thinking and output</summary><div class="fusion-detail-list">');
      finalAttempts.forEach((item, idx) => {
        pieces.push(renderDetailCard(item, `Final ${idx + 1}`, `final-${idx}`));
      });
      pieces.push("</div></details>");
    }
    if (details.note) {
      pieces.push(`<p class="fusion-detail-note">${escapeHtml(details.note)}</p>`);
    }
    els.details.innerHTML = pieces.join("");
    els.details.hidden = false;
    applyDetailLayout();
    restoreDetailViewState(viewState);
  }

  function renderDetailCard(item, label, cardKey) {
    const model = item?.model || "unknown model";
    const finish = item?.finish_reason ? ` · ${escapeHtml(item.finish_reason)}` : "";
    const usage = item?.input_tokens || item?.output_tokens
      ? ` · ${Number(item.input_tokens || 0)} in / ${Number(item.output_tokens || 0)} out`
      : "";
    const visible = item?.visible_answer || "";
    const raw = item?.raw_output || "";
    const thinking = Array.isArray(item?.thinking) ? item.thinking : [];
    const sections = [
      renderDetailSection(cardKey, "visible", "Visible answer", visible || "No visible answer returned."),
    ];
    if (item?.error) {
      sections.push(renderDetailSection(cardKey, "error", "Error", item.error));
    }
    thinking.forEach((block, idx) => {
      const suffix = block?.signature ? ` · signature ${block.signature}` : "";
      sections.push(renderDetailSection(cardKey, `thinking-${idx}`, `Raw thinking ${idx + 1}${suffix}`, block?.text || ""));
    });
    if (raw) {
      sections.push(renderDetailSection(cardKey, "raw", "Raw provider output", raw));
    }
    return [
      `<article class="fusion-detail-card" data-detail-card="${escapeHtml(cardKey)}">`,
      `<div class="fusion-detail-card-head"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(model)}${finish}${usage}</span></div>`,
      '<div class="fusion-detail-card-body">',
      sections.join(""),
      "</div>",
      "</article>",
    ].join("");
  }

  function renderDetailSection(cardKey, sectionKey, label, value) {
    const scrollKey = `${cardKey}:${sectionKey}`;
    return [
      '<div class="fusion-detail-section">',
      `<span>${escapeHtml(label)}</span>`,
      `<pre data-scroll-key="${escapeHtml(scrollKey)}">${escapeHtml(value)}</pre>`,
      "</div>",
    ].join("");
  }

  function captureDetailViewState() {
    if (!els.details || els.details.hidden) return null;
    const state = {
      windowY: window.scrollY,
      detailsScrollTop: els.details.scrollTop,
      openSections: {},
      scrollTops: {},
    };
    els.details.querySelectorAll("[data-detail-section]").forEach((node) => {
      state.openSections[node.dataset.detailSection] = node.open;
    });
    els.details.querySelectorAll("[data-scroll-key]").forEach((node) => {
      state.scrollTops[node.dataset.scrollKey] = node.scrollTop;
    });
    return state;
  }

  function restoreDetailViewState(state) {
    if (!state || !els.details) return;
    Object.entries(state.openSections || {}).forEach(([key, open]) => {
      const node = els.details.querySelector(`[data-detail-section="${cssEscape(key)}"]`);
      if (node) node.open = open;
    });
    els.details.scrollTop = state.detailsScrollTop || 0;
    Object.entries(state.scrollTops || {}).forEach(([key, top]) => {
      const node = els.details.querySelector(`[data-scroll-key="${cssEscape(key)}"]`);
      if (node) node.scrollTop = top;
    });
    window.requestAnimationFrame(() => {
      els.details.scrollTop = state.detailsScrollTop || 0;
      Object.entries(state.scrollTops || {}).forEach(([key, top]) => {
        const node = els.details.querySelector(`[data-scroll-key="${cssEscape(key)}"]`);
        if (node) node.scrollTop = top;
      });
      if (Math.abs(window.scrollY - state.windowY) > 4) window.scrollTo(0, state.windowY);
    });
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(String(value));
    return String(value).replace(/["\\]/g, "\\$&");
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
        if (els.synthesisPrompt) els.synthesisPrompt.value = run.synthesis_prompt || "";
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
    if (els.synthesisPrompt) els.synthesisPrompt.value = "";
    els.answer.textContent = "Sign in, enter a prompt, then run Synth.";
    els.title.textContent = "Ready";
    els.meta.textContent = "";
    applyPreset(els.preset.value || "budget");
    modelSets.judges = DEFAULT_JUDGES.slice();
    modelSets.finals = DEFAULT_FINALS.slice();
    renderAllModelSets();
    renderDetails(null);
    setError("");
    renderCode();
  }

  function init() {
    detailLayout = loadDetailLayout();
    applyDetailLayout();
    applyPreset(els.preset.value || "budget");
    renderAllModelSets();
    renderHistory(loadHistory());
    renderCode();
    els.form.addEventListener("submit", runFusion);
    els.newRun.addEventListener("click", resetForm);
    els.copyCode.addEventListener("click", async () => {
      renderCode();
      try { await navigator.clipboard.writeText(els.code.textContent); } catch (_) {}
    });
    els.detailLayoutToggle?.addEventListener("click", toggleDetailLayout);
    els.preset.addEventListener("change", () => {
      applyPreset(els.preset.value || "budget");
      renderCode();
    });
    for (const input of [els.prompt, els.synthesisPrompt, els.maxTokens]) {
      if (!input) continue;
      input.addEventListener("input", renderCode);
      input.addEventListener("change", renderCode);
    }
    loadModels();
    document.addEventListener("click", (event) => {
      const remove = event.target.closest("[data-fusion-model-remove]");
      if (remove) {
        removeModel(remove.dataset.fusionModelRemove, Number(remove.dataset.index || 0));
        return;
      }
      const opener = event.target.closest("[data-fusion-open-picker]");
      if (opener) {
        openModelPicker(opener.dataset.fusionOpenPicker);
        return;
      }
      const reset = event.target.closest("[data-fusion-model-reset]");
      if (reset) {
        resetModelSet(reset.dataset.fusionModelReset);
      }
    });
  }

  init();
})();
