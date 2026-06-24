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
      total_per_m: (inputPerM || 0) + (outputPerM || 0),
      internal_only: !!ext.internal_only,
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

  window.TrustedRouterModelCatalog = {
    inferCapabilities,
    loadModels,
    normalizeModel,
    providerFromModelId,
  };
})();
