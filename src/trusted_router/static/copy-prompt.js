"use strict";

document.querySelectorAll("[data-copy-prompt-target]").forEach((button) => {
  const prompt = document.getElementById(button.dataset.copyPromptTarget);
  const status = document.getElementById(button.dataset.copyPromptStatus);
  if (!button || !prompt || !status) return;

  button.hidden = false;
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "";
    try {
      await navigator.clipboard.writeText(prompt.textContent.trim());
      status.textContent = "Copied. Ready for your agent chat.";
    } catch {
      const selection = window.getSelection();
      if (selection) {
        const range = document.createRange();
        range.selectNodeContents(prompt);
        selection.removeAllRanges();
        selection.addRange(range);
        status.textContent = "Prompt selected. Use your device's copy command.";
      } else {
        status.textContent = "Clipboard unavailable. You can select and copy the prompt.";
      }
    } finally {
      button.disabled = false;
    }
  });
});
