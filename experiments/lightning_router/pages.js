import { createIcons, ArrowRight, RefreshCw } from "lucide";
import { KEY, savedSession } from "./session.mjs";

const $ = id => document.getElementById(id);
createIcons({ icons: { ArrowRight, RefreshCw } });
for (const link of document.querySelectorAll('nav[aria-label="Main"] a')) {
  if (link.pathname === location.pathname) link.setAttribute("aria-current", "page");
}
function dollars(value) {
  if (typeof value !== "string" || !/^\d+(\.\d+)?$/.test(value)) return "Unavailable";
  const [whole, fraction = ""] = value.split(".");
  return "$" + whole + "." + fraction.replace(/0+$/, "").padEnd(2, "0");
}
async function read(path, key) {
  const response = await fetch(path, {headers: key ? {Authorization: "Bearer " + key} : {},
    cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(20000)});
  if (!response.ok) throw new Error(response.status === 401 ? "That key was not found or is no longer active." : "Temporarily unavailable. Please retry.");
  return response.json();
}

if ($("usage-key-form")) {
  const saved = savedSession();
  let currentKey = saved && (!saved.isNew || saved.reveal) ? saved.key : null;
  let busy = false;
  async function refresh(key) {
    if (busy) return;
    if (!KEY.test(key || "")) { $("page-error").textContent = "Enter a funded TrustedRouter API key."; return; }
    busy = true;
    $("page-error").textContent = "";
    $("usage-data").hidden = true;
    $("usage-status").textContent = "Checking balance and usage";
    for (const button of document.querySelectorAll("button")) button.disabled = true;
    try {
      const balance = await read("/api/account", key);
      currentKey = key;
      $("usage-key").value = "";
      $("usage-key").placeholder = "Key connected";
      $("usage-balance").textContent = dollars(balance.balance_usd);
      // A usage outage must not hide a successfully read credit balance.
      for (const field of ["total", "reserved", "byok", "limit", "remaining"]) $("usage-" + field).textContent = "Unavailable";
      $("usage-data").hidden = false;
      const usage = await read("/api/usage", key);
      $("usage-total").textContent = dollars(usage.usage_usd);
      $("usage-reserved").textContent = dollars(usage.reserved_usd);
      $("usage-byok").textContent = dollars(usage.byok_usage_usd);
      $("usage-limit").textContent = usage.limit_usd === null ? "No key limit" : dollars(usage.limit_usd);
      $("usage-remaining").textContent = usage.limit_usd === null ? "Subject to available credits" : dollars(usage.limit_remaining_usd);
      $("usage-status").textContent = "Updated " + new Date().toLocaleTimeString();
    } catch (error) {
      $("page-error").textContent = error.message;
      $("usage-status").textContent = "Could not finish the refresh";
    } finally { busy = false; for (const button of document.querySelectorAll("button")) button.disabled = false; }
  }
  $("usage-key-form").addEventListener("submit", event => {
    event.preventDefault(); refresh($("usage-key").value.trim() || currentKey);
  });
  $("refresh-usage").addEventListener("click", () => refresh(currentKey));
  if (currentKey) refresh(currentKey);
}

if ($("price-search")) {
  let models = [];
  let shown = 100;
  function render() {
    const query = $("price-search").value.trim().toLowerCase();
    const matches = models.filter(model => (model.id + " " + model.name).toLowerCase().includes(query));
    const rows = matches.slice(0, shown).map(model => {
      const row = document.createElement("tr");
      const name = document.createElement("th"); name.scope = "row";
      const link = document.createElement("a"); link.href = "/?model=" + encodeURIComponent(model.id) + "#setup-title"; link.textContent = model.name;
      const id = document.createElement("small"); id.textContent = model.id;
      name.append(link, id);
      if (model.output) { const limit = document.createElement("small"); limit.textContent = model.output.toLocaleString() + " max output tokens"; name.append(limit); }
      row.append(name);
      for (const field of ["input_per_million", "output_per_million", "cached_input_per_million"]) {
        const cell = document.createElement("td"); cell.textContent = dollars(model.pricing?.[field]); row.append(cell);
      }
      const fees = [["request_usd", "per request"], ["minimum_charge_usd", "minimum charge"]]
        .filter(([field]) => model.pricing?.[field] && /[1-9]/.test(model.pricing[field]));
      for (const [field, label] of fees) { const fee = document.createElement("small"); fee.textContent = dollars(model.pricing[field]) + " " + label; name.append(fee); }
      return row;
    });
    $("price-rows").replaceChildren(...rows);
    $("price-status").textContent = matches.length ? `${rows.length.toLocaleString()} of ${matches.length.toLocaleString()} models` : "No matching models";
    $("more-prices").hidden = rows.length >= matches.length;
  }
  $("price-search").addEventListener("input", () => { shown = 100; render(); });
  $("more-prices").addEventListener("click", () => { shown += 100; render(); });
  read("/api/models").then(result => { models = result.data; render(); }).catch(() => {
    $("price-status").textContent = "Prices unavailable";
    $("page-error").textContent = "The model catalog could not be loaded. Please refresh to retry.";
  });
}
