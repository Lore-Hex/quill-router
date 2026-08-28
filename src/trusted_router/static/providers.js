(() => {
  "use strict";

  const root = document.querySelector("[data-provider-explorer]");
  const input = root?.querySelector("[data-provider-search]");
  const rows = Array.from(document.querySelectorAll("[data-provider-row]"));
  const resultCount = root?.querySelector("[data-provider-result-count]");
  const empty = document.querySelector("[data-provider-empty]");

  if (!root || !input || !resultCount || !empty || rows.length === 0) return;

  const normalize = (value) => value.trim().toLocaleLowerCase();
  const haystacks = new Map(rows.map((row) => [row, normalize(row.textContent || "")]));

  const syncUrl = (query) => {
    const url = new URL(window.location.href);
    if (query) url.searchParams.set("q", query);
    else url.searchParams.delete("q");
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  };

  const applySearch = () => {
    const query = input.value.trim();
    const terms = normalize(query).split(/\s+/).filter(Boolean);
    let visible = 0;

    rows.forEach((row) => {
      const matches = terms.every((term) => haystacks.get(row).includes(term));
      row.hidden = !matches;
      if (matches) visible += 1;
    });

    resultCount.textContent = `${visible} ${visible === 1 ? "entry" : "entries"}`;
    empty.hidden = visible !== 0;
    syncUrl(query);
  };

  const initialQuery = new URL(window.location.href).searchParams.get("q") || "";
  input.value = initialQuery;
  input.addEventListener("input", applySearch);
  applySearch();
})();
