/* Wide comparison tables get the article's scrollbar styling; the sideways
   fade comes from the shared one (window.TRScroll, content-layout.js). */
document.querySelectorAll('.openrouter-article .model-table-wrap, .openrouter-article .seo-evidence-table-wrap').forEach((viewport) => {
  viewport.classList.add('horizontal-overflow');
});
