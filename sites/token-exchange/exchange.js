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

/* Progressive enhancement: navigation stays visible if JavaScript is unavailable. */
(() => {
  const header = document.querySelector('.masthead');
  const toggle = document.querySelector('.menu-toggle');
  const nav = document.querySelector('#primary-navigation');
  if (!header || !toggle || !nav) return;
  const setOpen = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    header.classList.toggle('menu-open', open);
  };
  toggle.hidden = false;
  header.classList.add('nav-ready');
  toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
  nav.addEventListener('click', (event) => {
    if (event.target.closest('a')) setOpen(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') {
      setOpen(false);
      toggle.focus();
    }
  });
  document.addEventListener('click', (event) => {
    if (!header.contains(event.target)) setOpen(false);
  });
  window.matchMedia('(max-width: 600px)').addEventListener('change', () => setOpen(false));
})();

/* Illustrative paths draw once on entry; all content remains visible without JS. */
(() => {
  if (!('IntersectionObserver' in window)) return;
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      }
    });
  }, {threshold: 0.35});
  document.querySelectorAll('.route-flow, .supplier-art').forEach(element => observer.observe(element));
})();

/* Decorative color motion stops outside the viewport and respects reduced motion. */
(() => {
  const closing = document.querySelector('.closing');
  if (!closing || !('IntersectionObserver' in window)) return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  let visible = false;
  const sync = () => closing.classList.toggle('is-animating', visible && !reduced.matches && !document.hidden);
  reduced.addEventListener('change', sync);
  document.addEventListener('visibilitychange', sync);
  new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting;
    sync();
  }).observe(closing);
})();

/* Show directional controls only when more market links are out of view. */
document.querySelectorAll('.geo-scroll').forEach(container => {
  const nav = container.querySelector('nav, [data-geo-links]');
  const previous = container.querySelector('.geo-prev');
  const next = container.querySelector('.geo-next');
  const update = () => {
    const left = nav.scrollLeft > 2;
    const right = nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 2;
    previous.hidden = !left;
    next.hidden = !right;
    container.classList.toggle('can-left', left);
    container.classList.toggle('can-right', right);
  };
  const move = direction => nav.scrollBy({
    left: direction * nav.clientWidth * .75,
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'
  });
  previous.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  nav.addEventListener('scroll', update, {passive:true});
  window.addEventListener('resize', update);
  if ('ResizeObserver' in window) new ResizeObserver(update).observe(nav);
  document.fonts.ready.then(update);
  update();
});
