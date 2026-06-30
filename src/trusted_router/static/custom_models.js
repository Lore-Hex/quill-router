"use strict";

(function () {
  const catalog = window.TrustedRouterModelCatalog;
  if (!catalog) return;

  const suggestedIds = [
    "trustedrouter/zdr",
    "trustedrouter/e2e",
    "trustedrouter/auto",
    "trustedrouter/cheap",
    "anthropic/claude-sonnet-4.6",
    "moonshotai/kimi-k2.7-code",
    "z-ai/glm-5.2",
    "deepseek/deepseek-v4-flash",
    "google/gemini-2.5-flash",
  ];
  let models = [];
  let loading = null;

  function load() {
    if (!loading) {
      loading = catalog.loadModels("/v1").then((rows) => {
        models = rows;
        hydrateAll();
        return rows;
      }).catch(() => {
        return [];
      });
    }
    return loading;
  }

  function baseAllowed(model) {
    if (!model || model.internal_only) return false;
    if (!model.id || model.id.startsWith("trustedrouter/user-")) return false;
    return model.supports_chat !== false;
  }

  function findModel(id) {
    return models.find((model) => model.id === id) || null;
  }

  function displayName(id) {
    const model = findModel(id);
    return model ? model.name : catalog.prettifyModelId(id);
  }

  function hydratePicker(root) {
    if (root.dataset.baseModelHydrated === "1") return;
    root.dataset.baseModelHydrated = "1";
    const input = root.querySelector("[data-base-model-input]");
    const button = root.querySelector("[data-base-model-button]");
    const nameEl = root.querySelector("[data-base-model-name]");
    const idEl = root.querySelector("[data-base-model-id]");
    if (!(input instanceof HTMLInputElement) || !(button instanceof HTMLButtonElement)) return;
    const value = input.value || root.dataset.currentModel || "";
    if (nameEl) nameEl.textContent = displayName(value);
    if (idEl) idEl.textContent = value;
    button.addEventListener("click", async () => {
      button.disabled = true;
      await load();
      button.disabled = false;
      catalog.openModelPicker({
        models,
        activeIds: input.value ? [input.value] : [],
        suggestedIds,
        allowModel: baseAllowed,
        onSelect: (model) => {
          input.value = model.id;
          if (nameEl) nameEl.textContent = model.name || model.id;
          if (idEl) idEl.textContent = model.id;
        },
      });
    });
  }

  function hydrateAll() {
    document.querySelectorAll("[data-base-model-picker]").forEach(hydratePicker);
  }

  hydrateAll();
  load();
})();
