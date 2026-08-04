(() => {
  const form = document.querySelector("[data-bgb-form]");
  const toggle = document.querySelector("[data-bgb-message-toggle]");
  const fields = document.querySelector("[data-bgb-message-fields]");

  const syncMessageFields = () => {
    if (!toggle || !fields) return;
    fields.hidden = !toggle.checked;
    const confirmation = fields.querySelector('input[name="public_message_confirmed"]');
    const message = fields.querySelector('textarea[name="public_message"]');
    if (confirmation) confirmation.required = toggle.checked;
    if (message) message.required = toggle.checked;
  };

  const amount = (name) => {
    const input = form?.querySelector(`[name="${name}"]`);
    const number = Number(input?.value || 0);
    return Number.isFinite(number) && number > 0 ? number : 0;
  };

  const dollars = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });

  const syncMath = () => {
    if (!form) return;
    const annual = amount("monthly_minimum") * 12;
    const annualNode = document.querySelector("[data-bgb-annual]");
    const savingsNode = document.querySelector("[data-bgb-savings]");
    if (annualNode) annualNode.textContent = dollars.format(annual);
    if (savingsNode) savingsNode.textContent = dollars.format(annual * 0.10);
  };

  toggle?.addEventListener("change", syncMessageFields);
  form?.addEventListener("input", syncMath);
  syncMessageFields();
  syncMath();

  document.querySelector("[data-bgb-remove]")?.addEventListener("click", (event) => {
    if (!window.confirm("Remove your pledge and anonymous public note?")) {
      event.preventDefault();
    }
  });

  document.querySelectorAll("[data-bgb-share]").forEach((button) => {
    const originalLabel = button.textContent;
    button.addEventListener("click", async () => {
      const share = {
        title: "The $1M Bedrock Group Buy",
        text: "Founders are combining Bedrock commitments to negotiate as one buyer and keep 10%.",
        url: new URL("/bedrock-group-buy", window.location.origin).href,
      };
      try {
        if (navigator.share) {
          await navigator.share(share);
        } else {
          await navigator.clipboard.writeText(share.url);
          button.textContent = "Link copied";
          window.setTimeout(() => { button.textContent = originalLabel; }, 1800);
        }
      } catch (error) {
        if (error?.name !== "AbortError") button.textContent = "Copy failed";
      }
    });
  });
})();
