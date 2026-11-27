(() => {
  const explorer = document.querySelector("[data-model-explorer]");
  if (!explorer) return;

  const results = explorer.querySelector("[data-model-results]");
  const search = document.querySelector("[data-model-search]");
  const sort = explorer.querySelector("[data-model-sort]");
  const count = explorer.querySelector("[data-model-result-count]");
  const detail = explorer.querySelector("[data-model-result-detail]");
  const empty = explorer.querySelector("[data-model-empty]");
  const showMore = explorer.querySelector("[data-model-show-more]");
  const clearButtons = [...explorer.querySelectorAll("[data-model-clear]")];
  const requestLinks = [...document.querySelectorAll("[data-model-request]")];
  const cards = [...explorer.querySelectorAll("[data-model-card]")];
  const kindTabs = [...explorer.querySelectorAll("[data-model-kind-tab]")];
  const kindMore = explorer.querySelector("[data-model-kind-more]");
  const sortHeads = [...explorer.querySelectorAll("[data-sort-key]")];
  const routeFilters = [...explorer.querySelectorAll("[data-route-filter]")];
  const pageSize = Number.parseInt(explorer.dataset.pageSize || "24", 10);
  const params = new URLSearchParams(window.location.search);
  const kinds = new Set(kindTabs.map((tab) => tab.dataset.modelKindTab));
  const kindNouns = { all: "", text: "text ", image: "image ", video: "video ", speech: "speech ", embeddings: "embedding " };
  const ascending = new Set(["price", "output", "name", "featured"]);
  let visibleLimit = pageSize;
  let kind = kinds.has(params.get("makes")) ? params.get("makes") : "all";

  const normalize = (value) => value.trim().toLocaleLowerCase().replace(/\s+/g, " ");
  const numberValue = (card, key, fallback = Number.POSITIVE_INFINITY) => {
    const raw = card.dataset[key];
    if (raw === undefined || raw === "") return fallback;
    const value = Number(raw);
    return Number.isFinite(value) ? value : fallback;
  };
  const isMedia = (card) => card.dataset.kind === "image" || card.dataset.kind === "video";

  search.value = params.get("q") || "";
  const requestedSort = params.get("sort") === "input" ? "price" : params.get("sort");
  if ([...sort.options].some((option) => option.value === requestedSort)) {
    sort.value = requestedSort;
  }

  function priceValue(card) {
    if (kind === "image" || kind === "video") return numberValue(card, "mediaPrice");
    return isMedia(card) ? Number.POSITIVE_INFINITY : numberValue(card, "inputPrice");
  }

  function sortCards(matches) {
    const mode = sort.value;
    return matches.sort((left, right) => {
      if (mode === "price") return priceValue(left) - priceValue(right);
      if (mode === "output") return numberValue(left, "outputPrice") - numberValue(right, "outputPrice");
      if (mode === "context") return numberValue(right, "context", 0) - numberValue(left, "context", 0);
      if (mode === "providers") return numberValue(right, "providerCount", 0) - numberValue(left, "providerCount", 0);
      if (mode === "name") return left.dataset.modelId.localeCompare(right.dataset.modelId);
      return numberValue(left, "initialIndex", 0) - numberValue(right, "initialIndex", 0);
    });
  }

  function writeUrl() {
    const next = new URLSearchParams(window.location.search);
    const query = search.value.trim();
    if (query) next.set("q", query);
    else next.delete("q");
    if (sort.value !== "featured") next.set("sort", sort.value);
    else next.delete("sort");
    if (kind !== "all") next.set("makes", kind);
    else next.delete("makes");
    const queryString = next.toString();
    history.replaceState(null, "", `${window.location.pathname}${queryString ? `?${queryString}` : ""}`);
  }

  function renderChrome() {
    explorer.dataset.activeKind = kind;
    kindTabs.forEach((tab) => {
      if (tab.dataset.modelKindTab === kind) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    });
    if (kindMore) {
      kindMore.value = kind === "speech" || kind === "embeddings" ? kind : "";
      kindMore.closest("label").classList.toggle("is-active", Boolean(kindMore.value));
    }
    sortHeads.forEach((button) => {
      const th = button.closest("th");
      const active = button.dataset.sortKey === sort.value;
      th.classList.toggle("is-sorted", active);
      if (active) th.setAttribute("aria-sort", ascending.has(sort.value) ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });
    routeFilters.forEach((link) => {
      const url = new URL(link.href, window.location.origin);
      if (kind !== "all") url.searchParams.set("makes", kind);
      else url.searchParams.delete("makes");
      if (search.value.trim()) url.searchParams.set("q", search.value.trim());
      else url.searchParams.delete("q");
      if (sort.value !== "featured") url.searchParams.set("sort", sort.value);
      else url.searchParams.delete("sort");
      link.href = `${url.pathname}${url.search}`;
    });
  }

  function render() {
    const query = normalize(search.value);
    const matches = sortCards(cards.filter((card) => (kind === "all" || card.dataset.kind === kind)
      && window.TRModelSearch.matches(card.dataset.searchText || "", query)));

    matches.forEach((card) => results.append(card));
    const visible = matches.slice(0, visibleLimit);
    const visibleSet = new Set(visible);
    cards.forEach((card) => { card.hidden = !visibleSet.has(card); });

    const noun = kindNouns[kind] || "";
    count.textContent = `${matches.length} ${noun}${matches.length === 1 ? "model" : "models"}`;
    detail.textContent = matches.length > visible.length ? ` · showing ${visible.length}` : "";
    empty.hidden = matches.length !== 0;
    showMore.hidden = matches.length <= visible.length;
    if (!showMore.hidden) {
      const remaining = matches.length - visible.length;
      showMore.textContent = `Show ${Math.min(pageSize, remaining)} more models`;
    }
    clearButtons.forEach((button) => { button.hidden = !query && sort.value === "featured"; });
    const requestParams = new URLSearchParams({ category: "model" });
    if (search.value.trim()) requestParams.set("model", search.value.trim().slice(0, 200));
    requestLinks.forEach((link) => {
      link.href = `/support?${requestParams.toString()}#support-inquiry`;
    });
    renderChrome();
    writeUrl();
  }

  function setKind(next) {
    kind = kinds.has(next) ? next : "all";
    visibleLimit = pageSize;
    render();
  }

  kindTabs.forEach((tab) => tab.addEventListener("click", (event) => {
    event.preventDefault();
    setKind(tab.dataset.modelKindTab);
  }));
  if (kindMore) kindMore.addEventListener("change", () => setKind(kindMore.value || "all"));
  const routeSelect = explorer.querySelector("[data-route-select]");
  if (routeSelect) routeSelect.addEventListener("change", () => {
    const url = new URL(routeSelect.value, window.location.origin);
    if (kind !== "all") url.searchParams.set("makes", kind);
    if (search.value.trim()) url.searchParams.set("q", search.value.trim());
    if (sort.value !== "featured") url.searchParams.set("sort", sort.value);
    window.location.href = `${url.pathname}${url.search}`;
  });
  sortHeads.forEach((button) => button.addEventListener("click", () => {
    sort.value = sort.value === button.dataset.sortKey ? "featured" : button.dataset.sortKey;
    visibleLimit = pageSize;
    render();
  }));
  search.addEventListener("input", () => {
    visibleLimit = pageSize;
    render();
    // Keep the response to typing visible while the input stays in the sticky header.
    count.scrollIntoView({ block: "start", behavior: "instant" });
  });
  sort.addEventListener("change", () => {
    visibleLimit = pageSize;
    render();
  });
  showMore.addEventListener("click", () => {
    visibleLimit += pageSize;
    render();
  });
  clearButtons.forEach((button) => button.addEventListener("click", () => {
    search.value = "";
    sort.value = "featured";
    visibleLimit = pageSize;
    search.focus();
    render();
  }));

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function dollarsPerMillion(raw) {
    const value = Number(raw);
    if (!Number.isFinite(value)) return null;
    return value * 1_000_000;
  }

  function formatDollarValue(value) {
    if (value >= 100) return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}/1M`;
    if (value >= 1) return `$${value.toFixed(2).replace(/\.00$/, "")}/1M`;
    if (value >= 0.01) return `$${value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "")}/1M`;
    return `$${value.toFixed(5).replace(/0+$/, "").replace(/\.$/, "")}/1M`;
  }

  function priceRange(routes, key) {
    const values = routes
      .map((route) => dollarsPerMillion(route.pricing && route.pricing[key]))
      .filter((value) => value !== null && value > 0)
      .sort((left, right) => left - right);
    if (!values.length) return key === "input_cache_read" ? "Not published" : "—";
    if (values[0] === values[values.length - 1]) return formatDollarValue(values[0]);
    return `${formatDollarValue(values[0])} – ${formatDollarValue(values[values.length - 1])}`;
  }

  function privacyCell(routes) {
    const postures = routes.map((route) => route.trustedrouter || {});
    const cell = routeCell("Available privacy", undefined, "catalog-privacy-summary");
    if (postures.some((posture) => posture.provider_confidential_compute === true && posture.provider_e2ee === true && posture.provider_zero_data_retention === true)) {
      const badge = element("span", "privacy-badge privacy-confidential", "Confidential");
      badge.dataset.privacy = "confidential";
      badge.title = "Available route with verified provider confidential compute, end-to-end encryption (E2EE), and explicit zero data retention (ZDR).";
      cell.append(badge);
    }
    if (postures.some((posture) => posture.provider_zero_data_retention === true)) {
      const badge = element("span", "privacy-badge privacy-zdr", "ZDR");
      badge.dataset.privacy = "zdr";
      badge.title = "Zero data retention on an available Credits route; BYOK account terms may differ.";
      cell.append(badge);
    }
    if (!cell.children.length) cell.textContent = "Not verified";
    return cell;
  }

  function routeCell(label, text, className = "") {
    const cell = element("div", className, text);
    cell.dataset.label = label;
    return cell;
  }

  function renderRoutes(container, routes, mediaPrices) {
    container.replaceChildren();
    const creditRoutes = routes.filter((route) => route.usage_type === "Credits");
    if (!creditRoutes.length) {
      container.append(element("p", "model-route-error", "No active Credits provider routes are published for this model."));
      return;
    }

    const grouped = new Map();
    creditRoutes.forEach((route) => {
      const key = route.provider || route.provider_name || "provider";
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(route);
    });

    const header = element("div", "model-route-head");
    (mediaPrices ? ["Provider", "Price", "Available privacy"] : ["Provider", "Input", "Cached input", "Output", "Available privacy"]).forEach((label) => header.append(element("span", "", label)));
    container.append(header);

    [...grouped.entries()]
      .sort(([, left], [, right]) => String(left[0].provider_name || left[0].name).localeCompare(String(right[0].provider_name || right[0].name)))
      .forEach(([slug, providerRoutes]) => {
        const first = providerRoutes[0];
        const row = element("div", "model-route-row");
        const providerCell = element("div", "model-route-provider");
        const providerLink = element("a", "", first.provider_name || first.name || slug);
        providerLink.href = `/providers/${encodeURIComponent(slug)}`;
        const logo = document.createElement("img");
        logo.src = `/static/provider-logos/${encodeURIComponent(slug)}.png`;
        logo.alt = "";
        logo.width = 24;
        logo.height = 24;
        providerLink.prepend(logo);
        providerCell.dataset.label = "Provider";
        providerCell.append(providerLink);
        row.append(providerCell);
        if (mediaPrices) {
          row.append(routeCell("Price", mediaPrices[slug] || "Not published", "model-route-price"));
        } else {
          row.append(routeCell("Input", priceRange(providerRoutes, "prompt"), "model-route-price"));
          row.append(routeCell("Cached input", priceRange(providerRoutes, "input_cache_read"), "model-route-price"));
          row.append(routeCell("Output", priceRange(providerRoutes, "completion"), "model-route-price"));
        }
        row.append(privacyCell(providerRoutes));
        container.append(row);
      });
  }

  explorer.querySelectorAll("[data-route-toggle]").forEach((toggle) => {
    toggle.addEventListener("click", async () => {
      const panel = document.getElementById(toggle.getAttribute("aria-controls"));
      if (!panel) return;
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      panel.hidden = !open;
      if (!open || toggle.dataset.loaded === "true" || toggle.dataset.loading === "true") return;
      const container = panel.querySelector("[data-route-results]");
      toggle.dataset.loading = "true";
      container.replaceChildren(element("p", "model-route-loading", "Loading current provider routes…"));
      try {
        const response = await fetch(toggle.dataset.endpointsUrl, { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        renderRoutes(container, Array.isArray(payload.data) ? payload.data : [],
          toggle.dataset.mediaPrices ? JSON.parse(toggle.dataset.mediaPrices) : null);
        toggle.dataset.loaded = "true";
      } catch (_error) {
        container.replaceChildren(element("p", "model-route-error", "Provider routes could not be loaded. Try again."));
      } finally {
        delete toggle.dataset.loading;
      }
    });
  });

  render();
})();
