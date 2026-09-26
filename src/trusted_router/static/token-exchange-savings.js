(function () {
  "use strict";
  const defaults = Object.freeze({ spend: 3350000, share: 40, discount: 80, enabled: true });
  const limits = { spend: 100000000, share: 100, discount: 95 };

  function validNumber(value, maximum, integer = false) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= maximum && (!integer || Number.isInteger(value));
  }

  function calculate(state) {
    for (const key of Object.keys(limits)) {
      if (!validNumber(state[key], limits[key], key !== "spend")) throw new RangeError(`Invalid ${key}`);
    }
    if (typeof state.enabled !== "boolean") throw new TypeError("Invalid enabled state");
    // Integer cents keep the visible line items and the total consistent.
    const baseline = Math.round(state.spend * 100);
    const eligible = state.enabled ? Math.round(baseline * state.share / 100) : 0;
    const kept = baseline - eligible;
    const provider = Math.round(eligible * (100 - state.discount) / 100);
    const fee = Math.round(provider * 55 / 1000);
    const total = kept + provider + fee;
    const saved = baseline - total;
    return { baseline, eligible, kept, provider, fee, total, saved, annual: saved * 12, percent: baseline ? saved / baseline * 100 : 0 };
  }

  function encode(state) {
    calculate(state);
    return new URLSearchParams({ spend: state.spend.toFixed(2), share: state.share, discount: state.discount, enabled: state.enabled ? "1" : "0" }).toString();
  }

  function decode(hash) {
    const params = new URLSearchParams(hash.replace(/^#/, ""));
    const state = { ...defaults };
    for (const key of Object.keys(limits)) {
      const raw = params.get(key);
      if (raw !== null && /^\d+(?:\.\d{1,2})?$/.test(raw)) {
        const value = Number(raw);
        if (validNumber(value, limits[key], key !== "spend")) state[key] = value;
      }
    }
    if (params.get("enabled") === "0") state.enabled = false;
    return state;
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { calculate, defaults, encode, decode };
  if (typeof document === "undefined") return;
  const root = document.getElementById("tx-calculator");
  if (!root) return;
  const el = name => document.getElementById(`tx-${name}`);
  const money = cents => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: cents % 100 ? 2 : 0, maximumFractionDigits: 2 }).format(cents / 100);
  let state = decode(window.location.hash);
  const setText = (name, value) => { el(name).textContent = value; };

  function render() {
    const cost = calculate(state);
    const share = state.enabled ? state.share : 0;
    root.dataset.enabled = String(state.enabled);
    root.dataset.increase = String(cost.saved < 0);
    for (const name of ["baseline", "kept", "provider", "fee", "total"]) setText(name, money(cost[name]));
    setText("saved", money(Math.abs(cost.saved)));
    setText("annual", money(Math.abs(cost.annual)));
    setText("savings-label", cost.saved < 0 ? "Estimated monthly increase" : "Estimated monthly savings");
    setText("annual-label", cost.saved < 0 ? "Annualized increase" : "Annualized savings");
    setText("percent", `${Math.abs(cost.percent).toFixed(1)}% ${cost.saved < 0 ? "more" : "less"} on tokens`);
    setText("share-label", `${state.share}%`);
    setText("discount-label", `${state.discount}%`);
    setText("premium-share", `${100 - share}% stays`);
    setText("exchange-share", share ? `${share}% moves` : "No spend routed");
    setText("toggle-label", state.enabled ? "On" : "Off");
    setText("policy", state.enabled ? "Your models. Your requirements." : "All work stays with your provider.");
    setText("comparison-label", state.enabled ? "With Token Exchange" : "Exchange off");
    // Use the larger total as the scale, including scenarios where fees increase cost.
    const scale = Math.max(cost.baseline, cost.total, 1);
    document.querySelector(".tx-full").style.width = `${cost.baseline / scale * 100}%`;
    el("kept-bar").style.width = `${cost.kept / scale * 100}%`;
    el("moved-bar").style.width = `${(cost.provider + cost.fee) / scale * 100}%`;
    for (const row of root.querySelectorAll("[data-tx-example]")) row.textContent = state.enabled && share > 0 ? "Token Exchange" : "Google Cloud";
    el("exchange-route").dataset.active = String(share > 0);
  }

  function applyState() {
    for (const key of Object.keys(limits)) el(key).value = String(state[key]);
    el("enabled").checked = state.enabled;
    el("spend").removeAttribute("aria-invalid");
    setText("validation", "");
    render();
  }

  function update() {
    const spend = el("spend").valueAsNumber;
    const valid = validNumber(spend, limits.spend) && el("spend").validity.valid;
    el("spend").setAttribute("aria-invalid", String(!valid));
    el("copy").disabled = !valid;
    setText("validation", valid ? "" : "Enter a monthly spend from $0 to $100,000,000, with at most two decimal places. The last valid estimate is shown.");
    setText("copy-status", "");
    el("copy-fallback").hidden = true;
    if (!valid) return;
    state = { spend, share: Number(el("share").value), discount: Number(el("discount").value), enabled: el("enabled").checked };
    render();
  }
  for (const key of [...Object.keys(limits), "enabled"]) el(key).addEventListener("input", update);
  el("reset").addEventListener("click", () => {
    state = { ...defaults };
    applyState();
    update();
    window.history.replaceState(null, "", window.location.pathname);
  });
  window.addEventListener("hashchange", () => { state = decode(window.location.hash); applyState(); update(); });
  el("copy").addEventListener("click", async () => {
    const url = new URL(window.location.pathname, window.location.origin);
    url.hash = encode(state);
    try {
      await navigator.clipboard.writeText(url.href);
      setText("copy-status", "Copied. The link includes your spend assumptions.");
    } catch {
      el("copy-fallback").value = url.href;
      el("copy-fallback").hidden = false;
      el("copy-fallback").focus();
      el("copy-fallback").select();
      setText("copy-status", "Copy this link to share your assumptions.");
    }
  });
  applyState();
  root.hidden = false;
})();
