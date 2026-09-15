import { createIcons, ArrowRight, Copy, Eye, LogOut, RefreshCw } from "lucide";
import { centsFromText, providerOrderFromText, setupFor } from "./setup.mjs";
import { KEY, SESSION, savedSession } from "./session.mjs";

const $ = (id) => document.getElementById(id);
const icons = () => createIcons({ icons: { ArrowRight, Copy, Eye, LogOut, RefreshCw } });
let state = null;
let config = null;
let models = [];
let agent = "cowork";
let busy = false;
let pollTimer;
let quoteTimer;
let quoteVersion = 0;
let toastTimer;
let setup = null;
let amountDirty = false;
const providerOrders = new Map();

function remember() { sessionStorage.setItem(SESSION, JSON.stringify(state)); }
function secret() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return "sk-tr-v1-" + btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}
function requestId() { return crypto.randomUUID().replaceAll("-", ""); }
function message(text = "") { $("error").textContent = text; }
async function api(path, { key = state?.key, body, idempotency } = {}) {
  const headers = {};
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotency) headers["Idempotency-Key"] = idempotency;
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST", headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(15000),
  });
  const result = await response.json();
  if (!response.ok) {
    const errors = {
      invalid_api_key: "That TrustedRouter API key was not found.",
      invoice_conflict: "An invoice is already open for this key. Resume it before creating another.",
      rate_limited: "Too many invoice requests. Please wait before trying again.",
      payments_not_ready: "Lightning payments are not ready yet.",
    };
    throw new Error(errors[result.error] || "The request could not finish. Your existing invoice and key are preserved; please retry.");
  }
  return result;
}
async function exclusive(action) {
  if (busy) return;
  busy = true;
  clearTimeout(pollTimer);
  message();
  for (const id of ["use-key", "update-invoice", "new-invoice", "sign-out", "amount", "existing-key"]) $(id).disabled = true;
  try { await action(); } catch (error) { message(error.message || "Request failed. Please retry."); }
  finally {
    busy = false;
    $("use-key").disabled = false;
    $("sign-out").disabled = false;
    $("update-invoice").disabled = !config?.payments_ready;
    $("amount").disabled = false;
    $("existing-key").disabled = false;
    renderInvoiceRecovery();
    schedulePoll();
  }
}
function renderSetup() {
  const cowork = agent === "cowork";
  $("cowork-setup").hidden = !cowork;
  $("cli-setup").hidden = cowork;
  $("agent-model-settings").hidden = cowork;
  $("setup-panel").setAttribute("aria-labelledby", "tab-" + agent);
  $("copy-cowork-key").disabled = !state?.reveal;
  $("cowork-key-status").hidden = Boolean(state?.reveal);
  $("env-code").textContent = "export LIGHTNINGROUTER_API_KEY='" + (state?.reveal ? state.key : "YOUR_API_KEY") + "'";
  if (cowork) { setup = null; return; }
  const model = models.find((item) => item.id === $("model").value);
  if (!model || !config) return;
  try {
    setup = setupFor(agent, model, config.api_base, providerOrderFromText($("provider-order").value));
    $("provider-order-error").textContent = "";
    $("provider-order").setAttribute("aria-invalid", "false");
    $("copy-config").disabled = false;
    $("copy-command").disabled = false;
  } catch (error) {
    setup = null;
    $("provider-order-error").textContent = error.message;
    $("provider-order").setAttribute("aria-invalid", "true");
    $("config-code").textContent = "";
    $("command-code").textContent = "";
    $("copy-config").disabled = true;
    $("copy-command").disabled = true;
    return;
  }
  const reasoning = model.reasoning;
  $("reasoning-values").textContent = reasoning?.field
    ? `${reasoning.field}${reasoning.values.length ? ": " + reasoning.values.join(", ") : ""}`
    : "Not yet verified";
  $("reasoning-default").textContent = setup.reasoningSummary;
  $("reasoning-note").textContent = reasoning?.note || "No model-specific effort override is generated.";
  const reference = $("reasoning-source");
  reference.hidden = !reasoning?.source;
  if (reasoning?.source) reference.href = reasoning.source;
  else reference.removeAttribute("href");
  $("config-path").textContent = setup.path;
  $("config-code").textContent = setup.config;
  $("command-code").textContent = setup.command;
  $("agent-docs").href = setup.docs;
  $("model-limits").textContent = [model.context ? `${model.context.toLocaleString()} context` : "Context not published",
    model.output ? `${model.output.toLocaleString()} max output` : "Output limit not published",
    model.default_output ? `${model.default_output.toLocaleString()} default output budget` : ""].filter(Boolean).join(" · ");
}
function renderKey() {
  const connected = Boolean(state?.reveal);
  $("existing-key").readOnly = connected;
  if (connected) $("existing-key").value = state.key;
  $("key-label").textContent = connected ? "Your API key" : "Existing API key (optional)";
  $("use-key").hidden = connected;
  for (const id of ["show-key", "copy-key", "key-reveal"]) $(id).hidden = !connected;
  $("account").hidden = !connected;
}
function renderAccount(balance) {
  $("account").hidden = false;
  $("balance-usd").textContent = `$${balance.balance_usd} USD`;
  renderKey();
  renderSetup();
}
async function showBalance() {
  const balance = await api("/api/account");
  renderAccount(balance);
  return balance;
}
function showFxTerms(quote) {
  const percent = quote.fx_margin_bps / 100;
  $("fx-terms").textContent = percent
    ? `${percent}% FX buffer included. Approx. $${quote.invoice_spot_usd} BTC value at the quoted Coinbase rate; ${100 - percent}% becomes USD credits.`
    : "No FX buffer on this invoice. Payment converts at its original quoted rate.";
}
async function showInvoice(invoice) {
  state.invoice = invoice;
  remember();
  const payable = invoice.state === "OPEN" && !invoice.expired && !amountDirty && !invoice.attention_required;
  $("qr").hidden = !payable;
  $("qr-empty").hidden = payable;
  $("invoice-actions").hidden = !payable;
  if (payable) {
    $("qr").src = invoice.qr;
    $("wallet-link").href = "lightning:" + invoice.bolt11;
  } else {
    $("qr").removeAttribute("src");
    $("wallet-link").removeAttribute("href");
  }
  if (!amountDirty) {
    $("btc-amount").textContent = `$${invoice.usd_amount} USD credits · ${invoice.invoice_btc} BTC invoice`;
    showFxTerms(invoice);
  }
  const labels = { OPEN: invoice.expired ? "Invoice expired. Update to create a new one." : "Waiting for payment", ACCEPTED: "Payment in flight. Waiting for settlement.", SETTLED: invoice.credited ? `Added $${invoice.credit_usd} in USD credits` : "Payment received. USD credit is pending.", CANCELED: "Invoice canceled" };
  $("invoice-state").textContent = labels[invoice.state];
  $("qr-message").textContent = invoice.state === "SETTLED" ? "Payment received" : labels[invoice.state];
  if (invoice.attention_required) {
    $("invoice-state").textContent = "Checkout needs review. Keep this tab open and contact support@trustedrouter.com.";
    $("qr-message").textContent = "Review required";
  }
  if (amountDirty && invoice.state === "OPEN") $("qr-message").textContent = "Update the invoice to use the new amount";
  if (invoice.credited) {
    state.reveal = true;
    remember();
  }
  renderKey();
  renderInvoiceRecovery();
  renderSetup();
  if (!state.isNew || state.reveal) await showBalance();
  else $("account").hidden = true;
}
function renderInvoiceRecovery() {
  const invoice = state?.invoice;
  const replaceable = invoice && !invoice.attention_required &&
    (invoice.state === "CANCELED" || (invoice.state === "OPEN" && invoice.expired));
  $("new-invoice").hidden = !replaceable;
  $("new-invoice").disabled = busy || !config?.payments_ready;
}
async function createInvoice() {
  const cents = centsFromText($("amount").value);
  if (!state) {
    state = { key: secret(), isNew: true, reveal: false, saved: false, invoice: null, requestId: requestId(), cents };
    // Refuse to request a payable invoice unless the recovery key was saved in
    // this tab FIRST. A timed-out response must not strand a paid invoice.
    remember();
  }
  const invoice = await api("/api/invoices", {
    body: { new_account: state.isNew, usd_cents: state.cents }, idempotency: state.requestId,
  });
  quoteVersion += 1;
  amountDirty = false;
  $("amount").value = invoice.usd_amount;
  await showInvoice(invoice);
}
async function finishOrCancel() {
  if (!state) return true;
  if (!state.invoice) await createInvoice(); // recover an ambiguous creation
  const before = state.invoice;
  if (["OPEN", "ACCEPTED"].includes(before.state)) {
    let invoice;
    try {
      invoice = await api(`/api/invoices/${before.id}/cancel`, { body: {} });
    } catch (error) {
      // The server may have committed before the response was lost. Preserve
      // this key and request ID unless a fresh lookup proves a terminal state.
      invoice = await api(`/api/invoices/${before.id}/refresh`, { body: {} });
      if (!["CANCELED", "SETTLED"].includes(invoice.state)) {
        await showInvoice(invoice);
        throw error;
      }
    }
    await showInvoice(invoice);
  }
  if (state.invoice.state === "SETTLED" && !state.invoice.credited) {
    message("Payment received. Wait for USD credits before leaving this checkout.");
    return false;
  }
  if (state.invoice.state === "SETTLED" && state.isNew && !state.saved) {
    message("Payment finished for this key. Copy your new API key before switching accounts or signing out.");
    return false;
  }
  return ["SETTLED", "CANCELED"].includes(state.invoice.state);
}
function schedulePoll() {
  if (!state?.invoice || document.hidden) return;
  if (!["OPEN", "ACCEPTED"].includes(state.invoice.state) && !(state.invoice.state === "SETTLED" && !state.invoice.credited)) return;
  pollTimer = setTimeout(() => exclusive(async () => {
    await showInvoice(await api(`/api/invoices/${state.invoice.id}/refresh`, { body: {} }));
  }), 5000);
}
async function quote() {
  const version = ++quoteVersion;
  try {
    const cents = centsFromText($("amount").value);
    const result = await api(`/api/quote?usd_cents=${cents}`, { key: null });
    if (version === quoteVersion) {
      $("btc-amount").textContent = `Approximately ${result.btc} BTC`;
      showFxTerms(result);
    }
  } catch {
    if (version === quoteVersion) {
      $("btc-amount").textContent = "BTC estimate unavailable";
      $("fx-terms").textContent = "";
    }
  }
}
async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 1800);
    return true;
  } catch { message("Clipboard access was blocked. Select and copy the text instead."); return false; }
}

