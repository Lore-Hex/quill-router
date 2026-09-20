/* Carry campaign attribution to existing TrustedRouter intake, without cookies or pixels. */
(() => {
  const incoming = new URLSearchParams(window.location.search);
  const allowed = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'];
  document.querySelectorAll('a[data-attribution]').forEach((link) => {
    const destination = new URL(link.href);
    allowed.forEach((key) => {
      const value = incoming.get(key);
      if (value && value.length <= 200) destination.searchParams.set(key, value);
    });
    link.href = destination.href;
  });
})();
