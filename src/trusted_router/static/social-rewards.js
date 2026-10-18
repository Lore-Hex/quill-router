/* Server-authorized promotional rewards. No payout or eligibility is supplied here. */
(() => {
  "use strict";
  const dialog = document.getElementById("social-rewards");
  if (!dialog) return;
  const list = dialog.querySelector("[data-rewards-list]");
  const notice = dialog.querySelector("[data-rewards-notice]");
  const openers = document.querySelectorAll("[data-rewards-open]");
  const dismissalKey = `social-rewards-v1:dismissed:${dialog.dataset.rewardsUser}`;
  let locallyDismissed = false;
  try { locallyDismissed = localStorage.getItem(dismissalKey) === "1"; } catch { /* Storage may be blocked. */ }
  const pending = new Set();
  const errors = new Set();
  let placement = "console_fallback";
  const impressions = new Set();
  function observe(event, action, source = placement) {
    if (!crypto.randomUUID) return;
    request("/observe", { event, placement: source, ...(action ? { action } : {}),
      observation_id: crypto.randomUUID() }).catch(() => {});
  }
  function openerPlacement(button) {
    return button.hasAttribute("data-rewards-api-keys") ? "api_key_setup"
        : location.pathname === "/console/credits" ? "credits" : "console_fallback";
  }
  const impressionObserver = typeof IntersectionObserver === "undefined" ? null
    : new IntersectionObserver((entries) => {
      entries.forEach(({ target, isIntersecting }) => {
        const source = openerPlacement(target);
        if (!isIntersecting || target.hidden || impressions.has(source)
            || document.visibilityState !== "visible" || !state?.enabled || !state?.eligible) return;
        impressions.add(source);
        observe("social_rewards_invitation_viewed", null, source);
      });
    });
  openers.forEach((button) => impressionObserver?.observe(button));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") openers.forEach((button) => {
      impressionObserver?.unobserve(button); impressionObserver?.observe(button);
    });
  });
  const presentation = {
    github: ["GitHub", "Star the repository", "Star"],
    x_trustedrouter: ["X", "Follow @TrustedRouter", "Follow"],
    x_founder: ["X", "Follow @jperla", "Follow"],
    linkedin: ["LinkedIn", "Follow TrustedRouter", "Follow"],
    youtube: ["YouTube", "Subscribe to TrustedRouter", "Subscribe"],
  };
  function platformIcon(id) {
    const icon = document.createElement("span");
    icon.className = "social-rewards-icon";
    icon.dataset.platform = id;
    icon.setAttribute("aria-hidden", "true");
    if (id === "github") {
      const img = document.createElement("img");
      img.src = "/static/provider-logos/github-mark.png";
      img.alt = "";
      icon.append(img);
    } else {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      const path = document.createElementNS(svg.namespaceURI, "path");
      path.setAttribute("fill", "currentColor");
      path.setAttribute("d", id.startsWith("x_")
        ? "M18.9 2H22l-6.8 7.8L23.2 22h-6.3L12 14.6 5.5 22H2.3l8.2-9.4L.8 2h6.5l4.4 6.7L18.9 2Zm-1.1 18h1.7L6.4 3.9H4.6L17.8 20Z"
        : id === "linkedin"
          ? "M20.4 2H3.6C2.7 2 2 2.7 2 3.6v16.8c0 .9.7 1.6 1.6 1.6h16.8c.9 0 1.6-.7 1.6-1.6V3.6c0-.9-.7-1.6-1.6-1.6ZM8 19H5V9.5h3V19ZM6.5 8.2a1.7 1.7 0 1 1 0-3.4 1.7 1.7 0 0 1 0 3.4ZM19 19h-3v-4.6c0-1.1 0-2.5-1.5-2.5s-1.8 1.2-1.8 2.4V19h-3V9.5h2.9v1.3h.1c.4-.8 1.4-1.6 2.8-1.6 3 0 3.5 2 3.5 4.5V19Z"
          : "M23 7s-.2-1.7-.9-2.4c-.9-.9-1.9-.9-2.4-1C16.4 3.4 12 3.4 12 3.4s-4.4 0-7.7.2c-.5.1-1.5.1-2.4 1C1.2 5.3 1 7 1 7S.8 9 .8 11v2c0 2 .2 4 .2 4s.2 1.7.9 2.4c.9.9 2.1.9 2.6 1 1.9.2 7.5.2 7.5.2s4.4 0 7.7-.2c.5-.1 1.5-.1 2.4-1 .7-.7.9-2.4.9-2.4s.2-2 .2-4v-2c0-2-.2-4-.2-4ZM9.7 15.5v-7l6.4 3.5-6.4 3.5Z");
      if (id === "linkedin" || id === "youtube") {
        const backing = document.createElementNS(svg.namespaceURI, id === "linkedin" ? "rect" : "path");
        backing.setAttribute("fill", "#fff");
        if (id === "linkedin") {
          backing.setAttribute("x", "3"); backing.setAttribute("y", "3");
          backing.setAttribute("width", "18"); backing.setAttribute("height", "18");
        } else {
          backing.setAttribute("d", "M9.7 15.5v-7l6.4 3.5-6.4 3.5Z");
        }
        svg.append(backing);
      }
      svg.append(path); icon.append(svg);
    }
    return icon;
  }
  let state;
  let returningFocus;
  let refreshing = false;
  let refreshAgain = false;
  let generation = 0;

  async function request(path = "", body, timeoutMs = 15000) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`/console/social-rewards${path}`, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (!response.ok || response.redirected) {
        const error = new Error("rewards_unavailable");
        error.retryable = !response.redirected && response.status >= 500;
        throw error;
      }
      return await response.json();
    } finally { clearTimeout(timeout); }
  }

  function render(next) {
    state = { ...next, dismissed: next.dismissed || locallyDismissed };
    const total = (state.actions || []).length;
    const keyCard = document.querySelector("[data-rewards-key-card]");
    if (keyCard) {
      keyCard.hidden = !state.eligible || !state.enabled;
      const claimed = (state.actions || []).filter((action) => action.claimed).length;
      keyCard.querySelector("[data-rewards-key-title]").textContent = total > 0 && claimed === total
        ? "All available rewards claimed." : !state.enabled ? "Rewards are paused."
          : claimed ? "Keep earning extra tokens." : "Earn extra API credit.";
      keyCard.querySelector("[data-rewards-key-description]").textContent = total > 0 && claimed === total
        ? "All available rewards have been added to your signup workspace."
        : !state.enabled ? "Your progress is saved. Check back to finish your rewards."
          : claimed ? `${total - claimed} rewards left to claim. Pick up where you left off.`
            : "Earn API credit by following us on social.";
      keyCard.querySelector("[data-rewards-open]").textContent = total > 0 && claimed === total || !state.enabled
        ? "View rewards" : "Earn extra tokens";
    }
    openers.forEach((button) => { button.hidden = !(state.eligible && state.enabled); });
    if (!state.enabled && dialog.open) dialog.close();
    if (!state.eligible) {
      list.replaceChildren();
      notice.hidden = false;
      notice.textContent = "This account is not eligible for this offer.";
      dialog.querySelector("[data-rewards-destination]").textContent = "";
      return;
    }
    const actions = state.actions || [];
    const count = actions.filter((action) => action.claimed).length;
    dialog.querySelector("[data-rewards-progress]").textContent = `${count} of ${total} rewards claimed`;
    dialog.querySelector("[data-rewards-meter]").max = total || 1;
    dialog.querySelector("[data-rewards-meter]").value = count;
    Array.from(list.children).forEach((row) => {
      if (!actions.some((action) => action.id === row.dataset.rewardId)) row.remove();
    });
    dialog.querySelector("[data-rewards-destination]").textContent = `Credit goes to your signup workspace: ${state.workspace_name}.`;
    notice.hidden = state.enabled;
    notice.textContent = !state.enabled
      ? "Rewards are currently paused. Your claimed rewards are safe."
      : "";
    actions.forEach((action) => {
      let row = Array.from(list.children).find((item) => item.dataset.rewardId === action.id);
      if (!row) {
        row = document.createElement("li");
        row.dataset.rewardId = action.id;
        const copy = presentation[action.id];
        const info = document.createElement("div");
        info.className = "social-rewards-info";
        const title = document.createElement("span");
        title.className = "social-rewards-action-title";
        title.textContent = copy[1];
        const amount = document.createElement("p");
        amount.textContent = `${copy[0]} · 200K tokens`;
        info.append(title, amount);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn social-rewards-start";
        button.dataset.rewardStart = "";
        button.addEventListener("click", () => {
          if (pending.size || !state?.eligible || !state.enabled
              || state.actions.find((item) => item.id === action.id)?.claimed) return;
          if (!errors.has(action.id)) {
            // Open synchronously in the user's gesture; no tab-return or follow proof is required.
            window.open(action.url, "_blank", "noopener,noreferrer");
            observe("social_reward_clicked", action.id);
          }
          claim(action.id);
        });
        const status = document.createElement("p");
        status.className = "social-rewards-controls";
        status.setAttribute("role", "status");
        row.append(platformIcon(action.id), info, button, status);
        list.append(row);
      }
      const button = row.querySelector("[data-reward-start]");
      const busy = pending.has(action.id);
      button.disabled = Boolean(action.claimed || pending.size || !state.enabled);
      button.setAttribute("aria-busy", String(busy));
      button.textContent = action.claimed ? "✓ Claimed" : busy ? "Adding reward…"
        : errors.has(action.id) ? "Retry" : presentation[action.id][2];
      button.setAttribute("aria-label", action.claimed || busy || errors.has(action.id)
        ? button.textContent : `${presentation[action.id][2]} on ${presentation[action.id][0]}: ${presentation[action.id][1]}`);
      row.classList.toggle("claimed", Boolean(action.claimed));
      const status = row.querySelector('[role="status"]');
      status.hidden = !errors.has(action.id) || action.claimed;
      status.textContent = status.hidden ? "" : "We couldn’t confirm your reward yet.";
    });
  }

  async function claim(id) {
    if (pending.size || !state?.eligible || !state.enabled) return;
    generation += 1;
    const retry = errors.has(id);
    pending.add(id); errors.delete(id); render(state);
    // All attempts and reconciliation share a 15-second budget, not 15 seconds each.
    const deadline = Date.now() + 15000;
    const claimed = () => state.actions.some((action) => action.id === id && action.claimed);
    const boundedRequest = (path, body) => request(path, body, Math.max(1, Math.min(4000, deadline - Date.now())));
    try {
      if (retry) {
        try { render(await boundedRequest()); } catch { /* The same receipt makes replay safe. */ }
      }
      for (let attempt = 0; attempt < 2 && Date.now() < deadline; attempt += 1) {
        if (claimed() || !state.eligible || !state.enabled) break;
        let retryable = true;
        try {
          render(await boundedRequest("/claim", { action: id }));
          break;
        } catch (error) {
          retryable = error.retryable !== false;
          // A timed-out request can still commit. Read its receipt before replaying.
          if (Date.now() < deadline) {
            try { render(await boundedRequest()); } catch { /* Keep the last authoritative state. */ }
          }
        }
        if (!retryable || claimed() || attempt === 1 || Date.now() >= deadline) break;
        await new Promise((resolve) => setTimeout(resolve, Math.min(500, deadline - Date.now())));
      }
    } finally {
      if (!claimed()) errors.add(id);
      pending.delete(id); render(state);
    }
  }

  function open(source = "console_fallback") {
    if (dialog.open || !state?.eligible || !state.enabled) return;
    returningFocus = document.activeElement;
    dialog.showModal();
    placement = source;
    observe("social_rewards_opened");
    dialog.querySelector("h2").focus();
  }
  function canAutoOpen() {
    return !document.querySelector(".console-key-reveal") && !document.querySelector("dialog[open]");
  }
  async function refresh(auto = false) {
    if (refreshing) { refreshAgain = true; return; }
    if (pending.size) return;
    refreshing = true;
    const current = generation;
    try {
      const next = await request();
      if (current !== generation) return;
      if (locallyDismissed && next.eligible && !next.dismissed) {
        // Retry an offline dismissal before considering automatic display.
        try { await request("/dismiss", {}); } catch { /* Retain local suppression for the next visit. */ }
        if (current !== generation) return;
      }
      render(next);
      if (auto && state.eligible && state.enabled && state.first_call_complete && !state.dismissed
          && canAutoOpen()) {
        const shown = await request("/shown", {});
        if (current !== generation) return;
        render(shown);
        if (shown.show_now && canAutoOpen()) open();
      }
    } catch { /* Optional offer must not block the console or key setup. */ }
    finally {
      refreshing = false;
      if (refreshAgain) { refreshAgain = false; refresh(); }
    }
  }
  function dismiss() {
    locallyDismissed = true;
    generation += 1; // Invalidate any automatic opening already in flight.
    try { localStorage.setItem(dismissalKey, "1"); } catch { /* Keep suppression in memory. */ }
    dialog.close();
    request("/dismiss", {}).catch(() => {
      // Manual opening may precede /shown. Refresh retries this preference;
      // the account-scoped local marker also survives navigation and reload.
    });
  }
  openers.forEach((button) => button.addEventListener("click", () => { open(openerPlacement(button)); refresh(); }));
  dialog.querySelectorAll("[data-rewards-close]").forEach((button) => button.addEventListener("click", dismiss));
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const controls = Array.from(dialog.querySelectorAll("a[href], button:not(:disabled), input:not(:disabled), summary"))
      .filter((node) => node.getClientRects().length);
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first?.focus();
    }
  });
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); dismiss(); });
  dialog.addEventListener("close", () => returningFocus?.focus());
  window.addEventListener("focus", () => refresh(!dialog.open));
  refresh(true);
})();
