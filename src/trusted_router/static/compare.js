"use strict";

// Sideways chip rows: the fade and arrow show only while more chips sit
// off to the right. The arrow scrolls the row for mouse users.
document.querySelectorAll(".compare-chips").forEach((row) => {
  const track = row.querySelector(".compare-chip-track");
  const nudge = row.querySelector(".compare-chip-nudge");
  if (!track) return;
  const update = () => {
    const more = track.scrollLeft + track.clientWidth < track.scrollWidth - 2;
    row.toggleAttribute("data-more", more);
  };
  row.setAttribute("data-ready", "");
  update();
  track.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
  nudge?.addEventListener("click", () => {
    track.scrollBy({ left: track.clientWidth * 0.8, behavior: "smooth" });
  });
});

// Quickstart box: the same tabs, copy button and phone prompt fold as the
// model page. Without script every panel and the full prompt show.
document.querySelectorAll("[data-md-code]").forEach((code) => {
  const tabs = Array.from(code.querySelectorAll("[data-md-tab]"));
  const panels = Array.from(code.querySelectorAll("[data-md-panel]"));
  const copy = code.querySelector("[data-md-copy]");
  const status = code.querySelector("[data-md-copy-status]");
  const expand = code.querySelector("[data-md-expand]");
  const phone = window.matchMedia("(max-width: 639px)");
  let active = code.dataset.mdFirst || "agent";

  const activePanel = () => panels.find((panel) => panel.dataset.mdPanel === active);

  const syncFold = () => {
    if (!expand) return;
    const isPrompt = active === "agent";
    const expanded = code.classList.contains("md-expanded");
    code.classList.add("md-collapsible");
    const body = code.querySelector(".md-code-body");
    const overflows = body && phone.matches && body.scrollHeight > body.clientHeight + 4;
    expand.hidden = !(isPrompt && phone.matches && (overflows || expanded));
    if (!isPrompt || !phone.matches) code.classList.add("md-expanded");
  };

  const select = (name, focus) => {
    active = name;
    tabs.forEach((tab) => {
      const on = tab.dataset.mdTab === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      if (on && focus) tab.focus();
    });
    panels.forEach((panel) => {
      if (panel.dataset.mdPanel === name) panel.removeAttribute("data-md-inactive");
      else panel.setAttribute("data-md-inactive", "");
    });
    if (name === "agent" && expand && expand.getAttribute("aria-expanded") !== "true") {
      code.classList.remove("md-expanded");
    }
    syncFold();
  };

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab.dataset.mdTab, false));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      select(tabs[(index + step + tabs.length) % tabs.length].dataset.mdTab, true);
    });
  });

  if (expand) {
    expand.addEventListener("click", () => {
      const open = expand.getAttribute("aria-expanded") !== "true";
      expand.setAttribute("aria-expanded", open ? "true" : "false");
      expand.setAttribute("aria-label", open ? "Show less" : "Show full prompt");
      code.classList.toggle("md-expanded", open);
      syncFold();
    });
    phone.addEventListener("change", () => {
      if (phone.matches && expand.getAttribute("aria-expanded") !== "true") code.classList.remove("md-expanded");
      syncFold();
    });
  }

  if (copy && status) {
    copy.hidden = false;
    copy.addEventListener("click", async () => {
      const source = activePanel()?.querySelector("code");
      if (!source) return;
      try {
        await navigator.clipboard.writeText(source.textContent.trim());
        status.textContent = "Copied.";
        copy.setAttribute("data-copied", "");
        window.setTimeout(() => copy.removeAttribute("data-copied"), 1600);
      } catch {
        const selection = window.getSelection();
        if (selection) {
          const range = document.createRange();
          range.selectNodeContents(source);
          selection.removeAllRanges();
          selection.addRange(range);
          status.textContent = "Text selected. Use your device's copy command.";
        }
      }
    });
  }

  select(active, false);
});
