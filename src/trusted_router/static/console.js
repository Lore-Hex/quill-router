"use strict";

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.left = "-9999px";
  document.body.appendChild(input);
  input.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    input.remove();
  }
}

function setCopyStatus(button, message, isError) {
  const statusId = button.getAttribute("aria-describedby");
  const status = statusId ? document.getElementById(statusId) : null;
  if (status) {
    status.textContent = message;
    status.classList.toggle("error", Boolean(isError));
  }
  button.textContent = isError ? "Copy" : "Copied";
  if (!isError) {
    window.setTimeout(() => {
      button.textContent = "Copy";
      if (status && status.textContent === message) {
        status.textContent = "";
      }
    }, 2200);
  }
}

function copyTargetText(target) {
  if (target.hasAttribute("data-copy-lines")) {
    return Array.from(target.children, (line) => line.textContent.trim())
      .join("\n\n")
      .trim();
  }
  return target.textContent.trim();
}

function templateCopyText(target, secret) {
  return copyTargetText(target).replaceAll("YOUR_TRUSTEDROUTER_API_KEY", secret);
}

function postActivationEvent(event, details = {}) {
  Promise.resolve().then(() => fetch("/analytics/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event, ...details }),
    credentials: "same-origin",
    keepalive: true,
  })).catch(() => {
    /* Acquisition telemetry is best-effort and must never block setup. */
  });
}

function microdollarsDisplay(value) {
  let amount = 0n;
  try {
    amount = BigInt(value || 0);
  } catch {
    amount = 0n;
  }
  const sign = amount < 0n ? "-" : "";
  const absolute = amount < 0n ? -amount : amount;
  const dollars = absolute / 1000000n;
  const fraction = String(absolute % 1000000n).padStart(6, "0");
  return `${sign}$${dollars}.${fraction}`;
}

function completionText(payload) {
  const content = payload?.choices?.[0]?.message?.content;
  if (typeof content === "string")
    return content.trim();
  if (Array.isArray(content)) {
    return content
      .filter((part) => part && (part.type === "text" || part.type === "output_text"))
      .map((part) => part.text || "")
      .join("")
      .trim();
  }
  return "";
}

function completionMetadata(payload, response) {
  const usage = payload?.usage && typeof payload.usage === "object" ? payload.usage : {};
  const providerUsage = usage.provider_usage && typeof usage.provider_usage === "object"
    ? usage.provider_usage
    : {};
  const trusted = payload?.trustedrouter && typeof payload.trustedrouter === "object"
    ? payload.trustedrouter
    : {};
  const routing = trusted.routing && typeof trusted.routing === "object"
    ? trusted.routing
    : {};
  return {
    model: response.headers.get("x-trustedrouter-served-model")
      || routing.selected_model
      || providerUsage.selected_model
      || payload?.model
      || "trustedrouter/cheap",
    provider: response.headers.get("x-trustedrouter-provider")
      || routing.selected_provider
      || providerUsage.selected_provider
      || payload?.provider
      || "Selected automatically",
    costMicrodollars: usage.total_cost_microdollars
      ?? usage.cost_microdollars
      ?? providerUsage.total_cost_microdollars
      ?? providerUsage.cost_microdollars
      ?? 0,
  };
}

function activationErrorCopy(status) {
  if (status === 401) {
    return {
      title: "This key was not accepted",
      message: "Create a new API key, then run the check again.",
      action: "/console/api-keys#new-api-key",
      actionLabel: "Create a new key",
    };
  }
  if (status === 402) {
    return {
      title: "Credits are required",
      message: "Add credits, then return here to run the live request.",
      action: "/console/credits",
      actionLabel: "Add credits",
    };
  }
  if (status === 429) {
    return {
      title: "The request was rate limited",
      message: "Wait a moment, then run the check again.",
    };
  }
  return {
    title: "The live request did not complete",
    message: "Your key is safe. Try again while TrustedRouter selects another healthy route.",
  };
}

function showActivationError(flow, status) {
  const copy = activationErrorCopy(status);
  const container = flow.querySelector("[data-call-error]");
  const result = flow.querySelector("[data-call-result]");
  if (result)
    result.hidden = true;
  if (!container)
    return;
  container.querySelector("[data-call-error-title]").textContent = copy.title;
  container.querySelector("[data-call-error-message]").textContent = copy.message;
  const action = container.querySelector("[data-call-error-action]");
  if (action) {
    action.hidden = !copy.action;
    if (copy.action) {
      action.href = copy.action;
      action.textContent = copy.actionLabel;
    }
  }
  container.hidden = false;
}

