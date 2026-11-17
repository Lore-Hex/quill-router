"use strict";

// Copy buttons that name their text with data-copy-prompt-target and their
// status line with data-copy-prompt-status. The copy itself is the shared
// one (window.TRCode in content-layout.js); these buttons keep their own
// label and wording. Buttons already wired by the shared code block (a
// prompt panel's Copy in its header strip) are skipped.
document.querySelectorAll("[data-copy-prompt-target]").forEach((button) => {
  const prompt = document.getElementById(button.dataset.copyPromptTarget);
  const status = document.getElementById(button.dataset.copyPromptStatus);
  if (!prompt || !status || !window.TRCode || "copyWired" in button.dataset) return;

  button.hidden = false;
  window.TRCode.wireCopy(button, {
    source: prompt,
    status,
    // A button may name its own confirmation; prompts keep the agent wording.
    done: button.dataset.copyPromptDone || "Copied. Ready for your agent chat.",
    fallback: "Prompt selected. Use your device's copy command.",
    keepLabel: true,
    disableWhileCopying: true,
  });
});
