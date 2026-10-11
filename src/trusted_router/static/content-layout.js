// Repeat column labels visually on phones while retaining table semantics.
(() => {
  "use strict";
  document.querySelectorAll("[data-reference-table]").forEach((table) => {
    if (table.querySelector("[colspan], [rowspan], table")) return;
    const rows = [...table.tBodies].flatMap((body) => [...body.rows]);
    if (!rows.length) return;
    const headers = table.tHead?.rows.length === 1 ? [...table.tHead.rows[0].cells] : [];
    const columns = headers.length > 0 && headers.every((cell) => cell.tagName === "TH")
      && rows.every((row) => row.cells.length === headers.length);
    const rowHeaders = !table.tHead && rows.every((row) => row.cells.length === 2
      && row.cells[0].tagName === "TH" && row.cells[1].tagName === "TD");
    if (!columns && !rowHeaders) return;
    table.classList.add("reference-mobile-table");
    table.setAttribute("role", "table");
    [...table.tBodies, ...(table.tHead ? [table.tHead] : [])].forEach((group) => group.setAttribute("role", "rowgroup"));
    [...table.rows].forEach((row) => {
      row.setAttribute("role", "row");
      [...row.cells].forEach((cell) => cell.setAttribute("role", cell.tagName === "TH"
        ? (row.parentElement === table.tHead ? "columnheader" : "rowheader") : "cell"));
    });
    if (columns) {
      table.classList.add("reference-column-table");
      headers.forEach((header) => { header.scope = "col"; });
      rows.forEach((row) => [...row.cells].forEach((cell, index) => {
        if (index === 0) { cell.classList.add("reference-key"); return; }
        const label = document.createElement("span");
        label.className = "reference-cell-label";
        label.setAttribute("aria-hidden", "true");
        label.textContent = headers[index].textContent.trim();
        cell.prepend(label);
      }));
    } else {
      rows.forEach((row) => { row.cells[0].scope = "row"; });
    }
  });
  // Keep the desktop reference table intact; offer compact, native disclosures on phones.
  document.querySelectorAll("[data-reference-accordion]").forEach((table) => {
    if (table.classList.contains("has-reference-accordion")
      || table.querySelector("[colspan], [rowspan], table")) return;
    const rows = [...table.tBodies].flatMap((body) => [...body.rows]);
    if (!rows.length || !rows.every((row) => row.cells.length === 3
      && row.dataset.purpose?.trim() && row.cells[0].querySelector("code"))) return;
    const cloneContent = (source) => {
      const clone = source.cloneNode(true);
      clone.removeAttribute("id");
      clone.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
      clone.querySelectorAll(".reference-cell-label").forEach((node) => node.remove());
      return [...clone.childNodes];
    };
    const accordion = document.createElement("div");
    accordion.className = "content-reference-accordion";
    rows.forEach((row, index) => {
      const entry = document.createElement("details");
      entry.className = "content-reference-entry";
      entry.open = index === 0;
      const summary = document.createElement("summary");
      const field = document.createElement("code");
      field.append(...cloneContent(row.cells[0].querySelector("code")));
      const purpose = document.createElement("span");
      purpose.className = "content-reference-purpose";
      purpose.textContent = row.dataset.purpose.trim();
      summary.append(field, purpose);
      const body = document.createElement("div");
      body.className = "content-reference-body";
      body.append(...cloneContent(row.cells[1]));
      const values = document.createElement("div");
      values.className = "content-reference-values";
      const label = document.createElement("strong");
      label.textContent = "Values";
      values.append(label, document.createTextNode(" "), ...cloneContent(row.cells[2]));
      body.append(values);
      entry.append(summary, body);
      accordion.append(entry);
    });
    table.after(accordion);
    table.classList.add("has-reference-accordion");
  });
  document.querySelectorAll(".content-section pre").forEach((pre) => {
    const frame = document.createElement("div");
    frame.className = "content-code-frame";
    pre.before(frame);
    frame.append(pre);
    const controls = document.createElement("div");
    controls.className = "content-code-controls";
    controls.hidden = true;
    frame.append(controls);
    const buttons = [-1, 1].map((direction) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "content-code-scroll";
      button.setAttribute("aria-label", `Scroll code ${direction < 0 ? "left" : "right"}`);
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 20 20");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", direction < 0 ? "M12 5 7 10l5 5" : "m8 5 5 5-5 5");
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "1.5");
      path.setAttribute("stroke-linecap", "round");
      path.setAttribute("stroke-linejoin", "round");
      svg.append(path);
      button.append(svg);
      button.addEventListener("click", () => {
        pre.scrollBy({ left: direction * pre.clientWidth * 0.75, behavior: "instant" });
        update();
      });
      controls.append(button);
      return button;
    });
    const update = () => {
      const maxScroll = pre.scrollWidth - pre.clientWidth;
      const overflow = maxScroll > 1;
      controls.hidden = !overflow;
      buttons[0].disabled = pre.scrollLeft <= 1;
      buttons[1].disabled = pre.scrollLeft >= maxScroll - 1;
      pre.classList.toggle("has-overflow", overflow);
      pre.tabIndex = overflow ? 0 : -1;
      pre.setAttribute("aria-label", overflow ? "Code example, scroll horizontally for more" : "Code example");
    };
    pre.addEventListener("scroll", update, { passive: true });
    new ResizeObserver(update).observe(pre);
    document.fonts.ready.then(update);
    update();
  });
})();

