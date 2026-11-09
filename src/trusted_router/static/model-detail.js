"use strict";

// Public model page: the provider table. Each provider is a main row plus a
// detail row. Without script every detail row shows and the toggles stay
// hidden; the script adds .md-js, folds the details and lets the toggle (or a
// click anywhere on the main row) open one.
//
// The sort control: the server renders the rows cheapest first, so without
// script the table keeps that order and the control stays hidden. Speed sorts
// by measured tokens/s (highest first), Latency by measured time to first
// token (lowest first); rows without a measurement go last, in price order.
// Each detail row moves with its main row. The quickstart box and chip row
// are driven by quickstart.js.
(() => {
  const table = document.querySelector("[data-md-providers]");
  if (!table) return;
  const body = table.tBodies[0];
  const rows = Array.from(body.querySelectorAll(":scope > tr.md-provider"));

  const detailOf = (row) => {
    const next = row.nextElementSibling;
    return next && next.classList.contains("md-provider-detail") ? next : null;
  };
  const setOpen = (row, open) => {
    const button = row.querySelector(".md-toggle");
    row.classList.toggle("is-open", open);
    if (button) button.setAttribute("aria-expanded", open ? "true" : "false");
  };

  rows.forEach((row) => {
    setOpen(row, false);
    row.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      const selection = window.getSelection && window.getSelection();
      if (selection && !selection.isCollapsed && row.contains(selection.anchorNode)) return;
      setOpen(row, !row.classList.contains("is-open"));
    });
  });
  table.classList.add("md-js");

  // A link to #provider-<slug> opens that row.
  const target = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
  if (target && rows.includes(target)) setOpen(target, true);

  const control = document.querySelector("[data-md-sort]");
  if (!control) return;
  const buttons = Array.from(control.querySelectorAll("[data-md-sort-key]"));
  if (rows.length < 2 || buttons.length < 2) return;

  const number = (row, key) => {
    const value = row.getAttribute(`data-sort-${key}`);
    return value === null || value === "" ? null : Number(value);
  };
  const byPrice = (a, b) => number(a.row, "price") - number(b.row, "price") || a.index - b.index;
  const indexed = rows.map((row, index) => ({ row, detail: detailOf(row), index }));

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
    ordered.forEach(({ row, detail }) => {
      body.appendChild(row);
      if (detail) body.appendChild(detail);
    });
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
