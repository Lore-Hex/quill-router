"use strict";

// Shared quickstart box (templates/public/_quickstart_box.html and the
// compare kit): tabs and the phone prompt fold for every box on the page.
// Copy uses the shared code block's behaviour (window.TRCode from
// content-layout.js), and the code area and chip row get the shared scroll
// chevrons from content-layout.js. Without script every quickstart panel
// and the full prompt show.
(() => {
  const phone = window.matchMedia("(max-width: 639px)");

  document.querySelectorAll("[data-md-code]").forEach((code) => {
    const tabs = Array.from(code.querySelectorAll("[data-md-tab]"));
    const panels = Array.from(code.querySelectorAll("[data-md-panel]"));
    const copy = code.querySelector("[data-md-copy]");
    const status = code.querySelector("[data-md-copy-status]");
    const expand = code.querySelector("[data-md-expand]");
    const body = code.querySelector(".md-code-body");
    if (!tabs.length) return;
    let active = code.dataset.mdFirst || tabs[0].dataset.mdTab;

    const activePanel = () => panels.find((panel) => panel.dataset.mdPanel === active);

    const syncFold = () => {
      if (!expand) return;
      const isPrompt = active === "agent";
      const expanded = code.classList.contains("md-expanded");
      code.classList.add("md-collapsible");
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

    if (copy && status && window.TRCode) {
      copy.hidden = false;
      window.TRCode.wireCopy(copy, {
        // A panel holding a before/after pair copies its marked "after" side.
        source: () => {
          const panel = activePanel();
          return (panel?.querySelector("[data-md-copy-source]") || panel)?.querySelector("code");
        },
        status,
        fallback: "Text selected. Use your device's copy command.",
        resetAfter: 1600,
      });
    }

    select(active, false);
  });
})();
