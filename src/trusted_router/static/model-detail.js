"use strict";

// Public model page: the provider sort control. The server renders the rows
// cheapest first, so without script the list keeps that order and the
// control stays hidden. Speed sorts by measured tokens/s (highest first),
// Latency by measured time to first token (lowest first); rows without a
// measurement go last, in price order. The quickstart box and chip row are
// driven by quickstart.js.
(() => {
  const control = document.querySelector("[data-md-sort]");
  const list = document.querySelector(".md-providers");
  if (!control || !list) return;
  const rows = Array.from(list.querySelectorAll(":scope > .md-provider"));
  const buttons = Array.from(control.querySelectorAll("[data-md-sort-key]"));
  if (rows.length < 2 || buttons.length < 2) return;

  const number = (row, key) => {
    const value = row.getAttribute(`data-sort-${key}`);
    return value === null || value === "" ? null : Number(value);
  };
  const byPrice = (a, b) => number(a.row, "price") - number(b.row, "price") || a.index - b.index;
  const indexed = rows.map((row, index) => ({ row, index }));

  const sortBy = (key) => {
    const ordered = indexed.slice().sort((a, b) => {
      if (key === "price") return byPrice(a, b);
      const left = number(a.row, key);
      const right = number(b.row, key);
      if (left === null || right === null) {
        return left === right ? byPrice(a, b) : left === null ? 1 : -1;
      }
      const diff = key === "throughput" ? right - left : left - right;
      return diff || byPrice(a, b);
    });
    ordered.forEach(({ row }) => list.appendChild(row));
  };

  const select = (button, focus) => {
    buttons.forEach((other) => {
      const on = other === button;
      other.setAttribute("aria-checked", on ? "true" : "false");
      other.tabIndex = on ? 0 : -1;
    });
    if (focus) button.focus();
    sortBy(button.dataset.mdSortKey);
  };

  buttons.forEach((button, index) => {
    button.addEventListener("click", () => select(button, false));
    button.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      select(buttons[(index + step + buttons.length) % buttons.length], true);
    });
  });
  control.hidden = false;
})();