// Opt-in section navigation shared by long-form content pages.
(() => {
  "use strict";
  document.querySelectorAll("[data-content-toc]").forEach((layout) => {
    const content = layout.querySelector(".content-document");
    if (!content || layout.querySelector(".content-toc")) return;
    const headings = [...content.querySelectorAll("h2")];
    if (headings.length < 2) return;
    const nav = document.createElement("nav");
    nav.className = "content-toc";
    nav.setAttribute("aria-label", "On this page");
    const title = document.createElement("p");
    title.className = "content-toc-title";
    title.textContent = "On this page";
    const disclosure = document.createElement("details");
    disclosure.className = "content-toc-disclosure";
    const summary = document.createElement("summary");
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 20 20");
    icon.setAttribute("width", "20");
    icon.setAttribute("height", "20");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    const iconPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    iconPath.setAttribute("d", "M3 5h1m3 0h10M3 10h1m3 0h10M3 15h1m3 0h10");
    iconPath.setAttribute("fill", "none");
    iconPath.setAttribute("stroke", "currentColor");
    iconPath.setAttribute("stroke-width", "1.5");
    iconPath.setAttribute("stroke-linecap", "round");
    icon.append(iconPath);
    summary.append(icon, document.createTextNode("Contents"));
    const list = document.createElement("ol");
    const entries = headings.map((heading) => {
      const section = heading.closest(".content-section");
      const target = section && content.contains(section) && section.querySelector("h2") === heading
        ? section : heading;
      if (!target.id) {
        const base = heading.textContent.trim().toLowerCase().normalize("NFKD")
          .replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-")
          .replace(/^-|-$/g, "") || "section";
        let id = base;
        let suffix = 2;
        while (document.getElementById(id)) id = `${base}-${suffix++}`;
        target.id = id;
      }
      heading.tabIndex = -1;
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `#${encodeURIComponent(target.id)}`;
      const longLabel = document.createElement("span");
      longLabel.className = "content-toc-long";
      longLabel.textContent = heading.textContent.trim();
      const shortLabel = document.createElement("span");
      shortLabel.className = "content-toc-short";
      shortLabel.textContent = heading.dataset.tocLabel || longLabel.textContent;
      link.append(longLabel, shortLabel);
      item.append(link);
      list.append(item);
      return { heading, target, link };
    });
    disclosure.append(summary, list);
    nav.append(title, disclosure);
    layout.prepend(nav);
    layout.classList.add("has-content-toc");

    const desktop = window.matchMedia("(min-width: 1400px)");
    const syncDisclosure = () => { disclosure.open = desktop.matches; };
    desktop.addEventListener("change", syncDisclosure);
    syncDisclosure();

    let pending = false;
    const updateCurrent = () => {
      pending = false;
      let active = entries[0];
      for (const entry of entries) {
        if (entry.target.getBoundingClientRect().top <= 100) active = entry;
      }
      entries.forEach((entry) => {
        if (entry === active) entry.link.setAttribute("aria-current", "location");
        else entry.link.removeAttribute("aria-current");
      });
    };
    const scheduleCurrent = () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(updateCurrent);
    };
    const navigate = (entry, focus) => {
      disclosure.open = desktop.matches;
      requestAnimationFrame(() => {
        entry.target.scrollIntoView({ behavior: "instant", block: "start" });
        if (focus) entry.heading.focus({ preventScroll: true });
        updateCurrent();
      });
    };
    entries.forEach((entry) => {
      entry.link.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
          || event.shiftKey || event.altKey) return;
        event.preventDefault();
        const hash = entry.link.hash;
        if (window.location.hash !== hash) window.history.pushState(null, "", hash);
        navigate(entry, true);
      });
    });
    const followHash = (focus = true) => {
      let id;
      try { id = decodeURIComponent(window.location.hash.slice(1)); }
      catch { return; }
      const entry = entries.find((candidate) => candidate.target.id === id);
      if (entry) navigate(entry, focus);
      else scheduleCurrent();
    };
    window.addEventListener("hashchange", () => followHash());
    window.addEventListener("popstate", () => followHash());
    window.addEventListener("scroll", scheduleCurrent, { passive: true });
    window.addEventListener("resize", scheduleCurrent, { passive: true });
    new ResizeObserver(scheduleCurrent).observe(content);
    document.fonts.ready.then(scheduleCurrent);
    updateCurrent();
    followHash(false);
  });
})();
