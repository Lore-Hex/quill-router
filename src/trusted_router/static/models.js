(() => {
  const explorer = document.querySelector("[data-model-explorer]");
  if (!explorer) return;

  const results = explorer.querySelector("[data-model-results]");
  const search = explorer.querySelector("[data-model-search]");
  const sort = explorer.querySelector("[data-model-sort]");
  const count = explorer.querySelector("[data-model-result-count]");
  const detail = explorer.querySelector("[data-model-result-detail]");
  const empty = explorer.querySelector("[data-model-empty]");
  const showMore = explorer.querySelector("[data-model-show-more]");
  const clearButtons = [...explorer.querySelectorAll("[data-model-clear]")];
  const cards = [...explorer.querySelectorAll("[data-model-card]")];
  const pageSize = Number.parseInt(explorer.dataset.pageSize || "24", 10);
  const params = new URLSearchParams(window.location.search);
  let visibleLimit = pageSize;

  const normalize = (value) => value.trim().toLocaleLowerCase().replace(/\s+/g, " ");
  const numberValue = (card, key, fallback = Number.POSITIVE_INFINITY) => {
    const raw = card.dataset[key];
    if (raw === undefined || raw === "") return fallback;
    const value = Number(raw);
    return Number.isFinite(value) ? value : fallback;
  };

  search.value = params.get("q") || "";
  if ([...sort.options].some((option) => option.value === params.get("sort"))) {
    sort.value = params.get("sort");
  }

  function sortCards(matches) {
    const mode = sort.value;
    return matches.sort((left, right) => {
      if (mode === "input") return numberValue(left, "inputPrice") - numberValue(right, "inputPrice");
      if (mode === "cached") return numberValue(left, "cachedPrice") - numberValue(right, "cachedPrice");
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
    const queryString = next.toString();
    history.replaceState(null, "", `${window.location.pathname}${queryString ? `?${queryString}` : ""}`);
  }

  function render() {
    const query = normalize(search.value);
    const terms = query.split(" ").filter(Boolean);
    const matches = sortCards(cards.filter((card) => {
      const haystack = normalize(card.dataset.searchText || "");
      return terms.every((term) => haystack.includes(term));
    }));

    matches.forEach((card) => results.append(card));
    const visible = matches.slice(0, visibleLimit);
    const visibleSet = new Set(visible);
    cards.forEach((card) => { card.hidden = !visibleSet.has(card); });

    count.textContent = `${matches.length} ${matches.length === 1 ? "result" : "results"}`;
    detail.textContent = matches.length > visible.length ? ` · showing ${visible.length}` : "";
    empty.hidden = matches.length !== 0;
    showMore.hidden = matches.length <= visible.length;
    if (!showMore.hidden) {
      const remaining = matches.length - visible.length;
      showMore.textContent = `Show ${Math.min(pageSize, remaining)} more models`;
    }
    clearButtons.forEach((button) => { button.hidden = !query && sort.value === "featured"; });
    writeUrl();
  }

  search.addEventListener("input", () => {
    visibleLimit = pageSize;
    render();
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
  explorer.querySelectorAll("[data-model-query]").forEach((button) => {
    button.addEventListener("click", () => {
      search.value = button.dataset.modelQuery || "";
      visibleLimit = pageSize;
      render();
      search.focus();
    });
  });

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

  function privacyLabel(routes) {
    const postures = routes.map((route) => route.trustedrouter || {});
    if (postures.some((posture) => posture.provider_confidential_compute === true && posture.provider_e2ee === true)) return "E2EE";
    if (postures.some((posture) => posture.provider_zero_data_retention === true)) return "ZDR";
    if (postures.some((posture) => posture.stores_content === false)) return "No store";
    return "Standard";
  }

  function routeCell(label, text, className = "") {
    const cell = element("div", className, text);
    cell.dataset.label = label;
    return cell;
  }

  function renderRoutes(container, routes) {
    container.replaceChildren();
    if (!routes.length) {
      container.append(element("p", "model-route-error", "No active provider routes are published for this model."));
      return;
    }

    const grouped = new Map();
    routes.forEach((route) => {
      const key = route.provider || route.provider_name || "provider";
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(route);
    });

    const header = element("div", "model-route-head");
    ["Provider", "Routes", "Input", "Cached input", "Output", "Privacy"].forEach((label) => header.append(element("span", "", label)));
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
        row.append(routeCell("Routes", [...new Set(providerRoutes.map((route) => route.usage_type))].join(" + ")));
        row.append(routeCell("Input", priceRange(providerRoutes, "prompt"), "model-route-price"));
        row.append(routeCell("Cached input", priceRange(providerRoutes, "input_cache_read"), "model-route-price"));
        row.append(routeCell("Output", priceRange(providerRoutes, "completion"), "model-route-price"));
        row.append(routeCell("Privacy", privacyLabel(providerRoutes)));
        container.append(row);
      });
  }

  explorer.querySelectorAll("[data-endpoints-url]").forEach((details) => {
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.loaded === "true" || details.dataset.loading === "true") return;
      const container = details.querySelector("[data-route-results]");
      details.dataset.loading = "true";
      container.replaceChildren(element("p", "model-route-loading", "Loading current provider routes…"));
      try {
        const response = await fetch(details.dataset.endpointsUrl, { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        renderRoutes(container, Array.isArray(payload.data) ? payload.data : []);
        details.dataset.loaded = "true";
      } catch (_error) {
        container.replaceChildren(element("p", "model-route-error", "Provider routes could not be loaded. Try again."));
      } finally {
        delete details.dataset.loading;
      }
    });
  });

  render();
})();
