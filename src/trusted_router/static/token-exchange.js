(() => {
  "use strict";
  const form = document.getElementById("enterprise-brief-form");
  if (!form) return;
  const button = form.querySelector("button[type=submit]");
  const email = form.querySelector("input[name=email]");
  const status = document.getElementById("brief-status");
  const label = button.innerHTML;
  let busy = false;
  button.disabled = false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    busy = true;
    button.disabled = true;
    button.textContent = "Preparing your brief...";
    form.setAttribute("aria-busy", "true");
    status.textContent = "";
    status.dataset.state = "loading";
    email.removeAttribute("aria-invalid");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.value.trim(), website: form.elements.website.value }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const errors = {
          422: "Please enter a valid email address.",
          429: "You've reached the download limit. Please try later or email enterprise@trustedrouter.com.",
        };
        if (response.status === 422) email.setAttribute("aria-invalid", "true");
        throw new Error(errors[response.status] || "We couldn't prepare your brief. Please try again or email enterprise@trustedrouter.com.");
      }
      if (!response.headers.get("content-type")?.includes("application/pdf")) {
        throw new Error("Please refresh the page and try again, or email enterprise@trustedrouter.com.");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const download = document.createElement("a");
      download.href = url;
      download.download = "TrustedRouter-Enterprise-Brief.pdf";
      document.body.appendChild(download);
      download.click();
      download.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      status.dataset.state = "success";
      status.textContent = "Your download has started. Share the brief with your team. We'd be happy to discuss your requirements.";
    } catch (error) {
      status.dataset.state = "error";
      status.textContent = error.name === "AbortError"
        ? "This is taking longer than expected. Please try again or email enterprise@trustedrouter.com."
        : error instanceof TypeError
          ? "Please check your connection and try again."
          : error.message;
    } finally {
      clearTimeout(timeout);
      busy = false;
      button.disabled = false;
      button.innerHTML = label;
      form.removeAttribute("aria-busy");
    }
  });
})();