$("key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  exclusive(async () => {
    if (state?.reveal) return;
    const key = $("existing-key").value.trim();
    if (!KEY.test(key)) throw new Error("Enter an existing TrustedRouter API key beginning sk-tr-v1-.");
    const balance = await api("/api/account", { key });
    if (state?.key === key) { state.reveal = true; remember(); renderAccount(balance); return; }
    if (!await finishOrCancel()) return;
    state = { key, isNew: false, reveal: true, saved: true, invoice: null, requestId: requestId(), cents: centsFromText($("amount").value) };
    remember();
    $("existing-key").type = "password";
    renderAccount(balance);
    if (balance.active_invoice) {
      const invoice = await api(`/api/invoices/${balance.active_invoice}/refresh`, { body: {} });
      state.cents = centsFromText(invoice.usd_amount);
      $("amount").value = invoice.usd_amount;
      amountDirty = false;
      await showInvoice(invoice);
    } else await createInvoice();
  });
});
$("amount-form").addEventListener("submit", (event) => {
  event.preventDefault();
  exclusive(async () => {
    const cents = centsFromText($("amount").value);
    if (!await finishOrCancel()) return;
    if (state) { state.requestId = requestId(); state.invoice = null; state.cents = cents; remember(); }
    await createInvoice();
  });
});
$("amount").addEventListener("input", () => {
  amountDirty = true;
  $("qr").hidden = true;
  $("invoice-actions").hidden = true;
  $("qr-empty").hidden = false;
  $("qr-message").textContent = config?.payments_ready ? "Update the invoice to use the new amount" : "Lightning payments are not live yet";
  clearTimeout(quoteTimer); quoteTimer = setTimeout(quote, 250);
});
$("copy-invoice").addEventListener("click", () => { if (state?.invoice) copy(state.invoice.bolt11); });
async function copyFundedKey() {
  const key = state?.reveal ? state.key : null;
  if (key && await copy(key) && state?.key === key && state.reveal) { state.saved = true; remember(); }
}
$("copy-key").addEventListener("click", copyFundedKey);
$("copy-cowork-key").addEventListener("click", copyFundedKey);
$("show-key").addEventListener("click", () => {
  if (!state?.reveal) return;
  const field = $("existing-key");
  field.type = field.type === "password" ? "text" : "password";
  const label = field.type === "password" ? "Show key" : "Hide key";
  $("show-key").setAttribute("aria-label", label);
  $("show-key").title = label;
});
$("sign-out").addEventListener("click", () => exclusive(async () => {
  if (!await finishOrCancel()) return;
  state = null;
  sessionStorage.removeItem(SESSION);
  $("existing-key").value = "";
  $("existing-key").type = "password";
  $("show-key").setAttribute("aria-label", "Show key");
  $("show-key").title = "Show key";
  $("account").hidden = true;
  $("balance-usd").textContent = "Unavailable";
  renderKey();
  renderSetup();
  await createInvoice();
}));
$("copy-env").addEventListener("click", () => copy($("env-code").textContent));
$("copy-config").addEventListener("click", () => { if (setup) copy(setup.config); });
$("copy-command").addEventListener("click", () => { if (setup) copy(setup.command); });
$("refresh-balance").addEventListener("click", () => exclusive(showBalance));
$("model").addEventListener("change", () => {
  $("provider-order").value = providerOrders.get($("model").value) || "";
  renderSetup();
});
$("provider-order").addEventListener("input", () => {
  providerOrders.set($("model").value, $("provider-order").value);
  renderSetup();
});
for (const tab of document.querySelectorAll("[role=tab]")) {
  tab.addEventListener("click", () => {
    agent = tab.dataset.agent;
    for (const other of document.querySelectorAll("[role=tab]")) {
      other.setAttribute("aria-selected", String(other === tab)); other.tabIndex = other === tab ? 0 : -1;
    }
    renderSetup();
  });
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll("[role=tab]")];
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
      : (tabs.indexOf(tab) + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
    tabs[next].click(); tabs[next].focus();
  });
}
document.addEventListener("visibilitychange", () => { clearTimeout(pollTimer); if (!document.hidden) schedulePoll(); });

