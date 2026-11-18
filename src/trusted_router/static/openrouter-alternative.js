/* Wide comparison tables scroll sideways with the shared soft edge fade
   (window.TRScroll, content-layout.js). Code panels get theirs from the
   shared code block. */
(() => {
  document.querySelectorAll('.openrouter-article .model-table-wrap, .openrouter-article .seo-evidence-table-wrap').forEach((viewport) => {
    viewport.classList.add('horizontal-overflow');
    if (!window.TRScroll) return;
    window.TRScroll.attach(viewport, {
      place: 'over',
      label: 'table',
      focusLabel: viewport.querySelector('caption')?.textContent.trim() || 'Catalog comparison table',
    });
  });
})();
