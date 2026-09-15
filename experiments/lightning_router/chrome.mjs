import { createIcons, Moon, Sun, CircleHelp, X, Send } from "lucide";

const toolbar = document.createElement("div");
toolbar.className = "header-actions";
toolbar.innerHTML = `<button id="help-button" class="icon-button" type="button" aria-label="Help" title="Help" hidden><i data-lucide="circle-help"></i></button><button id="theme-toggle" class="icon-button" type="button"></button>`;
document.querySelector("header").append(toolbar);
const theme = document.getElementById("theme-toggle");
const help = document.getElementById("help-button");
let supportKey = null;
let dialog = null;
function icons() { createIcons({ icons: { Moon, Sun, CircleHelp, X, Send } }); }
function renderTheme() {
  const dark = document.documentElement.dataset.theme === "dark";
  theme.innerHTML = dark ? '<i data-lucide="sun"></i>' : '<i data-lucide="moon"></i>';
  theme.title = theme.ariaLabel = dark ? "Switch to light mode" : "Switch to dark mode";
  icons();
}
theme.addEventListener("click", () => {
  const value = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem("lightningrouter-theme", value); } catch {}
  renderTheme();
});
renderTheme();

export function connectSupport(key, eligible) {
  const next = eligible === true ? key : null;
  if (next !== supportKey && dialog) { dialog.close(); dialog.remove(); dialog = null; }
  supportKey = next;
  help.hidden = !supportKey;
}

help.addEventListener("click", () => {
  if (!supportKey) return;
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.className = "help-dialog";
    dialog.setAttribute("aria-labelledby", "help-title");
    dialog.innerHTML = `<div class="section-heading"><h2 id="help-title">How can we help?</h2><button type="button" class="icon-button" aria-label="Close help" title="Close help"><i data-lucide="x"></i></button></div>
      <form><label for="help-email">Your email</label><input id="help-email" type="email" autocomplete="email" maxlength="254" required>
      <label for="help-message">Message</label><textarea id="help-message" rows="5" maxlength="3000" required></textarea>
      <p class="muted small">Your user and workspace IDs are included for support. Do not include API keys or payment secrets.</p>
      <p class="error" role="alert"></p><p role="status" class="small"></p>
      <button type="submit" class="button primary"><i data-lucide="send"></i>Send feedback</button></form>`;
    document.body.append(dialog);
    const current = dialog;
    current.querySelector('[aria-label="Close help"]').addEventListener("click", () => current.close());
    const form = current.querySelector("form");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      if (button.disabled || !supportKey) return;
      const email = form.querySelector("input").value.trim();
      const message = form.querySelector("textarea").value.trim();
      const error = form.querySelector('[role="alert"]');
      const status = form.querySelector('[role="status"]');
      error.textContent = "";
      if (!message) { error.textContent = "Please add a message."; return; }
      if (/sk-tr-v1-/i.test(email + message)) { error.textContent = "Remove API keys before sending."; return; }
      button.disabled = true;
      status.textContent = "Sending feedback";
      try {
        const response = await fetch("/api/feedback", { method: "POST",
          headers: { Authorization: "Bearer " + supportKey, "Content-Type": "application/json" },
          body: JSON.stringify({ email, message }), cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(40000) });
        const result = await response.json();
        if (!response.ok || result.sent !== true) {
          const errors = { funded_key_required: "Connect a funded API key to send feedback.", invalid_api_key: "Reconnect your funded API key.",
            rate_limited: "Too many messages. Please wait 15 minutes before trying again.", remove_api_keys: "Remove API keys before sending.", invalid_feedback: "Enter a valid email and message." };
          throw new Error(errors[result.error] || "Feedback could not be delivered. Please retry or email support@trustedrouter.com.");
        }
        form.reset(); status.textContent = "Feedback sent. We will reply by email.";
      } catch (failure) { status.textContent = ""; error.textContent = failure.message; }
      finally { button.disabled = false; }
    });
    icons();
  }
  dialog.showModal();
});
