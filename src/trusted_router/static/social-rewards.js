/* Server-authorized honor-system rewards. No payout or eligibility is supplied here. */
(() => {
  "use strict";
  const dialog = document.getElementById("social-rewards");
  if (!dialog) return;
  const list = dialog.querySelector("[data-rewards-list]");
  const notice = dialog.querySelector("[data-rewards-notice]");
  const openers = document.querySelectorAll("[data-rewards-open]");
  const pending = new Set();
  const errors = new Set();
  let state;
  let returningFocus;
  let refreshing = false;
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
    state = next;
    openers.forEach((button) => { button.hidden = !state.eligible; });
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
    notice.hidden = state.enabled && state.first_call_complete;
    notice.textContent = !state.enabled
      ? "Rewards are currently paused. Your claimed rewards are safe."
      : "Make your first successful API call to unlock these rewards.";
    actions.forEach((action) => {
      let row = Array.from(list.children).find((item) => item.dataset.rewardId === action.id);
      if (!row) {
        row = document.createElement("li");
        row.dataset.rewardId = action.id;
        const info = document.createElement("div");
        const link = document.createElement("a");
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = `${action.label} ↗`;
        link.href = action.url;
        link.addEventListener("click", () => {
          request("/clicked", { action: action.id }).catch(() => {
            /* Optional telemetry must not block the outbound link or rewards. */
          });
        });
        link.setAttribute("aria-label", `${action.label} (opens a new tab)`);
        const amount = document.createElement("p");
        amount.textContent = "Approximately 200K tokens";
        info.append(link, amount);
        const controls = document.createElement("div");
        controls.className = "social-rewards-controls";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn primary";
        button.addEventListener("click", () => claim(action.id));
        const status = document.createElement("p");
        status.setAttribute("role", "status");
        controls.append(button, status);
        row.append(info, controls);
        list.append(row);
      }
      const button = row.querySelector("button");
      const busy = pending.has(action.id);
      button.disabled = Boolean(action.claimed || pending.size || !state.enabled || !state.first_call_complete);
      button.setAttribute("aria-busy", String(busy));
      button.textContent = action.claimed ? "✓ Claimed" : busy ? "Adding reward…" : errors.has(action.id) ? "Retry claim" : "I’ve done this — claim reward";
      row.classList.toggle("claimed", Boolean(action.claimed));
      row.querySelector('[role="status"]').textContent = errors.has(action.id) && !action.claimed
        ? "Couldn’t confirm your reward. Retry safely; it can only be added once." : "";
    });
  }

  async function claim(id) {
    if (pending.size || !state?.eligible || !state.enabled || !state.first_call_complete) return;
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

  function open() {
    if (dialog.open) return;
    returningFocus = document.activeElement;
    dialog.showModal();
    dialog.querySelector("h2").focus();
  }
  function canAutoOpen() {
    return !document.querySelector(".console-key-reveal") && !document.querySelector("dialog[open]");
  }
  async function refresh(auto = false) {
    if (refreshing || pending.size) return;
    refreshing = true;
    const current = generation;
    try {
      const next = await request();
      if (current !== generation) return;
      render(next);
      if (auto && state.eligible && state.enabled && state.first_call_complete && !state.dismissed
          && canAutoOpen()) {
        const shown = await request("/shown", {});
        if (current !== generation) return;
        render(shown);
        if (shown.show_now && canAutoOpen()) open();
      }
    } catch { /* Optional offer must not block the console or key setup. */ }
    finally { refreshing = false; }
  }
  function dismiss() {
    dialog.close();
    request("/dismiss", {}).catch(() => {
      // Automatic display was durably suppressed before opening. A failed
      // explicit dismissal therefore cannot cause repeat automatic popups.
    });
  }
  openers.forEach((button) => button.addEventListener("click", () => { open(); refresh(); }));
  dialog.querySelectorAll("[data-rewards-close]").forEach((button) => button.addEventListener("click", dismiss));
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const controls = Array.from(dialog.querySelectorAll("a[href], button:not(:disabled)"))
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
