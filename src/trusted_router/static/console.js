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

function updateCheckoutMinimum() {
  const method = document.querySelector("[data-checkout-payment-method]");
  const amount = document.querySelector("[data-checkout-amount]");
  const message = document.querySelector("[data-paypal-minimum-message]");
  if (!method || !amount)
    return;

  const isPayPal = method.value === "paypal";
  const minimum = isPayPal
    ? Number(amount.dataset.paypalMinimum)
    : Number(amount.dataset.defaultMinimum);
  if (Number.isFinite(minimum)) {
    amount.min = String(minimum);
    if (isPayPal && Number(amount.value) < minimum) {
      amount.value = String(minimum);
    }
  }
  if (message) {
    message.hidden = !isPayPal;
  }
}

function replaceCreditsStripeFragment(documentFragment, sourceSelector, targetSelector) {
  const source = documentFragment.querySelector(sourceSelector);
  const target = document.querySelector(targetSelector);
  if (!source || !target)
    return;
  target.replaceChildren(
    ...Array.from(source.childNodes, (node) => node.cloneNode(true)),
  );
}

async function loadCreditsStripeDetails() {
  const panel = document.querySelector("[data-credits-stripe-details-url]");
  if (!panel)
    return;
  const history = document.querySelector("[data-credits-payment-history]");
  const count = document.querySelector("[data-credits-payment-history-count]");
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
      "[data-credits-saved-payment-method]",
    );
    replaceCreditsStripeFragment(
      fragment,
      "[data-credits-payment-history-count-fragment]",
      "[data-credits-payment-history-count]",
    );
    replaceCreditsStripeFragment(
      fragment,
      "[data-credits-payment-history-fragment]",
      "[data-credits-payment-history]",
    );
  } catch {
    if (count)
      count.textContent = "unavailable";
    if (history) {
      history.replaceChildren();
      const message = document.createElement("p");
      message.style.cssText = "color:var(--muted);font-size:14px;margin:0";
      message.textContent = (
        "Payment history is temporarily unavailable. "
        + "Your balance and billing controls are unaffected."
      );
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
  updateCheckoutMinimum();
  loadCreditsStripeDetails();
  const checkoutMethod = document.querySelector("[data-checkout-payment-method]");
  if (checkoutMethod) {
    checkoutMethod.addEventListener("change", updateCheckoutMinimum);
  }
  const createKeyPanel = document.getElementById("new-api-key");
  if (createKeyPanel) {
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
    const setupTab = target.closest("[data-setup-tab]");
    if (setupTab) {
      event.preventDefault();
      selectSetupTab(setupTab);
      return;
    }
    const button = target.closest("[data-copy-secret]");
    if (!button)
      return;
    event.preventDefault();
    const secretId = button.getAttribute("data-copy-secret");
    const secret = secretId ? document.getElementById(secretId) : null;
    const value = secret ? secret.textContent.trim() : "";
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
