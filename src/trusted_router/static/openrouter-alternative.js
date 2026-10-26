/* Visual, keyboard-accessible cues appear only when a table overflows. */
(() => {
  document.querySelectorAll('.openrouter-article .model-table-wrap, .openrouter-article .seo-evidence-table-wrap').forEach((viewport, index) => {
    viewport.id ||= `comparison-scroll-${index}`;
    const tools = document.createElement('div');
    tools.className = 'table-scroll-tools';
    tools.hidden = true;
    const track = document.createElement('div');
    track.className = 'table-scroll-track';
    track.setAttribute('aria-hidden', 'true');
    const thumb = document.createElement('div');
    thumb.className = 'table-scroll-thumb';
    track.append(thumb);
    const makeButton = (direction) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('aria-controls', viewport.id);
      button.className = 'table-scroll-button';
      button.setAttribute('aria-label', direction < 0 ? 'Previous table columns' : 'Next table columns');
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('viewBox', '0 0 24 24');
      icon.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', direction < 0 ? 'M15 5l-7 7 7 7' : 'M9 5l7 7-7 7');
      icon.append(path);
      button.append(icon);
      button.addEventListener('click', () => viewport.scrollBy({
        left: direction * viewport.clientWidth * .75,
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
      }));
      return button;
    };
    const previous = makeButton(-1);
    const next = makeButton(1);
    tools.append(track, previous, next);
    viewport.before(tools);
    const update = () => {
      const max = viewport.scrollWidth - viewport.clientWidth;
      tools.hidden = max <= 1;
      previous.disabled = viewport.scrollLeft <= 1;
      next.disabled = viewport.scrollLeft >= max - 1;
      thumb.style.width = `${100 * viewport.clientWidth / viewport.scrollWidth}%`;
      thumb.style.left = `${100 * viewport.scrollLeft / viewport.scrollWidth}%`;
      if (max > 1) {
        viewport.tabIndex = 0;
        viewport.setAttribute('role', 'region');
        viewport.setAttribute('aria-label', viewport.querySelector('caption')?.textContent.trim() || 'Catalog comparison table');
      } else {
        viewport.removeAttribute('tabindex');
        viewport.removeAttribute('role');
        viewport.removeAttribute('aria-label');
      }
    };
    viewport.addEventListener('scroll', update, { passive: true });
    const observer = new ResizeObserver(update);
    observer.observe(viewport);
    observer.observe(viewport.querySelector('table'));
    update();
  });
})();
