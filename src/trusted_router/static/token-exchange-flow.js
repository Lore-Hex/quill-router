(function () {
  "use strict";
  const examples = {
    strategic: ["Account summary for a regulated client", "Product search ranking", "Claims triage note", "Customer support decision"],
    eligible: ["Campaign email draft", "Webinar recap", "Social post variants", "SEO meta descriptions"],
  };

  function exampleFor(state, index) {
    const eligible = Math.floor((index + 1) * state.share / 100) > Math.floor(index * state.share / 100);
    const kind = eligible ? "eligible" : "strategic";
    return { kind, name: examples[kind][index % examples[kind].length], exchange: eligible && state.enabled };
  }

  function point(curve, t) {
    const u = 1 - t;
    return [0, 1].map(axis => u ** 3 * curve[0][axis] + 3 * u * u * t * curve[1][axis] + 3 * u * t * t * curve[2][axis] + t ** 3 * curve[3][axis]);
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { exampleFor, point };
  if (typeof document === "undefined") return;
  const root = document.getElementById("tx-calculator");
  if (!root) return;
  const el = name => document.getElementById(`tx-${name}`);
  const stage = el("flow");
  const canvas = el("flow-canvas");
  const ctx = canvas.getContext("2d");
  const log = el("request-log");
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let state, curves = {}, colors = {}, width = 0, height = 0;
  let packets = [], frame = 0, last = 0, elapsed = 0, spawnAt = 0, sequence = 0;
  let paused = false, visible = true;

  function appendRequest(request, animate = false) {
    const row = document.createElement("li");
    row.dataset.route = request.exchange ? "exchange" : "current";
    row.dataset.kind = request.kind;
    for (const [tag, text, className] of [
      ["span", request.name, ""],
      ["span", request.kind === "eligible" ? "Eligible" : "Strategic", "tx-request-type"],
      ["strong", request.exchange ? "Token Exchange" : "Google Cloud", ""],
    ]) {
      const cell = document.createElement(tag);
      cell.textContent = text;
      cell.className = className;
      row.append(cell);
    }
    if (animate) row.className = "tx-request-arrived";
    log.prepend(row);
    while (log.children.length > 5) log.lastElementChild.remove();
  }

  function measure() {
    const bounds = stage.getBoundingClientRect();
    width = bounds.width;
    height = bounds.height;
    if (!width || !height || !ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const vertical = window.matchMedia("(max-width: 640px)").matches;
    const boxes = {};
    for (const id of ["strategic-node", "eligible-node", "gateway", "current-route", "exchange-route"]) {
      const box = el(id).getBoundingClientRect();
      boxes[id] = { left: box.left - bounds.left, right: box.right - bounds.left, top: box.top - bounds.top, bottom: box.bottom - bounds.top, x: box.left - bounds.left + box.width / 2, y: box.top - bounds.top + box.height / 2 };
    }
    const connect = (from, to, port) => {
      const a = boxes[from], b = boxes[to];
      const start = vertical ? [a.x + (from === "gateway" ? port : 0), a.bottom] : [a.right, a.y + (from === "gateway" ? port : 0)];
      const end = vertical ? [b.x + (to === "gateway" ? port : 0), b.top] : [b.left, b.y + (to === "gateway" ? port : 0)];
      return vertical
        ? [start, [start[0], (start[1] + end[1]) / 2], [end[0], (start[1] + end[1]) / 2], end]
        : [start, [(start[0] + end[0]) / 2, start[1]], [(start[0] + end[0]) / 2, end[1]], end];
    };
    curves = {
      strategic: connect("strategic-node", "gateway", -18),
      eligible: connect("eligible-node", "gateway", 18),
      current: connect("gateway", "current-route", -18),
      rerouted: connect("gateway", "current-route", 18),
      exchange: connect("gateway", "exchange-route", 18),
    };
    const style = getComputedStyle(stage);
    colors = { strategic: style.getPropertyValue("--tx-gold").trim(), eligible: style.getPropertyValue("--tx-green").trim() };
    draw();
  }

  function draw() {
    if (!ctx || !state || !curves.strategic) return;
    ctx.clearRect(0, 0, width, height);
    for (const [name, curve] of Object.entries(curves)) {
      const eligible = ["eligible", "exchange", "rerouted"].includes(name);
      const active = name === "exchange" ? state.enabled && state.share > 0
        : name === "rerouted" ? !state.enabled && state.share > 0
        : eligible ? state.share > 0 : state.share < 100;
      ctx.globalAlpha = active ? 0.45 : 0.12;
      ctx.strokeStyle = colors[eligible ? "eligible" : "strategic"];
      ctx.lineWidth = 1.5;
      ctx.setLineDash(active ? [] : [4, 5]);
      ctx.beginPath();
      ctx.moveTo(...curve[0]);
      ctx.bezierCurveTo(...curve[1], ...curve[2], ...curve[3]);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    for (const packet of packets) {
      const progress = (elapsed - packet.start) / 2400;
      // The gap between segments is the gateway: packets never draw over its text.
      const curve = progress < 0.5 ? curves[packet.kind] : curves[packet.exchange ? "exchange" : packet.kind === "eligible" ? "rerouted" : "current"];
      const [x, y] = point(curve, progress < 0.5 ? progress * 2 : (progress - 0.5) * 2);
      ctx.fillStyle = colors[packet.kind];
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function tick(now) {
    frame = 0;
    if (last && now - last < 1000 / 30) { frame = requestAnimationFrame(tick); return; }
    elapsed += last ? Math.min(now - last, 100) : 0;
    last = now;
    if (elapsed >= spawnAt && packets.length < 12) {
      packets.push({ ...exampleFor(state, sequence), start: elapsed });
      sequence = (sequence + 1) % 100;
      spawnAt = elapsed + 420;
    }
    packets = packets.filter(packet => {
      if (elapsed - packet.start < 2400) return true;
      appendRequest(packet, true);
      return false;
    });
    draw();
    frame = requestAnimationFrame(tick);
  }

  function syncMotion() {
    cancelAnimationFrame(frame);
    frame = 0;
    last = 0;
    const reduced = motion.matches;
    const running = Boolean(ctx && state && state.spend > 0 && !paused && !reduced && visible && !document.hidden);
    root.dataset.animation = running ? "running" : "paused";
    const label = reduced ? "Reduced motion" : paused ? "Resume animation" : "Pause animation";
    el("motion-label").textContent = label;
    el("motion").title = label;
    el("motion").disabled = reduced || !ctx;
    el("motion").dataset.paused = String(paused || reduced);
    if (running) frame = requestAnimationFrame(tick);
    draw();
  }

  root.addEventListener("tx:change", event => {
    state = event.detail;
    // Drop in-flight examples and re-seed on every routing change, never show an old destination.
    packets = [];
    elapsed = 0;
    spawnAt = 0;
    sequence = 5;
    log.replaceChildren();
    for (let i = 0; i < 5; i++) appendRequest(exampleFor(state, i));
    measure();
    syncMotion();
  });
  el("motion").addEventListener("click", () => { paused = !paused; syncMotion(); });
  motion.addEventListener("change", syncMotion);
  document.addEventListener("visibilitychange", syncMotion);
  window.addEventListener("pagehide", () => { visible = false; syncMotion(); });
  window.addEventListener("pageshow", () => { visible = true; syncMotion(); });
  new ResizeObserver(measure).observe(stage);
  new MutationObserver(measure).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const visibleParts = new Set();
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting) visibleParts.add(entry.target);
      else visibleParts.delete(entry.target);
    }
    visible = visibleParts.size > 0;
    syncMotion();
  });
  observer.observe(stage);
  observer.observe(log);
})();