async function start() {
  icons();
  try {
    config = await api("/api/config", { key: null });
    $("connection").hidden = config.network !== "regtest" && config.payments_ready;
    $("connection").textContent = config.network === "regtest" ? "Test network" : "Payments not live yet";
    $("use-key").disabled = false;
    $("update-invoice").disabled = !config.payments_ready;
    $("api-readiness").hidden = config.inference_configured;
    const saved = savedSession();
    if (saved) {
      state = saved;
      renderKey();
      $("amount").value = `${Math.floor(state.cents / 100)}.${String(state.cents % 100).padStart(2, "0")}`;
      if (!state.isNew || state.reveal) await showBalance();
    }
    if (config.payments_ready) await exclusive(async () => {
      if (state?.invoice) await showInvoice(await api(`/api/invoices/${state.invoice.id}/refresh`, { body: {} }));
      else await createInvoice();
    });
    else {
      $("qr-message").textContent = "Lightning payments are not live yet";
      $("invoice-state").textContent = "No payments can be accepted yet.";
      await quote();
    }
  } catch (error) { message(error.message); $("qr-message").textContent = "Payments temporarily unavailable"; }
  try {
    const result = await api("/api/models", { key: null });
    models = result.data;
    $("model").replaceChildren(...models.map((model) => new Option(model.name, model.id)));
    const requested = new URLSearchParams(location.search).get("model");
    const preferred = models.find(model => model.id === requested)
      || models.find(model => model.id === "deepseek/deepseek-v4.1-flash")
      || models.find(model => model.id === "deepseek/deepseek-flash")
      || models.find((model) => model.id.includes("deepseek") && model.id.includes("flash"));
    if (preferred) $("model").value = preferred.id;
    $("model").disabled = !models.length;
    $("model-count").textContent = `${models.length} models`;
    if (config) renderSetup();
  } catch { $("model").replaceChildren(new Option("Model catalog unavailable", "")); $("model-count").textContent = "Try again shortly"; }
}
start();
