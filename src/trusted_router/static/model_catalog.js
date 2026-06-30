"use strict";

(function () {
  function inferCapabilities(id) {
    const i = String(id || "").toLowerCase();
    const caps = [];
    const visionFamilies = [
      "claude-opus", "claude-sonnet", "claude-haiku",
      "gpt-5", "gpt-4o", "gpt-4-vision", "gpt-4-turbo",
      "gemini-3", "gemini-2", "gemini-1.5", "gemini-pro-vision",
      "llama-3.2-vision", "llama-3.3-vision",
      "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl",
      "pixtral", "molmo", "internvl", "minicpm-v",
      "minimax-m2", "minimax-m2.1", "minimax-m2.5", "minimax-m2.7",
      "step-1v", "yi-vision", "phi-3.5-vision",
      "vision",
    ];
    if (visionFamilies.some((family) => i.includes(family))) caps.push("vision");
    const toolsFamilies = [
      "claude-opus", "claude-sonnet", "claude-haiku",
      "gpt-5", "gpt-4o", "gpt-4-turbo", "gpt-4.1", "gpt-4.5",
      "gemini-3", "gemini-2", "gemini-1.5",
      "mistral-large", "mistral-small", "mistral-medium",
      "llama-3.1-70b-instruct", "llama-3.3-70b-instruct",
      "qwen2.5", "qwen3",
      "deepseek-v3", "deepseek-v4", "deepseek-r1",
      "kimi-k2", "glm-4", "glm-5", "yi-large",
      "command-r", "nova-pro", "nova-lite",
    ];
    if (toolsFamilies.some((family) => i.includes(family))) caps.push("tools");
    return caps;
  }

  function normalizeModel(raw) {
    const pricing = raw.pricing || {};
    const ext = raw.trustedrouter || {};
    const inputPerM = pricing.prompt != null ? Number(pricing.prompt) * 1_000_000 : null;
    const outputPerM = pricing.completion != null ? Number(pricing.completion) * 1_000_000 : null;
    const catalogCaps = ext.capabilities || [];
    const inferredCaps = inferCapabilities(raw.id || "");
    const allCaps = Array.from(new Set([...catalogCaps, ...inferredCaps]));
    return {
      id: raw.id,
      name: raw.name || raw.id,
      description: raw.description || "",
      context_length: raw.context_length || ext.context_length || null,
      input_per_m: inputPerM,
      output_per_m: outputPerM,
      uptime_pct: ext.uptime_pct || null,
      capabilities: allCaps,
      free: pricing && Number(pricing.prompt) === 0,
      open_weights: !!ext.open_weights,
      total_per_m: (inputPerM || 0) + (outputPerM || 0),
      internal_only: !!ext.internal_only,
      route_kind: ext.route_kind || "model",
      supports_chat: ext.supports_chat !== false,
    };
  }

  async function loadModels(catalogBase) {
    const base = catalogBase || "/v1";
    const resp = await fetch(base + "/models");
    if (!resp.ok) throw new Error("models fetch " + resp.status);
    const json = await resp.json();
    const data = Array.isArray(json.data) ? json.data : [];
    return data.map((model) => normalizeModel(model));
  }

  function providerFromModelId(id) {
    if (!id || typeof id !== "string") return "";
    const slash = id.indexOf("/");
    return slash > 0 ? id.slice(0, slash) : id;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function prettifyModelId(id) {
    const tail = String(id || "").split("/").pop() || "";
    return tail
      .replace(/[-_.]/g, " ")
      .split(" ")
      .map((word) => (word ? word[0].toUpperCase() + word.slice(1) : ""))
      .join(" ")
      .trim();
  }

  function providerAvatar(provider, sizeClass) {
    const p = String(provider || "tr").toLowerCase();
    const letters = p.slice(0, 2).toUpperCase();
    return `<span class="chat-avatar ${escapeHtml(sizeClass || "chat-avatar-row")} chat-avatar-${escapeHtml(p)}">${escapeHtml(letters)}</span>`;
  }

  function modelTagsHtml(model, activeIds) {
    const caps = model.capabilities || [];
    return [
      model.free ? '<span class="chat-tag chat-tag-free">Free</span>' : "",
      model.open_weights ? '<span class="chat-tag chat-tag-open">Open weights</span>' : "",
      caps.includes("vision") ? '<span class="chat-tag chat-tag-vision">Vision</span>' : "",
      caps.includes("tools") || caps.includes("tool_use")
        ? '<span class="chat-tag chat-tag-tools">Tools</span>'
        : "",
      activeIds && activeIds.has(model.id)
        ? '<span class="chat-tag chat-tag-active">In use</span>'
        : "",
    ].join("");
  }

  function makePickerRow(model, activeIds, onSelect) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "chat-model-row";
    if (activeIds && activeIds.has(model.id)) row.classList.add("is-active-model");
    const provider = providerFromModelId(model.id);
    row.innerHTML = `
      <div class="chat-model-row-main">
        ${providerAvatar(provider, "chat-avatar-row")}
        <div class="chat-model-row-text">
          <div class="chat-model-row-name">${escapeHtml(model.name || model.id)}</div>
          <div class="chat-model-row-provider">${escapeHtml(provider || model.id)}</div>
        </div>
      </div>
      <div class="chat-model-row-meta">
        ${model.input_per_m != null ? `<span>$${model.input_per_m.toFixed(2)}/M in</span>` : ""}
        ${model.output_per_m != null ? `<span>$${model.output_per_m.toFixed(2)}/M out</span>` : ""}
        ${model.context_length ? `<span>${(model.context_length / 1000).toFixed(0)}k ctx</span>` : ""}
        ${modelTagsHtml(model, activeIds)}
      </div>
    `;
    row.addEventListener("click", () => onSelect(model));
    return row;
  }

  function openModelPicker(options) {
    const models = (options && options.models) || [];
    const suggestedIds = (options && options.suggestedIds) || [];
    const allowModel = (options && options.allowModel) || (() => true);
    const onSelect = (options && options.onSelect) || (() => {});
    const activeIds = new Set((options && options.activeIds) || []);
    const state = { query: "", filters: { cheap: false, vision: false, tools: false, open: false } };
    const picker = document.createElement("div");
    picker.className = "chat-model-picker";
    picker.innerHTML = `
      <div class="chat-model-picker-backdrop" data-close></div>
      <div class="chat-model-picker-panel">
        <input type="text" class="chat-model-picker-search" placeholder="Search models..." autofocus>
        <div class="chat-model-picker-filters">
          <button type="button" class="chat-picker-filter" data-filter="cheap" title="Sort ascending by price">Cheap <span class="chat-picker-filter-count" data-count="cheap"></span></button>
          <button type="button" class="chat-picker-filter" data-filter="vision" title="Models with image-input support">Vision <span class="chat-picker-filter-count" data-count="vision"></span></button>
          <button type="button" class="chat-picker-filter" data-filter="tools" title="Models with tool-use support">Tools <span class="chat-picker-filter-count" data-count="tools"></span></button>
          <button type="button" class="chat-picker-filter" data-filter="open" title="Pure open-weight model or orchestration">Open weights <span class="chat-picker-filter-count" data-count="open"></span></button>
        </div>
        <div class="chat-model-picker-list"></div>
        <div class="chat-model-picker-footer">
          <span>Type to search by model or provider.</span><kbd>Esc</kbd>
        </div>
      </div>
    `;

    function close() {
      picker.remove();
      document.removeEventListener("keydown", onKeydown);
    }

    function countMatches(query) {
      const q = query.toLowerCase();
      return models.filter((model) => {
        if (model.internal_only || !allowModel(model)) return false;
        if (!q) return true;
        return (
          model.id.toLowerCase().includes(q) ||
          (model.name || "").toLowerCase().includes(q)
        );
      });
    }

    function filteredModels() {
      const q = state.query.toLowerCase();
      let filtered = models.filter((model) => {
        if (model.internal_only || !allowModel(model)) return false;
        if (
          q &&
          !model.id.toLowerCase().includes(q) &&
          !(model.name || "").toLowerCase().includes(q)
        ) {
          return false;
        }
        const caps = model.capabilities || [];
        if (state.filters.vision && !caps.includes("vision")) return false;
        if (
          state.filters.tools &&
          !caps.includes("tools") &&
          !caps.includes("tool_use")
        ) {
          return false;
        }
        if (state.filters.open && !model.open_weights) return false;
        return true;
      });
      if (state.filters.cheap) {
        filtered = filtered
          .slice()
          .sort((a, b) => (a.total_per_m || 0) - (b.total_per_m || 0))
          .slice(0, 30);
      } else {
        filtered = filtered.slice(0, 300);
      }
      return filtered;
    }

    function render() {
      const list = picker.querySelector(".chat-model-picker-list");
      if (!list) return;
      list.innerHTML = "";
      const queryMatched = countMatches(state.query);
      const counts = { cheap: queryMatched.length, vision: 0, tools: 0, open: 0 };
      for (const model of queryMatched) {
        const caps = model.capabilities || [];
        if (caps.includes("vision")) counts.vision += 1;
        if (caps.includes("tools") || caps.includes("tool_use")) counts.tools += 1;
        if (model.open_weights) counts.open += 1;
      }
      for (const key of ["cheap", "vision", "tools", "open"]) {
        const count = picker.querySelector(`[data-count="${key}"]`);
        const chip = picker.querySelector(`.chat-picker-filter[data-filter="${key}"]`);
        if (count) count.textContent = counts[key] > 0 ? `(${counts[key]})` : "";
        if (chip) {
          chip.hidden = counts[key] === 0;
          chip.classList.toggle("is-on", !!state.filters[key]);
        }
      }
      const filtered = filteredModels();
      const renderedIds = new Set();
      if (!state.query && suggestedIds.length > 0) {
        const suggested = [];
        for (const id of suggestedIds) {
          if (renderedIds.has(id)) continue;
          const real = models.find((model) => model.id === id);
          if (real && filtered.includes(real)) {
            suggested.push(real);
          } else if (
            !state.filters.cheap &&
            !state.filters.vision &&
            !state.filters.tools &&
            !state.filters.open
          ) {
            suggested.push({ id, name: prettifyModelId(id), capabilities: [], free: false });
          }
        }
        if (suggested.length) {
          const header = document.createElement("div");
          header.className = "chat-model-picker-group";
          header.textContent = "Suggested";
          list.appendChild(header);
          for (const model of suggested) {
            list.appendChild(makePickerRow(model, activeIds, (selected) => {
              onSelect(selected);
              close();
            }));
            renderedIds.add(model.id);
          }
        }
      }
      const grouped = new Map();
      for (const model of filtered) {
        if (renderedIds.has(model.id)) continue;
        const provider = providerFromModelId(model.id);
        if (!grouped.has(provider)) grouped.set(provider, []);
        grouped.get(provider).push(model);
      }
      for (const provider of Array.from(grouped.keys()).sort()) {
        const header = document.createElement("div");
        header.className = "chat-model-picker-group";
        header.textContent = provider;
        list.appendChild(header);
        for (const model of grouped.get(provider)) {
          list.appendChild(makePickerRow(model, activeIds, (selected) => {
            onSelect(selected);
            close();
          }));
        }
      }
      if (list.childElementCount === 0) {
        const empty = document.createElement("div");
        empty.className = "chat-model-picker-empty";
        empty.innerHTML = state.query
          ? `<div class="chat-model-picker-empty-title">No models match "${escapeHtml(state.query)}"</div><div class="chat-model-picker-empty-hint">Try a shorter query or clear the chip filters.</div>`
          : '<div class="chat-model-picker-empty-title">No models match the active filters</div><div class="chat-model-picker-empty-hint">Click a chip again to disable it.</div>';
        list.appendChild(empty);
      }
    }

    function onKeydown(event) {
      if (event.key === "Escape") close();
    }

    picker.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (target.closest("[data-close]")) close();
      const filter = target.closest("[data-filter]");
      if (filter instanceof HTMLElement) {
        const key = filter.dataset.filter;
        if (key && Object.prototype.hasOwnProperty.call(state.filters, key)) {
          state.filters[key] = !state.filters[key];
          render();
        }
      }
    });
    const input = picker.querySelector(".chat-model-picker-search");
    if (input instanceof HTMLInputElement) {
      input.addEventListener("input", () => {
        state.query = input.value;
        render();
      });
    }
    document.body.appendChild(picker);
    document.addEventListener("keydown", onKeydown);
    render();
    if (input instanceof HTMLInputElement) input.focus();
    return { close };
  }

  window.TrustedRouterModelCatalog = {
    escapeHtml,
    inferCapabilities,
    loadModels,
    openModelPicker,
    normalizeModel,
    providerFromModelId,
    prettifyModelId,
  };
})();
