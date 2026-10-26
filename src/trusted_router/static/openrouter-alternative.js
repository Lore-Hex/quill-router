/* Native scrolling with edge fades; no extra navigation controls. */
(() => {
  document.querySelectorAll('.openrouter-article .model-table-wrap, .openrouter-article .seo-evidence-table-wrap, .openrouter-article .copy-terminal pre').forEach((viewport) => {
    const frame = document.createElement('div');
    frame.className = 'horizontal-overflow-frame';
    viewport.before(frame);
    frame.append(viewport);
    viewport.classList.add('horizontal-overflow');
    const update = () => {
      const max = viewport.scrollWidth - viewport.clientWidth;
      viewport.classList.toggle('can-scroll-left', viewport.scrollLeft > 1);
      viewport.classList.toggle('can-scroll-right', viewport.scrollLeft < max - 1);
      if (max > 1) {
        viewport.tabIndex = 0;
        viewport.setAttribute('role', 'region');
        viewport.setAttribute('aria-label', viewport.matches('pre')
          ? 'OpenAI SDK migration example'
          : viewport.querySelector('caption')?.textContent.trim() || 'Catalog comparison table');
      } else {
        viewport.removeAttribute('tabindex');
        viewport.removeAttribute('role');
        viewport.removeAttribute('aria-label');
      }
    };
    viewport.addEventListener('keydown', (event) => {
      if (event.target !== viewport || event.altKey || event.ctrlKey || event.metaKey
        || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      viewport.scrollBy({ left: event.key === 'ArrowRight' ? 40 : -40, behavior: 'instant' });
    });
    viewport.addEventListener('scroll', update, { passive: true });
    const observer = new ResizeObserver(update);
    observer.observe(viewport);
    const content = viewport.querySelector('table, code');
    if (content) observer.observe(content);
    document.fonts.ready.then(update);
    update();
  });
})();
