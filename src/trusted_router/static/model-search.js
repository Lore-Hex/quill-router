(() => {
  "use strict";
  // Search equivalence only. Never use this to select or rewrite an API model ID.
  const compact = value => String(value).normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
  window.TRModelSearch = {
    matches(text, query) {
      const haystack = compact(text);
      return String(query).trim().split(/\s+/).map(compact).filter(Boolean)
        .every(term => haystack.includes(term));
    },
  };
})();
