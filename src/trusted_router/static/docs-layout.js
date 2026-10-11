// Repeat column labels visually on phones while retaining table semantics.
(() => {
  "use strict";
  document.querySelectorAll(".docs-page table").forEach((table) => {
    if (table.querySelector("[colspan], [rowspan], table")) return;
    const rows = [...table.tBodies].flatMap((body) => [...body.rows]);
    if (!rows.length) return;
    const headers = table.tHead?.rows.length === 1 ? [...table.tHead.rows[0].cells] : [];
    const columns = headers.length > 0 && headers.every((cell) => cell.tagName === "TH")
      && rows.every((row) => row.cells.length === headers.length);
    const rowHeaders = !table.tHead && rows.every((row) => row.cells.length === 2
      && row.cells[0].tagName === "TH" && row.cells[1].tagName === "TD");
    if (!columns && !rowHeaders) return;
    table.classList.add("docs-mobile-table");
    table.setAttribute("role", "table");
    [...table.tBodies, ...(table.tHead ? [table.tHead] : [])].forEach((group) => group.setAttribute("role", "rowgroup"));
    [...table.rows].forEach((row) => {
      row.setAttribute("role", "row");
      [...row.cells].forEach((cell) => cell.setAttribute("role", cell.tagName === "TH"
        ? (row.parentElement === table.tHead ? "columnheader" : "rowheader") : "cell"));
    });
    if (columns) {
      table.classList.add("docs-column-table");
      headers.forEach((header) => { header.scope = "col"; });
      rows.forEach((row) => [...row.cells].forEach((cell, index) => {
        const label = document.createElement("span");
        label.className = "docs-cell-label";
        label.setAttribute("aria-hidden", "true");
        label.textContent = headers[index].textContent.trim();
        cell.prepend(label);
      }));
    } else {
      rows.forEach((row) => { row.cells[0].scope = "row"; });
    }
  });
})();