function markActivationComplete(flow) {
  const steps = flow.querySelectorAll(".activation-progress li");
  if (steps[1]) {
    steps[1].classList.remove("current");
    steps[1].classList.add("complete");
  }
  if (steps[2])
    steps[2].classList.add("current");
}

async function runFirstActivationCall(button) {
  const flow = button.closest("[data-first-call-flow]");
  if (!flow || button.disabled)
    return;
  const keySource = document.getElementById(flow.dataset.keySource || "");
  const apiKey = keySource ? copyTargetText(keySource) : "";
  const requestId = crypto.randomUUID();
  const started = performance.now();
  const elapsedMs = () => Math.min(120000, Math.max(0, Math.round(performance.now() - started)));
  const details = { attempt_id: requestId };
  postActivationEvent("onboarding_call_started", details);
  if (!apiKey) {
    postActivationEvent("onboarding_call_failed", {
      ...details, http_status: 0, elapsed_ms: elapsedMs(), failure_reason: "missing_key",
    });
    showActivationError(flow, 401);
    return;
  }

  const endpoint = flow.dataset.endpoint;
  const label = button.querySelector("[data-run-label]");
  const error = flow.querySelector("[data-call-error]");
  if (error)
    error.hidden = true;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  if (label)
    label.textContent = "Routing live request...";
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 75000);
  let status = 0;
  let failureReason = "network_error";
  let finishReason = "unknown";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
        "Idempotency-Key": `welcome-${requestId}`,
        "X-Request-ID": requestId,
      },
      body: JSON.stringify({
        model: "trustedrouter/cheap",
        messages: [{ role: "user", content: "Reply with exactly PONG." }],
        temperature: 0,
        // Reasoning tokens share this budget on some cheap routes.
        max_tokens: 512,
        stream: false,
      }),
    });
    status = response.status;
    failureReason = "http_error";
    if (!response.ok)
      throw new Error("request_failed");
    failureReason = "invalid_response";
    const payload = await response.json();
    const rawFinishReason = payload?.choices?.[0]?.finish_reason;
    if (["stop", "length", "content_filter", "tool_calls"].includes(rawFinishReason))
      finishReason = rawFinishReason;
    const output = completionText(payload);
    failureReason = finishReason === "length" ? "output_budget_exhausted" : "empty_output";
    if (!output)
      throw new Error("empty_response");
    failureReason = "client_error";
    const metadata = completionMetadata(payload, response);
    const elapsed = Math.max(1, Math.round(performance.now() - started));
    const result = flow.querySelector("[data-call-result]");
    result.querySelector("[data-result-output]").textContent = output;
    result.querySelector("[data-result-model]").textContent = metadata.model;
    result.querySelector("[data-result-provider]").textContent = metadata.provider;
    result.querySelector("[data-result-latency]").textContent = `${elapsed.toLocaleString()} ms`;
    result.querySelector("[data-result-cost]").textContent = microdollarsDisplay(
      metadata.costMicrodollars,
    );
    result.hidden = false;
    markActivationComplete(flow);
    if (label)
      label.textContent = "Test passed";
    button.classList.add("activation-run-success");
    postActivationEvent("onboarding_call_succeeded", {
      ...details, http_status: status, elapsed_ms: elapsedMs(), finish_reason: finishReason,
    });
  } catch {
    postActivationEvent("onboarding_call_failed", {
      ...details, http_status: status, elapsed_ms: elapsedMs(), finish_reason: finishReason,
      failure_reason: controller.signal.aborted ? "timeout" : failureReason,
    });
    showActivationError(flow, status);
    button.disabled = false;
    if (label)
      label.textContent = "Try the live request again";
  } finally {
    window.clearTimeout(timeout);
    button.removeAttribute("aria-busy");
  }
}

function selectSetupTab(tab) {
  const tablist = tab.closest("[role=tablist]");
  if (!tablist)
    return;
  const panelId = tab.dataset.setupTab;
  tablist.querySelectorAll("[data-setup-tab]").forEach((candidate) => {
    const selected = candidate === tab;
    candidate.setAttribute("aria-selected", String(selected));
    candidate.tabIndex = selected ? 0 : -1;
  });
  const scope = tablist.parentElement;
  scope.querySelectorAll("[data-setup-panel]").forEach((panel) => {
    panel.hidden = panel.id !== panelId;
  });
}

function ceilDivide(numerator, denominator) {
  return Math.floor((numerator + denominator - 1) / denominator);
}

