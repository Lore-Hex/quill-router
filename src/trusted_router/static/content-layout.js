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
