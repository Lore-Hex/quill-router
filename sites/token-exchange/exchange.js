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

/* Keep the initial mobile composition when browser chrome expands/collapses.
   Recalculate on width changes (including rotation), never on scroll/height alone. */
(() => {
  const hero = document.querySelector('.hero');
  const copy = hero?.querySelector('.hero-copy');
  const actions = hero?.querySelector('.actions');
  if (!hero || !copy || !actions) return;
  let previousWidth;
  const sizeHero = () => {
    const width = document.documentElement.clientWidth;
    if (width === previousWidth) return;
    previousWidth = width;
    hero.style.removeProperty('--mobile-hero-top');
    hero.style.removeProperty('--mobile-hero-gap');
    hero.classList.remove('hero-layout-locked', 'hero-compact');
    if (width > 600) return;
    const top = getComputedStyle(copy).paddingTop;
    const gap = getComputedStyle(actions).marginTop;
    hero.style.setProperty('--mobile-hero-top', top);
    hero.style.setProperty('--mobile-hero-gap', gap);
    hero.classList.toggle('hero-compact', window.matchMedia('(max-height: 650px)').matches);
    hero.classList.add('hero-layout-locked');
  };
  sizeHero();
  window.addEventListener('resize', sizeHero);
})();

/* Initialize buyer highlights on entry; the underlying diagram stays visible. */
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
  document.querySelectorAll('.route-flow').forEach(element => observer.observe(element));
})();

/* Decorative loops run only while visible; static artwork remains without JS. */
(() => {
  if (!('IntersectionObserver' in window)) return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const visible = new Map();
  const sync = () => visible.forEach((inView, element) => {
    element.classList.toggle('is-animating', inView && !reduced.matches && !document.hidden);
  });
  reduced.addEventListener('change', sync);
  document.addEventListener('visibilitychange', sync);
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => visible.set(entry.target, entry.isIntersecting));
    sync();
  });
  document.querySelectorAll('.hero, .route-flow, .supplier-art, .closing').forEach(element => observer.observe(element));
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