// Mirrors services/stripe_fees.py processing_fee(): integer cents throughout,
// so the summary matches the line items Stripe Checkout will show.
function checkoutFeeCents(creditCents, rates) {
  const basisPoints = Number(rates.basis_points) || 0;
  const fixedCents = Number(rates.fixed_cents) || 0;
  const minimumCents = Number(rates.minimum_cents) || 0;
  const maxCents = rates.max_cents === null || rates.max_cents === undefined
    ? null
    : Number(rates.max_cents);
  const multiplier = Number(rates.multiplier_basis_points) || 10000;
  const uncapped = ceilDivide((creditCents + fixedCents) * 10000, 10000 - basisPoints);
  const charge = maxCents !== null && uncapped - creditCents > maxCents
    ? creditCents + maxCents
    : uncapped;
  return ceilDivide(Math.max(charge - creditCents, minimumCents) * multiplier, 10000);
}

function formatCents(cents) {
  return `$${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}

function initCreditsCheckout() {
  const form = document.querySelector("[data-checkout-form]");
  if (!form)
    return;
  // {rails: {option: rail}, rates: {rail: processing_fee() inputs}}, from the route.
  let schedule = { rails: {}, rates: {} };
  try {
    schedule = { rails: {}, rates: {}, ...JSON.parse(form.dataset.feeSchedule || "{}") };
  } catch {
    schedule = { rails: {}, rates: {} };
  }
  const paypalMinimum = Number(form.dataset.paypalMinimum) || 0;
  const presets = Array.from(form.querySelectorAll("[data-checkout-preset]"));
  const otherInput = form.querySelector("[data-checkout-amount]");
  const method = form.querySelector("[data-checkout-payment-method]");
  const fixedMethod = form.querySelector('input[type="hidden"][name="payment_method"]');
  const methodWrap = form.querySelector("[data-checkout-method-wrap]");
  // Each icon names the payment-method values it stands for.
  const methodIcons = Array.from(methodWrap?.querySelectorAll("[data-method-icon]") || []);
  const saved = form.querySelector("[data-checkout-saved]");
  const change = form.querySelector("[data-checkout-change]");
  const message = form.querySelector("[data-paypal-minimum-message]");
  const messageText = form.querySelector("[data-paypal-minimum-text]");
  const submit = form.querySelector("[data-checkout-submit]");
  const submitLabel = form.querySelector("[data-checkout-submit-label]");
  const cells = [
    form.querySelector("[data-summary-credit]"),
    form.querySelector("[data-summary-fee]"),
    form.querySelector("[data-summary-total]"),
  ];
  let bumpedTo = null;

  const selectedMethod = () => (method ? method.value : fixedMethod ? fixedMethod.value : "auto");
  const checkedPreset = () => presets.find((preset) => preset.checked) || null;
  const amountCents = () => {
    const checked = checkedPreset();
    if (!checked)
      return NaN;
    if (checked.value === "other") {
      if (!otherInput || otherInput.value.trim() === "")
        return NaN;
      const dollars = Number(otherInput.value);
      return Number.isInteger(dollars) ? dollars * 100 : NaN;
    }
    return Number(checked.value) * 100;
  };
  const setText = (element, text) => {
    if (element)
      element.textContent = text;
  };

  function sync(trigger) {
    const paypal = selectedMethod() === "paypal";
    const minimumCents = paypalMinimum * 100;
    presets.forEach((preset) => {
      if (preset.value !== "other")
        preset.disabled = paypal && Number(preset.value) * 100 < minimumCents;
    });
    bumpedTo = null;
    if (trigger !== "amount") {
      const checked = checkedPreset();
      if (paypal && checked && checked.disabled) {
        const first = presets.find((preset) => preset.value !== "other" && !preset.disabled);
        if (first) {
          first.checked = true;
          bumpedTo = Number(first.value);
        }
      }
    }
    const other = Boolean(checkedPreset() && checkedPreset().value === "other");
    if (otherInput) {
      otherInput.disabled = !other;
      otherInput.required = other;
    }
    if (methodWrap) {
      const icon = methodIcons.find(
        (span) => (span.dataset.methods || "").split(" ").includes(selectedMethod()),
      );
      methodWrap.dataset.method = icon ? icon.dataset.methodIcon : "card";
    }
    const cents = amountCents();
    const tooLow = paypal && Number.isFinite(cents) && cents < minimumCents;
    const valid = Number.isFinite(cents) && cents >= 100 && cents <= 1000000 && !tooLow;
    if (message && messageText) {
      message.hidden = !(paypal && (bumpedTo !== null || tooLow));
      messageText.textContent = tooLow
        ? `PayPal starts at $${paypalMinimum}.`
        : `PayPal starts at $${paypalMinimum}, so $${bumpedTo} is selected.`;
    }
    const rates = schedule.rates[schedule.rails[selectedMethod()] || "card"];
    if (valid && rates) {
      const fee = checkoutFeeCents(cents, rates);
      setText(cells[0], formatCents(cents));
      setText(cells[1], formatCents(fee));
      setText(cells[2], formatCents(cents + fee));
    } else {
      cells.forEach((cell) => setText(cell, "—"));
    }
    if (submit)
      submit.disabled = !valid;
    setText(submitLabel, paypal ? "Continue to PayPal" : "Continue to checkout");
  }

  presets.forEach((preset) => {
    preset.addEventListener("change", () => {
      sync("amount");
      if (preset.value === "other" && preset.checked && otherInput)
        otherInput.focus();
    });
  });
  otherInput?.addEventListener("input", () => sync("amount"));
  method?.addEventListener("change", () => sync("method"));
  // The chip stands in for the select only while the saved card is the
  // choice; a method the browser restored on Back stays visible.
  const syncSavedCard = () => {
    if (!change || !methodWrap || !saved || !method)
      return;
    const savedCardChosen = method.value === method.options[0]?.value;
    methodWrap.hidden = savedCardChosen;
    saved.hidden = !savedCardChosen;
  };
  if (change && methodWrap && saved && method) {
    change.hidden = false;
    change.addEventListener("click", () => {
      saved.hidden = true;
      methodWrap.hidden = false;
      method.focus();
    });
  }
  const syncAll = () => {
    syncSavedCard();
    sync("init");
  };
  syncAll();
  // Browsers restore form state (a preset, the Other amount, the method)
  // after this deferred script has run; pageshow follows that restoration.
  window.addEventListener("pageshow", syncAll);
}

function initAutoRefill() {
  const form = document.querySelector("[data-auto-refill]");
  const toggle = form?.querySelector("[data-auto-refill-switch]");
  if (!form || !toggle)
    return;
  // Switching on reveals the settings (CSS); switching off saves at once,
  // after the browser has had its say on the two amount fields.
  toggle.addEventListener("change", () => {
    if (toggle.checked)
      return;
    toggle.checked = true;
    if (!form.reportValidity())
      return;
    toggle.checked = false;
    form.submit();
  });
}

function replaceCreditsStripeFragment(documentFragment, sourceSelector, targetSelector) {
  const source = documentFragment.querySelector(sourceSelector);
  if (!source || !source.textContent.trim())
    return;
  document.querySelectorAll(targetSelector).forEach((target) => {
    target.replaceChildren(
      ...Array.from(source.childNodes, (node) => node.cloneNode(true)),
    );
  });
}

async function loadCreditsStripeDetails() {
  const panel = document.querySelector("[data-credits-stripe-details-url]");
  if (!panel)
    return;
  const history = document.querySelector("[data-credits-payment-history]");
  try {
    const response = await fetch(panel.dataset.creditsStripeDetailsUrl, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "text/html" },
    });
    if (!response.ok || response.redirected)
      throw new Error("credits_stripe_details_unavailable");
    const fragment = new DOMParser().parseFromString(await response.text(), "text/html");
    const fragmentHistory = fragment.querySelector(
      "[data-credits-payment-history-fragment]",
    );
    if (!fragmentHistory)
      throw new Error("credits_stripe_details_invalid");
    replaceCreditsStripeFragment(
      fragment,
      "[data-credits-saved-payment-method-fragment]",
      "[data-credits-saved-card-label]",
    );
    replaceCreditsStripeFragment(
      fragment,
      "[data-credits-payment-history-fragment]",
      "[data-credits-payment-history]",
    );
  } catch {
    if (history) {
      history.replaceChildren();
      const message = document.createElement("p");
      message.className = "credits-quiet";
      message.setAttribute("role", "status");
      message.textContent = "Payment history is temporarily unavailable.";
      history.append(message);
    }
  } finally {
    history?.setAttribute("aria-busy", "false");
  }
}

function initConsoleNavigation() {
  const sidebar = document.getElementById("console-navigation");
  const toggle = document.querySelector(".console-menu-toggle");
  const utilities = document.querySelector(".console-utilities");
  if (!sidebar || !toggle || !utilities) return;
  const home = utilities.parentElement;
  const media = window.matchMedia("(max-width: 800px)");
  const close = () => {
    document.body.classList.remove("console-navigation-open");
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "Menu";
  };
  const sync = () => {
    close();
    if (media.matches) sidebar.append(utilities);
    else home.insertBefore(utilities, toggle);
    utilities.querySelectorAll("details").forEach(detail => {
      detail.open = media.matches && detail.classList.contains("console-account");
    });
    const configuration = sidebar.querySelector(".console-configuration");
    if (configuration) configuration.open = !media.matches || Boolean(configuration.querySelector(".active"));
  };
  document.body.classList.add("console-navigation-ready");
  toggle.hidden = false;
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    document.body.classList.toggle("console-navigation-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Close" : "Menu";
  });
  sidebar.addEventListener("click", event => {
    if (event.target.closest("a") && media.matches) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      close();
      toggle.focus();
    }
    if (event.key === "Escape" && !media.matches) {
      utilities.querySelectorAll("details[open]").forEach(detail => {
        if (detail.contains(document.activeElement)) detail.querySelector("summary").focus();
        detail.open = false;
      });
    }
  });
  media.addEventListener("change", sync);
  sync();
}

function initConsole() {
  initConsoleNavigation();
  initCreditsCheckout();
  initAutoRefill();
  loadCreditsStripeDetails();
  const createKeyPanel = document.getElementById("new-api-key");
  if (createKeyPanel instanceof HTMLDetailsElement) {
    createKeyPanel.addEventListener("toggle", () => {
      if (!createKeyPanel.open) createKeyPanel.hidden = true;
    });
  }
  if (window.location.hash === "#new-api-key") {
    const panel = document.getElementById("new-api-key");
    if (panel) {
      panel.hidden = false;
      panel.open = true;
      window.setTimeout(() => panel.querySelector('input[name="name"]')?.focus(), 100);
    }
  }
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!target)
      return;
    const newKeyButton = target.closest('[data-action="open-new-key"]');
    if (newKeyButton) {
      event.preventDefault();
      const panel = document.getElementById("new-api-key");
      if (panel) {
        panel.hidden = false;
        panel.open = true;
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
        const input = panel.querySelector('input[name="name"]');
        if (input) {
          window.setTimeout(() => input.focus(), 250);
        }
      }
      return;
    }
    const runButton = target.closest('[data-action="run-first-call"]');
    if (runButton) {
      event.preventDefault();
      runFirstActivationCall(runButton);
      return;
    }
    const setupTab = target.closest("[data-setup-tab]");
    if (setupTab) {
      event.preventDefault();
      selectSetupTab(setupTab);
      return;
    }
    const templateButton = target.closest("[data-copy-template-target]");
    if (templateButton) {
      event.preventDefault();
      const templateId = templateButton.getAttribute("data-copy-template-target");
      const sourceId = templateButton.getAttribute("data-secret-source");
      const template = templateId ? document.getElementById(templateId) : null;
      const source = sourceId ? document.getElementById(sourceId) : null;
      const secret = source ? copyTargetText(source) : "";
      const value = template && secret ? templateCopyText(template, secret) : "";
      if (!value) {
        setCopyStatus(templateButton, "No key to copy.", true);
        return;
      }
      copyText(value)
        .then(() => setCopyStatus(templateButton, "Copied to clipboard.", false))
        .catch(() => setCopyStatus(
          templateButton,
          "Select the setup and copy it manually.",
          true,
        ));
      return;
    }
    const button = target.closest("[data-copy-secret]");
    if (!button)
      return;
    event.preventDefault();
    const secretId = button.getAttribute("data-copy-secret");
    const secret = secretId ? document.getElementById(secretId) : null;
    const value = secret ? copyTargetText(secret) : "";
    if (!value) {
      setCopyStatus(button, "No key to copy.", true);
      return;
    }
    copyText(value)
      .then(() => setCopyStatus(button, "Copied to clipboard.", false))
      .catch(() => setCopyStatus(button, "Select the key and copy it manually.", true));
  });

  document.addEventListener("keydown", (event) => {
    const tab = event.target.closest?.("[data-setup-tab]");
    if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
      return;
    const tabs = Array.from(tab.closest("[role=tablist]").querySelectorAll("[data-setup-tab]"));
    const current = tabs.indexOf(tab);
    let next = current;
    if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
    if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    event.preventDefault();
    selectSetupTab(tabs[next]);
    tabs[next].focus();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initConsole);
} else {
  initConsole();
}
