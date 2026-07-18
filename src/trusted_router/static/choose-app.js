(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const CATALOG_URL = "/choose/catalog.json";
  const VERTICES = {
    quality: { x: 500, y: 74 },
    cost: { x: 92, y: 824 },
    speed: { x: 908, y: 824 },
  };
  const QUALITY_LEVELS = [
    { key: "simple", label: "Simple", detail: "IQ 0+", floor: 0 },
    { key: "balanced", label: "Balanced", detail: "IQ 105+", floor: 105 },
    { key: "smart", label: "Smart", detail: "IQ 115+", floor: 115 },
    { key: "frontier", label: "Frontier", detail: "IQ 124+", floor: 124 },
  ];
  const SPEED_LEVELS = [
    { key: "realtime", label: "Realtime", detail: "TTFT <= 1s", maxTtftMs: 1000 },
    { key: "seconds", label: "Seconds", detail: "TTFT <= 5s", maxTtftMs: 5000 },
    { key: "minutes", label: "Minutes", detail: "Prefer measured", maxTtftMs: null },
    { key: "any", label: "Any", detail: "No speed floor", maxTtftMs: null },
  ];
  const EXAMPLES = [
    "Refactor a React component and write tests",
    "Extract invoice fields into JSON",
    "Analyze a hard proof",
    "Run an interactive support chat",
  ];
  const TASK_KEYWORDS = {
    frontier: ["proof", "theorem", "research", "novel", "architect", "distributed", "rigorous", "cryptograph"],
    smart: ["code", "debug", "refactor", "analyze", "reason", "plan", "implement", "review", "math", "legal"],
    balanced: ["summar", "rewrite", "draft", "translate", "explain", "outline", "classif", "extract"],
  };
  const DOMAIN_KEYWORDS = {
    coding: ["code", "debug", "refactor", "react", "javascript", "typescript", "python", "sql", "test", "api"],
    math: ["math", "theorem", "proof", "equation", "algebra", "calculus"],
    vision: ["image", "photo", "ocr", "screenshot", "diagram", "visual"],
    reasoning: ["reason", "strategy", "plan", "analyze", "decision", "legal"],
    writing: ["write", "draft", "rewrite", "essay", "blog"],
  };
  const SPEED_KEYWORDS = {
    realtime: ["real time", "realtime", "interactive", "chat", "autocomplete", "voice", "live"],
    any: ["overnight", "batch", "offline", "nightly", "background"],
    minutes: ["deep", "thorough", "report", "agent"],
  };
  const PRIVACY_COPY = {
    0: {
      title: "Open upstream routing",
      body: "The gateway remains attested and stores no prompt or output. Upstream retention follows the exact provider route shown.",
    },
    2: {
      title: "Zero-retention routes only",
      body: "Every displayed endpoint is marked zero retention or confidential by its provider-specific catalog policy.",
    },
    3: {
      title: "Confidential provider compute only",
      body: "Every displayed endpoint adds provider-side confidential compute and end-to-end encryption after the attested gateway.",
    },
  };

  const state = {
    catalog: null,
    privacy: 0,
    quality: "balanced",
    speed: "any",
    domain: null,
    selected: null,
    preference: { quality: 0.34, cost: 0.33, speed: 0.33 },
    dragging: false,
  };

  const dom = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function htmlElement(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function svgElement(tag, attributes = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) {
      node.setAttribute(name, String(value));
    }
    return node;
  }

  function safeUrl(value) {
    try {
      const url = new URL(String(value));
      return url.protocol === "https:" ? url.href : null;
    } catch (_error) {
      return null;
    }
  }

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function validEndpoint(endpoint) {
    return endpoint
      && typeof endpoint.provider === "string"
      && typeof endpoint.provider_name === "string"
      && typeof endpoint.usage_type === "string"
      && Number.isInteger(endpoint.privacy_tier)
      && Number.isInteger(endpoint.prompt_price_microdollars_per_million_tokens)
      && Number.isInteger(endpoint.completion_price_microdollars_per_million_tokens)
      && endpoint.prompt_price_microdollars_per_million_tokens >= 0
      && endpoint.completion_price_microdollars_per_million_tokens >= 0;
  }

  function normalizeCatalog(payload) {
    if (!payload || !Array.isArray(payload.models) || !Array.isArray(payload.routes)) {
      throw new Error("The model catalog returned an invalid response.");
    }
    const models = payload.models.map((model) => {
      const score = finiteNumber(model?.quality?.score);
      const endpoints = Array.isArray(model?.endpoints) ? model.endpoints.filter(validEndpoint) : [];
      if (!model || typeof model.id !== "string" || typeof model.name !== "string" || score === null || score <= 0 || !endpoints.length) {
        return null;
      }
      return { ...model, quality: { ...model.quality, score }, endpoints };
    }).filter(Boolean);
    if (!models.length) throw new Error("No independently scored routes are currently available.");

    const routes = payload.routes.filter((route) => {
      if (!route
        || typeof route.id !== "string"
        || !Number.isInteger(route.min_privacy_tier)
        || typeof route.description !== "string") return false;
      if (route.pricing_mode === "component_usage") return true;
      return route.pricing_mode === "selected_route" && [
        route.prompt_price_min_microdollars_per_million_tokens,
        route.prompt_price_max_microdollars_per_million_tokens,
        route.completion_price_min_microdollars_per_million_tokens,
        route.completion_price_max_microdollars_per_million_tokens,
      ].every((price) => Number.isInteger(price) && price >= 0);
    });
    if (!routes.length) throw new Error("The routing catalog is currently unavailable.");
    return { ...payload, models, routes };
  }

  function endpointsFor(model) {
    return model.endpoints.filter((endpoint) => endpoint.privacy_tier >= state.privacy);
  }

  function routePrice(endpoint) {
    return Math.round(
      endpoint.prompt_price_microdollars_per_million_tokens * 0.25
      + endpoint.completion_price_microdollars_per_million_tokens * 0.75,
    );
  }

  function measuredPerformance(endpoint) {
    const performance = endpoint.performance;
    const ttft = finiteNumber(performance?.p50_ttft_ms);
    const throughput = finiteNumber(performance?.p50_tokens_per_second);
    const samples = finiteNumber(performance?.sample_count) || 0;
    if (!performance || samples <= 0 || ttft === null || throughput === null || throughput <= 0) return null;
    return { endpoint, ttft, throughput, samples, uptime: finiteNumber(performance.uptime) };
  }

  function modelFacts(model) {
    if (!model) return null;
    const endpoints = endpointsFor(model);
    if (!endpoints.length) return null;
    const cheapest = [...endpoints].sort((left, right) => routePrice(left) - routePrice(right))[0];
    const measured = endpoints.map(measuredPerformance).filter(Boolean);
    const fastest = measured.sort((left, right) => {
      if (right.throughput !== left.throughput) return right.throughput - left.throughput;
      return left.ttft - right.ttft;
    })[0] || null;
    const lowestTtft = measured.sort((left, right) => left.ttft - right.ttft)[0] || null;
    return {
      model,
      endpoints,
      cheapest,
      blendedPrice: routePrice(cheapest),
      fastest,
      lowestTtft,
    };
  }

  function qualityLevel() {
    return QUALITY_LEVELS.find((level) => level.key === state.quality) || QUALITY_LEVELS[1];
  }

  function speedLevel() {
    return SPEED_LEVELS.find((level) => level.key === state.speed) || SPEED_LEVELS[1];
  }

  function passesRequirements(facts) {
    if (facts.model.quality.score < qualityLevel().floor) return false;
    const maxTtftMs = speedLevel().maxTtftMs;
    if (maxTtftMs === null) return true;
    return Boolean(facts.lowestTtft && facts.lowestTtft.ttft <= maxTtftMs);
  }

  function normalizedMetric(value, low, high, inverse = false) {
    if (!Number.isFinite(value)) return 0;
    if (high <= low) return 0.5;
    const normalized = Math.max(0, Math.min(1, (value - low) / (high - low)));
    return inverse ? 1 - normalized : normalized;
  }

  function scoreFacts(factsList) {
    const qualities = factsList.map((facts) => facts.model.quality.score);
    const prices = factsList.map((facts) => Math.log10(Math.max(1, facts.blendedPrice)));
    const measuredSpeeds = factsList
      .map((facts) => facts.fastest?.throughput)
      .filter((value) => Number.isFinite(value) && value > 0)
      .map((value) => Math.log10(value));
    const ranges = {
      quality: [Math.min(...qualities), Math.max(...qualities)],
      price: [Math.min(...prices), Math.max(...prices)],
      speed: measuredSpeeds.length
        ? [Math.min(...measuredSpeeds), Math.max(...measuredSpeeds)]
        : [0, 1],
    };
    return factsList.map((facts) => {
      const qualityScore = normalizedMetric(facts.model.quality.score, ...ranges.quality);
      const costScore = normalizedMetric(
        Math.log10(Math.max(1, facts.blendedPrice)),
        ...ranges.price,
        true,
      );
      const speedScore = facts.fastest
        ? normalizedMetric(Math.log10(facts.fastest.throughput), ...ranges.speed)
        : 0;
      const matchScore = (
        qualityScore * state.preference.quality
        + costScore * state.preference.cost
        + speedScore * state.preference.speed
      );
      return { ...facts, scores: { quality: qualityScore, cost: costScore, speed: speedScore }, matchScore };
    });
  }

  function evaluatedFacts() {
    if (!state.catalog) return [];
    return state.catalog.models.map(modelFacts).filter(Boolean);
  }

  function barycentricPoint(weights) {
    return {
      x: weights.quality * VERTICES.quality.x + weights.cost * VERTICES.cost.x + weights.speed * VERTICES.speed.x,
      y: weights.quality * VERTICES.quality.y + weights.cost * VERTICES.cost.y + weights.speed * VERTICES.speed.y,
    };
  }

  function metricWeights(scores) {
    const total = scores.quality + scores.cost + scores.speed;
    if (total <= 0) return { quality: 1 / 3, cost: 1 / 3, speed: 1 / 3 };
    return {
      quality: scores.quality / total,
      cost: scores.cost / total,
      speed: scores.speed / total,
    };
  }

  function pointWeights(x, y) {
    const a = VERTICES.quality;
    const b = VERTICES.cost;
    const c = VERTICES.speed;
    const denominator = (b.y - c.y) * (a.x - c.x) + (c.x - b.x) * (a.y - c.y);
    let quality = ((b.y - c.y) * (x - c.x) + (c.x - b.x) * (y - c.y)) / denominator;
    let cost = ((c.y - a.y) * (x - c.x) + (a.x - c.x) * (y - c.y)) / denominator;
    let speed = 1 - quality - cost;
    quality = Math.max(0, quality);
    cost = Math.max(0, cost);
    speed = Math.max(0, speed);
    const total = quality + cost + speed || 1;
    return { quality: quality / total, cost: cost / total, speed: speed / total };
  }

  function tierClass(tier) {
    if (tier >= 3) return "tier-3";
    if (tier >= 2) return "tier-2";
    return "tier-0";
  }

  function tierShortLabel(tier) {
    if (tier >= 3) return "TEE";
    if (tier >= 2) return "ZDR";
    return "Open";
  }

  function drawScaffold() {
    dom.triangle.replaceChildren();
    const defs = svgElement("defs");
    const filter = svgElement("filter", { id: "pointGlow", x: "-80%", y: "-80%", width: "260%", height: "260%" });
    filter.appendChild(svgElement("feGaussianBlur", { stdDeviation: 6, result: "blur" }));
    const merge = svgElement("feMerge");
    merge.appendChild(svgElement("feMergeNode", { in: "blur" }));
    merge.appendChild(svgElement("feMergeNode", { in: "SourceGraphic" }));
    filter.appendChild(merge);
    defs.appendChild(filter);
    dom.triangle.appendChild(defs);

    dom.triangle.appendChild(svgElement("polygon", {
      points: `${VERTICES.quality.x},${VERTICES.quality.y} ${VERTICES.cost.x},${VERTICES.cost.y} ${VERTICES.speed.x},${VERTICES.speed.y}`,
      fill: "#0a111c",
      stroke: "#344863",
      "stroke-width": 2,
    }));
    for (const amount of [0.2, 0.4, 0.6, 0.8]) {
      for (const axis of ["quality", "cost", "speed"]) {
        const other = ["quality", "cost", "speed"].filter((name) => name !== axis);
        const start = barycentricPoint({ [axis]: amount, [other[0]]: 1 - amount, [other[1]]: 0 });
        const end = barycentricPoint({ [axis]: amount, [other[0]]: 0, [other[1]]: 1 - amount });
        dom.triangle.appendChild(svgElement("line", {
          x1: start.x, y1: start.y, x2: end.x, y2: end.y,
          stroke: "#1a2739", "stroke-width": 1,
        }));
      }
    }
    const labels = [
      ["QUALITY", 500, 42, "#aa8cff", "middle"],
      ["LOW COST", 60, 862, "#36d39a", "start"],
      ["MEASURED SPEED", 940, 862, "#f5b642", "end"],
    ];
    for (const [label, x, y, fill, anchor] of labels) {
      const text = svgElement("text", { x, y, fill, "text-anchor": anchor, "font-size": 18, "font-weight": 800 });
      text.textContent = label;
      dom.triangle.appendChild(text);
    }
    dom.linksLayer = svgElement("g", { "aria-hidden": "true" });
    dom.pointsLayer = svgElement("g");
    dom.preferenceLayer = svgElement("g", { "aria-hidden": "true" });
    dom.triangle.append(dom.linksLayer, dom.pointsLayer, dom.preferenceLayer);
  }

  function drawChart(scoredFacts, ranked) {
    dom.linksLayer.replaceChildren();
    dom.pointsLayer.replaceChildren();
    dom.preferenceLayer.replaceChildren();
    const matchingIds = new Set(ranked.map((facts) => facts.model.id));
    const topId = ranked[0]?.model.id || null;
    const order = [...scoredFacts].sort((left, right) => Number(matchingIds.has(left.model.id)) - Number(matchingIds.has(right.model.id)));
    for (const facts of order) {
      const point = barycentricPoint(metricWeights(facts.scores));
      const matches = matchingIds.has(facts.model.id);
      const top = facts.model.id === topId;
      const selected = facts.model.id === state.selected;
      const group = svgElement("g", {
        transform: `translate(${point.x},${point.y})`,
        "data-model-id": facts.model.id,
        role: "img",
        "aria-label": `${facts.model.name}, AI IQ ${facts.model.quality.score}, ${tierShortLabel(state.privacy)}-qualified routes`,
      });
      group.style.cursor = "pointer";
      const title = svgElement("title");
      title.textContent = `${facts.model.name}: AI IQ ${facts.model.quality.score}`;
      group.appendChild(title);
      const radius = top ? 14 : matches ? 9 : 6;
      if (top) {
        group.appendChild(svgElement("circle", { r: radius + 8, fill: "none", stroke: "#f4c95d", "stroke-width": 2, opacity: 0.75 }));
      }
      if (selected && !top) {
        group.appendChild(svgElement("circle", { r: radius + 5, fill: "none", stroke: "#ffffff", "stroke-width": 1.5, opacity: 0.7 }));
      }
      group.appendChild(svgElement("circle", {
        r: radius,
        fill: state.privacy >= 3 ? "#37c96f" : state.privacy >= 2 ? "#38c6df" : "#95a8c2",
        "fill-opacity": matches ? 0.95 : 0.18,
        stroke: top ? "#f4c95d" : "#0a111c",
        "stroke-width": top ? 3 : 1.5,
        filter: top ? "url(#pointGlow)" : "",
      }));
      dom.pointsLayer.appendChild(group);
    }

    const preferencePoint = barycentricPoint(state.preference);
    if (ranked[0]) {
      const topFacts = scoredFacts.find((facts) => facts.model.id === ranked[0].model.id);
      if (topFacts) {
        const topPoint = barycentricPoint(metricWeights(topFacts.scores));
        dom.linksLayer.appendChild(svgElement("line", {
          x1: preferencePoint.x, y1: preferencePoint.y, x2: topPoint.x, y2: topPoint.y,
          stroke: "#f4c95d", "stroke-width": 1.5, "stroke-dasharray": "5 6", opacity: 0.5,
        }));
      }
    }
    const handle = svgElement("g", { transform: `translate(${preferencePoint.x},${preferencePoint.y})` });
    handle.appendChild(svgElement("circle", { r: 22, fill: "#f4c95d", "fill-opacity": 0.12 }));
    handle.appendChild(svgElement("circle", { r: 12, fill: "#070b12", stroke: "#f4c95d", "stroke-width": 3 }));
    handle.appendChild(svgElement("circle", { r: 4, fill: "#f4c95d" }));
    const label = svgElement("text", { x: 0, y: -27, fill: "#f4c95d", "text-anchor": "middle", "font-size": 13, "font-weight": 800 });
    label.textContent = "YOU";
    handle.appendChild(label);
    dom.preferenceLayer.appendChild(handle);
  }

  function formatDollars(microdollars) {
    const dollars = microdollars / 1_000_000;
    if (dollars >= 100) return `$${dollars.toFixed(0)}`;
    if (dollars >= 1) return `$${dollars.toFixed(2).replace(/\.00$/, "")}`;
    return `$${dollars.toFixed(6).replace(/0+$/, "").replace(/\.$/, "")}`;
  }

  function formatContext(tokens) {
    const value = finiteNumber(tokens) || 0;
    if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value % 1_000_000 ? 1 : 0)}M`;
    if (value >= 1000) return `${Math.round(value / 1000)}K`;
    return String(value);
  }

  function providerRoute(endpoint) {
    const url = safeUrl(endpoint.provider_policy_url);
    const node = htmlElement(url ? "a" : "span", `provider-route ${tierClass(endpoint.privacy_tier)}`);
    node.textContent = `${endpoint.provider_name} · ${endpoint.usage_type} · ${tierShortLabel(endpoint.privacy_tier)}`;
    if (url) {
      node.href = url;
      node.target = "_blank";
      node.rel = "noopener";
      node.title = `${endpoint.provider_name} provider policy`;
    }
    return node;
  }

  function reasonFor(facts, ranked) {
    if (state.domain && facts.model.tags.includes(state.domain)) return `strong ${state.domain} fit`;
    if (facts.model.id === ranked[0]?.model.id) return "best match for your mix";
    if (state.preference.quality >= 0.45) return "high independent score";
    if (state.preference.cost >= 0.45) return "low-cost qualifying route";
    if (state.preference.speed >= 0.45 && facts.fastest) return "fast measured route";
    return "qualified alternative";
  }

  function renderModelCard(facts, rank, ranked) {
    const card = htmlElement("article", `model-card${rank === 0 ? " top" : ""}`);
    const rankNode = htmlElement("div", "rank", rank === 0 ? "1" : String(rank + 1));
    const body = htmlElement("div", "model-body");
    const name = htmlElement("h3", "model-name", facts.model.name);
    const qualityUrl = safeUrl(facts.model.quality.url);
    if (qualityUrl) {
      const qualityLink = htmlElement("a", "quality-link", `AI IQ ${facts.model.quality.score}`);
      qualityLink.href = qualityUrl;
      qualityLink.target = "_blank";
      qualityLink.rel = "noopener";
      qualityLink.title = `${facts.model.name} independent AI IQ profile`;
      name.appendChild(qualityLink);
    }
    name.appendChild(htmlElement("span", "reason-badge", reasonFor(facts, ranked)));
    body.append(name, htmlElement("div", "model-id", facts.model.id));

    const tags = htmlElement("div", "tags");
    for (const tag of facts.model.tags.slice(0, 5)) tags.appendChild(htmlElement("span", "tag", tag));
    if (facts.model.open_weights) tags.appendChild(htmlElement("span", "tag", "open weights"));
    body.appendChild(tags);

    const routes = htmlElement("div", "provider-routes");
    for (const endpoint of facts.endpoints) routes.appendChild(providerRoute(endpoint));
    body.appendChild(routes);
    body.appendChild(htmlElement(
      "div",
      "route-guard",
      state.privacy >= 3
        ? "Filtered to confidential + E2EE provider routes."
        : state.privacy >= 2
          ? "Filtered to zero-retention or stronger provider routes."
          : "No upstream retention floor. Check each provider route above.",
    ));

    const metrics = htmlElement("div", "model-metrics");
    metrics.appendChild(htmlElement("strong", "", `${formatDollars(facts.cheapest.completion_price_microdollars_per_million_tokens)}/M out`));
    metrics.appendChild(htmlElement("span", "", `${formatDollars(facts.cheapest.prompt_price_microdollars_per_million_tokens)}/M in via ${facts.cheapest.provider_name}`));
    metrics.appendChild(htmlElement("span", "", `${formatContext(facts.model.context_length)} context`));
    metrics.appendChild(htmlElement(
      "span",
      "",
      facts.fastest
        ? `${facts.fastest.throughput.toFixed(1)} tok/s · ${Math.round(facts.fastest.ttft)} ms TTFT via ${facts.fastest.endpoint.provider_name}`
        : "No recent route speed sample",
    ));
    metrics.appendChild(htmlElement("span", `privacy-badge ${tierClass(state.privacy)}`, `${tierShortLabel(state.privacy)} qualified`));
    card.append(rankNode, body, metrics);
    return card;
  }

  function routeById(id) {
    return state.catalog.routes.find((route) => route.id === id) || null;
  }

  function recommendedRoutes() {
    if (state.privacy >= 3) return [routeById("trustedrouter/e2e")].filter(Boolean);
    if (state.privacy >= 2) return [routeById("trustedrouter/zdr"), routeById("trustedrouter/e2e")].filter(Boolean);
    if (state.quality === "frontier" || state.preference.quality >= 0.5) {
      return [routeById("trustedrouter/synth"), routeById("trustedrouter/auto")].filter(Boolean);
    }
    if (state.preference.cost >= 0.45) {
      return [routeById("trustedrouter/cheap"), routeById("trustedrouter/auto")].filter(Boolean);
    }
    return [routeById("trustedrouter/auto"), routeById("trustedrouter/cheap")].filter(Boolean);
  }

  function routePriceLabel(route) {
    if (route.pricing_mode === "component_usage") return "component usage";
    const promptMin = route.prompt_price_min_microdollars_per_million_tokens;
    const promptMax = route.prompt_price_max_microdollars_per_million_tokens;
    const outputMin = route.completion_price_min_microdollars_per_million_tokens;
    const outputMax = route.completion_price_max_microdollars_per_million_tokens;
    const range = (low, high) => low === high ? formatDollars(low) : `${formatDollars(low)}–${formatDollars(high)}`;
    return `${range(promptMin, promptMax)}/M in · ${range(outputMin, outputMax)}/M out`;
  }

  function renderRouteRecommendation() {
    const routes = recommendedRoutes();
    dom.routeRecommendation.replaceChildren();
    dom.routeRecommendation.appendChild(htmlElement("div", "kicker", "Routing shortcut"));
    dom.routeRecommendation.appendChild(htmlElement("h2", "", state.privacy ? "Keep the privacy floor automatic." : "Let the router choose per request."));
    dom.routeRecommendation.appendChild(htmlElement(
      "p",
      "",
      state.privacy
        ? "These aliases enforce the selected upstream privacy level in the gateway before provider authorization."
        : "These aliases optimize routes without claiming an upstream privacy guarantee. Select ZDR or TEE when that guarantee is required.",
    ));
    for (const [index, route] of routes.entries()) {
      const card = htmlElement("div", `alias-card${index === 0 ? " primary" : ""}`);
      const head = htmlElement("div", "alias-head");
      head.appendChild(htmlElement("code", "", route.id));
      head.appendChild(htmlElement("span", "alias-price", routePriceLabel(route)));
      card.append(head, htmlElement("p", "", route.description));
      dom.routeRecommendation.appendChild(card);
    }
    const actions = htmlElement("div", "route-actions");
    const consoleLink = htmlElement("a", "", "Get an API key");
    consoleLink.href = "https://trustedrouter.com/console/api-keys";
    consoleLink.target = "_blank";
    consoleLink.rel = "noopener";
    const docsLink = htmlElement("a", "", "Routing docs");
    docsLink.href = "https://trustedrouter.com/docs/agent-setup";
    docsLink.target = "_blank";
    docsLink.rel = "noopener";
    actions.append(consoleLink, docsLink);
    dom.routeRecommendation.appendChild(actions);
  }

  function renderResults(ranked) {
    dom.modelResults.replaceChildren();
    dom.resultsTitle.textContent = ranked.length
      ? `Matches · ${ranked.length} independently scored model${ranked.length === 1 ? "" : "s"}`
      : "No independently scored model meets every requirement";
    if (!ranked.length) {
      dom.modelResults.appendChild(htmlElement(
        "div",
        "empty-state",
        "Relax the quality or speed requirement, or choose a broader upstream privacy floor. No route is silently substituted.",
      ));
    } else {
      ranked.slice(0, 3).forEach((facts, index) => dom.modelResults.appendChild(renderModelCard(facts, index, ranked)));
    }
    renderRouteRecommendation();
  }

  function renderTooltip(facts, clientX, clientY) {
    dom.tooltip.replaceChildren();
    dom.tooltip.appendChild(htmlElement("h3", "", facts.model.name));
    dom.tooltip.appendChild(htmlElement("p", "", `${facts.model.id} · AI IQ ${facts.model.quality.score}`));
    dom.tooltip.appendChild(htmlElement("p", "", `${formatDollars(facts.cheapest.prompt_price_microdollars_per_million_tokens)}/M in · ${formatDollars(facts.cheapest.completion_price_microdollars_per_million_tokens)}/M out via ${facts.cheapest.provider_name}`));
    dom.tooltip.appendChild(htmlElement("p", "", facts.fastest
      ? `${facts.fastest.throughput.toFixed(1)} tok/s · ${Math.round(facts.fastest.ttft)} ms measured TTFT via ${facts.fastest.endpoint.provider_name}`
      : "No recent route speed sample"));
    const host = dom.tooltip.offsetParent || dom.triangle.parentElement;
    const bounds = host.getBoundingClientRect();
    const width = 260;
    const left = Math.max(10, Math.min(clientX - bounds.left + 14, bounds.width - width - 10));
    const top = Math.max(10, clientY - bounds.top + 14);
    dom.tooltip.style.left = `${left}px`;
    dom.tooltip.style.top = `${top}px`;
    dom.tooltip.classList.add("visible");
  }

  function hideTooltip() {
    dom.tooltip.classList.remove("visible");
  }

  function renderPrivacyNote() {
    const copy = PRIVACY_COPY[state.privacy];
    const eligibleModels = evaluatedFacts().length;
    dom.privacyNote.textContent = `${copy.title}. ${copy.body} ${eligibleModels} independently scored model${eligibleModels === 1 ? "" : "s"} currently qualify.`;
  }

  function render() {
    if (!state.catalog) return;
    const facts = scoreFacts(evaluatedFacts());
    const ranked = facts.filter(passesRequirements).sort((left, right) => right.matchScore - left.matchScore);
    drawChart(facts, ranked);
    renderResults(ranked);
    renderPrivacyNote();
    dom.qualityWeight.textContent = String(Math.round(state.preference.quality * 100));
    dom.costWeight.textContent = String(Math.round(state.preference.cost * 100));
    dom.speedWeight.textContent = String(Math.round(state.preference.speed * 100));
    postHeight();
  }

  function buildSegments(container, values, stateKey) {
    container.replaceChildren();
    for (const value of values) {
      const button = htmlElement("button", "segment-button");
      button.type = "button";
      button.setAttribute("aria-pressed", String(state[stateKey] === value.key));
      button.append(htmlElement("strong", "", value.label), htmlElement("small", "", value.detail));
      button.addEventListener("click", () => {
        state[stateKey] = value.key;
        buildSegments(container, values, stateKey);
        render();
      });
      container.appendChild(button);
    }
  }

  function keywordMatch(text, keyword) {
    return text.includes(keyword);
  }

  function classifyTask(text) {
    const lowered = text.trim().toLowerCase();
    let quality = lowered.length > 180 ? "smart" : "balanced";
    for (const level of ["frontier", "smart", "balanced"]) {
      if (TASK_KEYWORDS[level].some((keyword) => keywordMatch(lowered, keyword))) {
        quality = level;
        break;
      }
    }
    let domain = null;
    let bestMatches = 0;
    for (const [name, keywords] of Object.entries(DOMAIN_KEYWORDS)) {
      const matches = keywords.filter((keyword) => keywordMatch(lowered, keyword)).length;
      if (matches > bestMatches) {
        domain = name;
        bestMatches = matches;
      }
    }
    let speed = "seconds";
    for (const key of ["realtime", "any", "minutes"]) {
      if (SPEED_KEYWORDS[key].some((keyword) => keywordMatch(lowered, keyword))) {
        speed = key;
        break;
      }
    }
    return { quality, domain, speed };
  }

  function applyTask() {
    const text = dom.problem.value.trim();
    if (!text) {
      dom.rationale.textContent = "Describe a task first.";
      return;
    }
    const result = classifyTask(text);
    state.quality = result.quality;
    state.domain = result.domain;
    state.speed = result.speed;
    state.preference = result.quality === "frontier" || result.quality === "smart"
      ? { quality: 0.54, cost: 0.22, speed: 0.24 }
      : { quality: 0.3, cost: 0.4, speed: 0.3 };
    if (result.speed === "realtime") state.preference = { quality: 0.25, cost: 0.2, speed: 0.55 };
    buildSegments(dom.qualitySegments, QUALITY_LEVELS, "quality");
    buildSegments(dom.speedSegments, SPEED_LEVELS, "speed");
    dom.rationale.textContent = `${result.domain ? `${result.domain} · ` : ""}${qualityLevel().label} quality · ${speedLevel().label} latency. Drag the marker to fine-tune the mix.`;
    render();
  }

  function svgPoint(event) {
    const point = dom.triangle.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return point.matrixTransform(dom.triangle.getScreenCTM().inverse());
  }

  function updatePreferenceFromPointer(event) {
    const point = svgPoint(event);
    state.preference = pointWeights(point.x, point.y);
    render();
  }

  function adjustPreference(axis, amount) {
    const next = { ...state.preference };
    if (axis === "balanced") {
      state.preference = { quality: 1 / 3, cost: 1 / 3, speed: 1 / 3 };
      render();
      return;
    }
    next[axis] = Math.min(1, next[axis] + amount);
    const others = Object.keys(next).filter((key) => key !== axis);
    const remaining = 1 - next[axis];
    const existing = next[others[0]] + next[others[1]];
    if (existing <= 0) {
      next[others[0]] = remaining / 2;
      next[others[1]] = remaining / 2;
    } else {
      next[others[0]] = remaining * next[others[0]] / existing;
      next[others[1]] = remaining * next[others[1]] / existing;
    }
    state.preference = next;
    render();
  }

  function wireChart() {
    dom.triangle.addEventListener("pointerdown", (event) => {
      const modelNode = event.target.closest?.("[data-model-id]");
      if (modelNode) {
        state.selected = modelNode.dataset.modelId;
        render();
        return;
      }
      state.dragging = true;
      dom.triangle.setPointerCapture?.(event.pointerId);
      updatePreferenceFromPointer(event);
    });
    dom.triangle.addEventListener("pointermove", (event) => {
      if (state.dragging) {
        updatePreferenceFromPointer(event);
        return;
      }
      const modelNode = event.target.closest?.("[data-model-id]");
      if (!modelNode || !state.catalog) {
        hideTooltip();
        return;
      }
      const facts = modelFacts(state.catalog.models.find((model) => model.id === modelNode.dataset.modelId));
      if (facts) renderTooltip(facts, event.clientX, event.clientY);
    });
    dom.triangle.addEventListener("pointerup", () => { state.dragging = false; });
    dom.triangle.addEventListener("pointercancel", () => { state.dragging = false; });
    dom.triangle.addEventListener("pointerleave", () => { if (!state.dragging) hideTooltip(); });
    dom.triangle.addEventListener("keydown", (event) => {
      const actions = {
        ArrowUp: ["quality", 0.08],
        ArrowLeft: ["cost", 0.08],
        ArrowRight: ["speed", 0.08],
        ArrowDown: ["balanced", 0],
      };
      if (!actions[event.key]) return;
      event.preventDefault();
      adjustPreference(...actions[event.key]);
    });
  }

  function renderExamples() {
    for (const example of EXAMPLES) {
      const button = htmlElement("button", "example-button", example);
      button.type = "button";
      button.addEventListener("click", () => {
        dom.problem.value = example;
        applyTask();
      });
      dom.examples.appendChild(button);
    }
  }

  function setLoadState(message, error = false) {
    dom.loadText.textContent = message;
    dom.loadState.classList.toggle("error", error);
    dom.retryCatalog.hidden = !error;
  }

  async function loadCatalog() {
    setLoadState("Loading verified route data...");
    try {
      const response = await fetch(CATALOG_URL, { headers: { accept: "application/json" } });
      if (!response.ok) throw new Error(`Catalog request failed with HTTP ${response.status}.`);
      state.catalog = normalizeCatalog(await response.json());
      dom.liveCount.textContent = String(state.catalog.catalog_model_count);
      dom.evaluatedCount.textContent = String(state.catalog.evaluated_model_count);
      setLoadState(`${state.catalog.evaluated_model_count} independently scored models matched against ${state.catalog.catalog_route_count} live provider routes.`);
      render();
    } catch (error) {
      state.catalog = null;
      dom.modelResults.replaceChildren(htmlElement("div", "empty-state", "Recommendations are unavailable until verified route data loads."));
      dom.routeRecommendation.replaceChildren();
      setLoadState(error instanceof Error ? error.message : "The verified route catalog is unavailable.", true);
      postHeight();
    }
  }

  function postHeight() {
    if (window.self === window.top) return;
    try {
      window.parent.postMessage({ type: "tr-choose-height", height: Math.ceil(document.body.scrollHeight) }, "*");
    } catch (_error) {
      // Height reporting is best-effort and contains no catalog or user data.
    }
  }

  function cacheDom() {
    for (const id of [
      "triangle", "tooltip", "privacy", "privacyNote", "problem", "examples", "guess", "rationale",
      "qualitySegments", "speedSegments", "qualityWeight", "costWeight", "speedWeight", "loadState",
      "loadText", "resultsTitle", "modelResults", "routeRecommendation", "liveCount",
      "evaluatedCount",
    ]) dom[id] = byId(id);
    dom.retryCatalog = byId("retry-catalog");
  }

  function boot() {
    cacheDom();
    if (window.self !== window.top) document.documentElement.classList.add("embedded");
    drawScaffold();
    wireChart();
    renderExamples();
    buildSegments(dom.qualitySegments, QUALITY_LEVELS, "quality");
    buildSegments(dom.speedSegments, SPEED_LEVELS, "speed");
    dom.privacy.addEventListener("change", () => {
      state.privacy = Number(dom.privacy.value);
      state.selected = null;
      render();
    });
    dom.guess.addEventListener("click", applyTask);
    dom.problem.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") applyTask();
    });
    dom.retryCatalog.addEventListener("click", loadCatalog);
    if (window.ResizeObserver) new ResizeObserver(postHeight).observe(document.body);
    window.addEventListener("resize", postHeight);
    loadCatalog();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
