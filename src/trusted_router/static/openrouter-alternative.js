/* Wide comparison tables scroll sideways with the shared scroll chevrons
   (window.TRScroll, content-layout.js) in a bar above each table. Code
   panels get theirs from the shared code block. */
(() => {
  document.querySelectorAll('.openrouter-article .model-table-wrap, .openrouter-article .seo-evidence-table-wrap').forEach((viewport) => {
    viewport.classList.add('horizontal-overflow');
    if (!window.TRScroll) return;
    const bar = document.createElement('div');
    bar.className = 'tr-scroll-bar';
    viewport.before(bar);
    window.TRScroll.attach(viewport, {
      place: 'bar',
      bar,
      label: 'table',
      focusLabel: viewport.querySelector('caption')?.textContent.trim() || 'Catalog comparison table',
    });
  });
})();
