// Keep cached trust pages working during a rolling asset deployment.
(() => {
  const button = document.getElementById("copy-trust-prompt");
  if (!button) return;
  button.dataset.copyPromptTarget = "trust-agent-prompt";
  button.dataset.copyPromptStatus = "trust-copy-status";
  void import("./copy-prompt.js");
})();
