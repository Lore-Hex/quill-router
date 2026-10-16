/* Server-authorized honor-system rewards. No payout or eligibility is supplied here. */
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
  const opened = new Set();
  const confirmed = new Set();
  let activeAction;
  let placement = "console_fallback";
  const impressions = new Set();
  function observe(event, action, source = placement) {
    if (!crypto.randomUUID) return;
    request("/observe", { event, placement: source, ...(action ? { action } : {}),
      observation_id: crypto.randomUUID() }).catch(() => {});
  }
  function openerPlacement(button) {
    return button.hasAttribute("data-rewards-early") ? "api_key_setup"
      : button.hasAttribute("data-rewards-success") ? "first_request_success"
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
    github: ["GitHub", "Star the repository", "Star", "I starred Lore-Hex/quill-router on GitHub.", "On GitHub, sign in and select Star at the top of the repository."],
    x_trustedrouter: ["X", "Follow @TrustedRouter", "Follow", "I followed @TrustedRouter on X.", "On X, select Follow on the @TrustedRouter profile."],
    x_founder: ["X", "Follow @jperla", "Follow", "I followed @jperla on X.", "On X, select Follow on the @jperla profile."],
    linkedin: ["LinkedIn", "Follow TrustedRouter", "Follow", "I followed TrustedRouter on LinkedIn.", "On LinkedIn, select Follow on the TrustedRouter company page."],
    youtube: ["YouTube", "Subscribe to TrustedRouter", "Subscribe", "I subscribed to TrustedRouter on YouTube.", "On YouTube, select Subscribe on the TrustedRouter channel."],
  };
  function platformIcon(id) {
    const icon = document.createElement("span");
    icon.className = "social-rewards-icon";
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
      svg.append(path); icon.append(svg);
    }
    return icon;
  }
  let state;
  let returningFocus;
  let refreshing = false;
  let refreshAgain = false;
  const invitation = document.querySelector("[data-rewards-invitation]");
  let generation = 0;

  async function request(path = "", body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/console/social-rewards${path}`, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (!response.ok || response.redirected) throw new Error("rewards_unavailable");
      return await response.json();
    } finally { clearTimeout(timeout); }
  }

  function render(next) {
    state = { ...next, dismissed: next.dismissed || locallyDismissed };
    const ready = Boolean(state.eligible && state.enabled && state.first_call_complete
      && document.querySelector("[data-call-result]:not([hidden])"));
    if (invitation) invitation.hidden = !ready;
    const defaultActions = document.querySelector("[data-rewards-default-actions]");
    if (defaultActions) defaultActions.hidden = ready;
    openers.forEach((button) => {
      button.hidden = button.hasAttribute("data-rewards-success") ? !ready
        : button.hasAttribute("data-rewards-early") ? !(state.eligible && state.enabled) || ready
          : !state.eligible;
    });
    if (!state.eligible) {
      list.replaceChildren();
      notice.hidden = false;
      notice.textContent = "This account is not eligible for this offer.";
      dialog.querySelector("[data-rewards-destination]").textContent = "";
      return;
    }
    const actions = state.actions || [];
    const count = actions.filter((action) => action.claimed).length;
    dialog.querySelector("[data-rewards-progress]").textContent = `${count} of 5 rewards claimed`;
    dialog.querySelector("[data-rewards-meter]").value = count;
    dialog.querySelector("[data-rewards-destination]").textContent = `Rewards go to your initial workspace: ${state.workspace_name}.`;
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
        const link = document.createElement("a");
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = copy[1];
        link.href = action.url;
        link.addEventListener("click", () => {
          opened.add(action.id);
          activeAction = action.id;
          render(state);
          observe("social_reward_clicked", action.id);
        });
        link.setAttribute("aria-label", `${action.label} (opens a new tab)`);
        const amount = document.createElement("p");
        amount.textContent = `${copy[0]} · ≈200K tokens`;
        info.append(link, amount);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn social-rewards-start";
        button.dataset.rewardStart = "";
        button.setAttribute("aria-label", `${copy[2]} on ${copy[0]}: ${copy[1]}`);
        button.setAttribute("aria-controls", `reward-step-${action.id}`);
        button.addEventListener("click", () => link.click());
        const controls = document.createElement("div");
        controls.id = `reward-step-${action.id}`;
        controls.className = "social-rewards-controls";
        const instruction = document.createElement("p");
        instruction.textContent = copy[4];
        const confirmation = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) confirmed.add(action.id);
          else confirmed.delete(action.id);
          render(state);
        });
        confirmation.append(checkbox, document.createTextNode(copy[3]));
        const claimButton = document.createElement("button");
        claimButton.type = "button";
        claimButton.className = "btn primary";
        claimButton.dataset.rewardClaim = "";
        claimButton.addEventListener("click", () => claim(action.id));
        const status = document.createElement("p");
        status.setAttribute("role", "status");
        controls.append(instruction, confirmation, claimButton, status);
        row.append(platformIcon(action.id), info, button, controls);
        list.append(row);
      }
      const button = row.querySelector("[data-reward-start]");
      const claimButton = row.querySelector("[data-reward-claim]");
      const checkbox = row.querySelector("input");
      const busy = pending.has(action.id);
      const unavailable = Boolean(action.claimed || pending.size || !state.enabled);
      const expanded = activeAction === action.id && opened.has(action.id) && !action.claimed;
      row.querySelector(".social-rewards-controls").hidden = !expanded;
      button.disabled = unavailable;
      button.setAttribute("aria-expanded", String(expanded));
      button.textContent = action.claimed ? "✓ Claimed" : opened.has(action.id) ? "Open again" : presentation[action.id][2];
      if (action.claimed) button.setAttribute("aria-label", "✓ Claimed");
      checkbox.checked = confirmed.has(action.id);
      checkbox.disabled = unavailable;
      claimButton.disabled = unavailable || !opened.has(action.id) || !confirmed.has(action.id);
      claimButton.setAttribute("aria-busy", String(busy));
      claimButton.textContent = busy ? "Adding reward…" : errors.has(action.id) ? "Retry claim" : "Claim 200K tokens";
      row.classList.toggle("claimed", Boolean(action.claimed));
      row.querySelector('[role="status"]').textContent = errors.has(action.id) && !action.claimed
        ? "Couldn’t confirm your reward. Retry safely; it can only be added once." : "";
    });
  }

  async function claim(id) {
    if (pending.size || !opened.has(id) || !confirmed.has(id) || !state?.eligible || !state.enabled) return;
    generation += 1;
    pending.add(id); errors.delete(id); render(state);
    try {
      render(await request("/claim", { action: id, attested: true }));
    } catch {
      // A lost response can follow a committed grant. Read authoritative progress
      // before offering the same account/action claim again.
      try { render(await request()); } catch { /* Keep previous committed state. */ }
      if (!state.actions.some((action) => action.id === id && action.claimed)) errors.add(id);
    } finally {
      pending.delete(id); render(state);
    }
  }

  function open(source = "console_fallback") {
    if (dialog.open) return;
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
      // Showing the invitation consumes the automatic offer, so continuing
      // to the console does not immediately interrupt the user with a modal.
      if (invitation && !invitation.hidden && !state.dismissed) {
        const shown = await request("/shown", {});
        if (current !== generation) return;
        render(shown);
      }
      if (auto && !invitation && state.eligible && state.enabled && state.first_call_complete && !state.dismissed
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
  window.addEventListener("trustedrouter:first-call-success", () => refresh());
  refresh(true);
})();
