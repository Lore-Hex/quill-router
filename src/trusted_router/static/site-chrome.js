// Shared site chrome: header menu, model search dialog and footer disclosures
// for every page outside the homepage. The homepage keeps the same behaviour in
// static/homepage/homepage.js. Sign-in and the signed-in "Console" swap stay in
// dashboard.js.
(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  // Footer link groups collapse on phones and stay open on wider screens.
  const compactContent = matchMedia("(max-width:600px)");
  const mobileDisclosures = $$("[data-mobile-disclosure]");
  const mobileOpen = new Map();
  function syncDisclosures() {
    mobileDisclosures.forEach((detail) => {
      const summary = $("summary", detail);
      const focused = document.activeElement;
      detail.open = compactContent.matches ? (mobileOpen.get(detail.id) ?? false) : true;
      summary.tabIndex = compactContent.matches ? 0 : -1;
      if (compactContent.matches && !detail.open && detail.contains(focused)) summary.focus({ preventScroll: true });
    });
    document.documentElement.classList.add("disclosures-ready");
  }
  mobileDisclosures.forEach((detail) => detail.addEventListener("toggle", () => {
    if (compactContent.matches) mobileOpen.set(detail.id, detail.open);
  }));
  compactContent.addEventListener("change", syncDisclosures);
  syncDisclosures();

  // Header menu drawer.
  const menu = $(".trnav .menu");
  const nav = $("#homepage-nav");
  function closeMenu() {
    if (!menu || !nav) return;
    nav.classList.remove("open");
    menu.setAttribute("aria-expanded", "false");
  }
  if (menu && nav) {
    menu.addEventListener("click", () => {
      const open = menu.getAttribute("aria-expanded") !== "true";
      nav.classList.toggle("open", open);
      menu.setAttribute("aria-expanded", String(open));
    });
    nav.addEventListener("click", (event) => { if (event.target.closest("a")) closeMenu(); });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && menu.getAttribute("aria-expanded") === "true") { closeMenu(); menu.focus(); }
    });
  }

  // Model search dialog, fed by the public picker endpoint.
  const search = $("#model-search");
  const query = $("#model-query");
  if (!search || !query) return;
  let icons = {};
  try { icons = JSON.parse($("#site-chrome-data")?.textContent || "{}").publisher_icons || {}; } catch { icons = {}; }
  let searchTrigger;
  let searchModels = null;
  let searchLoading = false;
  let searchFailed = false;
  function renderSearch() {
    const results = $("#search-results");
    results.replaceChildren();
    $("#search-retry").hidden = !searchFailed;
    if (searchLoading) { $("#search-count").textContent = "Loading the model catalog…"; return; }
    if (searchFailed) { $("#search-count").textContent = "The catalog could not be loaded. Try again or browse all models."; return; }
    const needle = query.value.trim().toLowerCase();
    const models = (searchModels || []).filter((m) => (m.name + " " + m.id).toLowerCase().includes(needle));
    $("#search-count").textContent = models.length
      ? `${models.length} ${models.length === 1 ? "match" : "matches"}${models.length > 50 ? " · showing the first 50" : ""}`
      : "No matching models. Try another name or browse all models.";
    for (const model of models.slice(0, 50)) {
      const a = document.createElement("a");
      a.href = "/models/" + model.id.split("/").map(encodeURIComponent).join("/");
      const icon = document.createElement("img");
      icon.className = "model-lab-icon";
      icon.src = icons[model.id.split("/")[0]] || "/static/homepage/mark.svg";
      icon.alt = "";
      icon.width = 30;
      icon.height = 30;
      const label = document.createElement("span");
      label.className = "model-label";
      label.textContent = model.name;
      const id = document.createElement("small");
      id.textContent = model.id;
      label.append(id);
      a.append(icon, label);
      results.append(a);
    }
  }
  async function loadSearch() {
    if (searchLoading || searchModels) return;
    searchLoading = true;
    searchFailed = false;
    renderSearch();
    try {
      const response = await fetch("/v1/models/picker", { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error("Catalog unavailable");
      const payload = await response.json();
      if (!Array.isArray(payload.data)) throw new Error("Invalid catalog");
      searchModels = payload.data.filter((m) => typeof m.id === "string" && typeof m.name === "string" && !m.trustedrouter?.internal_only);
    } catch {
      searchFailed = true;
    } finally {
      searchLoading = false;
      renderSearch();
    }
  }
  function openSearch(trigger, preset = "") {
    searchTrigger = trigger;
    closeMenu();
    search.showModal();
    query.value = preset;
    renderSearch();
    query.focus();
    void loadSearch();
  }
  $("#search-retry").addEventListener("click", () => void loadSearch());
  $$("[data-open-search]").forEach((button) => button.addEventListener("click", () => openSearch(button)));
  $("[data-close-search]").addEventListener("click", () => search.close());
  search.addEventListener("close", () => {
    const target = searchTrigger?.getClientRects().length ? searchTrigger : menu;
    target?.focus();
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); search.close(); }
  });
  search.addEventListener("click", (event) => {
    if (event.target !== search) return;
    const box = search.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) search.close();
  });
  query.addEventListener("input", renderSearch);
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openSearch(document.activeElement); }
  });
})();
