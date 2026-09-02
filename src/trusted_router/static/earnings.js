(() => {
  "use strict";

  const message = document.querySelector("[data-payout-message]");
  const payoutIdempotencyStorageKey = "trustedrouter.routable-cashout-idempotency";
  let payoutIdempotencyKey = null;

  function currentPayoutIdempotencyKey() {
    if (payoutIdempotencyKey) return payoutIdempotencyKey;
    try {
      payoutIdempotencyKey = window.sessionStorage.getItem(payoutIdempotencyStorageKey);
    } catch (_) {
      payoutIdempotencyKey = null;
    }
    if (!payoutIdempotencyKey) {
      payoutIdempotencyKey = crypto.randomUUID();
      try {
        window.sessionStorage.setItem(payoutIdempotencyStorageKey, payoutIdempotencyKey);
      } catch (_) {
        // The in-memory key still protects retries for this page load.
      }
    }
    return payoutIdempotencyKey;
  }

  function clearPayoutIdempotencyKey() {
    payoutIdempotencyKey = null;
    try {
      window.sessionStorage.removeItem(payoutIdempotencyStorageKey);
    } catch (_) {
      // Storage may be unavailable in privacy-restricted browser contexts.
    }
  }

  function showMessage(text, kind) {
    if (!message) return;
    message.textContent = text;
    message.className = `console-flash ${kind}`;
    message.hidden = false;
  }

  async function parseResponse(response) {
    let payload = {};
    try {
      payload = await response.json();
    } catch (_) {
      payload = {};
    }
    if (!response.ok) {
      const text = payload?.error?.message || "The payout request could not be completed.";
      throw new Error(text);
    }
    return payload;
  }

  const onboarding = document.querySelector("[data-routable-onboarding-form]");
  onboarding?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = onboarding.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    const values = Object.fromEntries(new FormData(onboarding).entries());
    try {
      const response = await fetch("/v1/payouts/onboarding", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      const payload = await parseResponse(response);
      if (payload.onboarding_url) {
        window.location.assign(payload.onboarding_url);
        return;
      }
      window.location.reload();
    } catch (error) {
      showMessage(error instanceof Error ? error.message : "Onboarding failed.", "error");
      if (button) button.disabled = false;
    }
  });

  const cashout = document.querySelector("[data-routable-cashout-form]");
  cashout?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = cashout.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    const amount = new FormData(cashout).get("amount");
    try {
      const response = await fetch("/v1/payouts", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": currentPayoutIdempotencyKey(),
        },
        body: JSON.stringify({ amount }),
      });
      const payload = await parseResponse(response);
      const state = String(payload?.data?.state || "submitted").replaceAll("_", " ");
      if (response.status === 202) {
        showMessage(
          `Cash-out ${state}. TrustedRouter retained the reservation and can safely check it again.`,
          "success",
        );
        window.setTimeout(() => window.location.reload(), 800);
        return;
      }
      clearPayoutIdempotencyKey();
      showMessage(`Cash-out ${state}.`, "success");
      window.setTimeout(() => window.location.reload(), 800);
    } catch (error) {
      showMessage(error instanceof Error ? error.message : "Cash-out failed.", "error");
      if (button) button.disabled = false;
    }
  });

  document.querySelectorAll("[data-payout-retry]").forEach((button) => {
    button.addEventListener("click", async () => {
      const payoutId = button.getAttribute("data-payout-retry");
      if (!payoutId) return;
      button.disabled = true;
      try {
        const response = await fetch(`/v1/payouts/${encodeURIComponent(payoutId)}/retry`, {
          method: "POST",
          credentials: "same-origin",
        });
        const payload = await parseResponse(response);
        const state = String(payload?.data?.state || "submitted").replaceAll("_", " ");
        showMessage(`Cash-out ${state}.`, "success");
        window.setTimeout(() => window.location.reload(), 800);
      } catch (error) {
        showMessage(error instanceof Error ? error.message : "Status check failed.", "error");
        button.disabled = false;
      }
    });
  });
})();
