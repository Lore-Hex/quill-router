/* Carry campaign attribution to existing TrustedRouter intake, without cookies or pixels. */
const campaignFields = (() => {
  const incoming = new URLSearchParams(window.location.search);
  const fields = {};
  ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'].forEach((key) => {
    const value = incoming.get(key);
    if (value && value.length <= 128) fields[key] = value;
  });
  return fields;
})();
document.querySelectorAll('a[data-attribution]').forEach((link) => {
  const destination = new URL(link.href);
  Object.entries(campaignFields).forEach(([key, value]) => destination.searchParams.set(key, value));
  link.href = destination.href;
});

/* Progressive enhancement: navigation stays visible if JavaScript is unavailable. */
(() => {
  const header = document.querySelector('.masthead');
  const toggle = document.querySelector('.menu-toggle');
  const nav = document.querySelector('#primary-navigation');
  if (!header || !toggle || !nav) return;
  const backdrop = document.createElement('div');
  backdrop.className = 'navigation-backdrop';
  backdrop.hidden = true;
  backdrop.setAttribute('aria-hidden', 'true');
  header.after(backdrop);
  const setOpen = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    header.classList.toggle('menu-open', open);
    backdrop.hidden = !open;
    if (!open) nav.querySelectorAll('details[open]').forEach(details => { details.open = false; });
  };
  toggle.hidden = false;
  backdrop.addEventListener('click', (event) => {
    event.preventDefault();
    setOpen(false);
    toggle.focus({preventScroll: true});
  });
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
  document.addEventListener('pointerdown', (event) => {
    if (event.target !== backdrop && !header.contains(event.target)) setOpen(false);
  }, {capture: true});
  document.addEventListener('focusin', (event) => {
    if (!header.contains(event.target)) setOpen(false);
  });
  window.matchMedia('(max-width: 1100px)').addEventListener('change', () => setOpen(false));
})();

/* Fill the first screen, but do not shift the document when mobile chrome
   changes height while the reader is farther down the page. */
(() => {
  const header = document.querySelector('.masthead');
  if (!header) return;
  let previousWidth;
  const lockedStyles = [
    ['.hero-copy', ['paddingTop', 'paddingBottom']],
    ['.hero-copy .actions', ['marginTop']],
    ['.hero h1', ['fontSize']],
    ['.hero .lead', ['fontSize', 'lineHeight', 'marginTop']],
  ];
  const measure = () => {
    const width = document.documentElement.clientWidth;
    if (previousWidth !== width || width > 900 || window.scrollY <= 1) {
      document.documentElement.style.setProperty('--hero-viewport', `${window.innerHeight}px`);
    }
    if (previousWidth !== width) {
      lockedStyles.forEach(([selector, properties]) => {
        const element = document.querySelector(selector);
        if (!element) return;
        properties.forEach(property => { element.style[property] = ''; });
        if (width <= 600) {
          const computed = getComputedStyle(element);
          const values = properties.map(property => computed[property]);
          properties.forEach((property, index) => { element.style[property] = values[index]; });
        }
      });
    }
    previousWidth = width;
    document.documentElement.style.setProperty('--header-height',
      `${header.getBoundingClientRect().height}px`);
  };
  measure();
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(measure);
    observer.observe(header);
  }
  window.addEventListener('resize', measure);
  window.addEventListener('scroll', () => { if (window.scrollY <= 1) measure(); }, {passive:true});
  document.fonts.ready.then(measure);
})();

/* Foreground reveals play once per visit; content stays visible without JS. */
(() => {
  if (!('IntersectionObserver' in window)) return;
  const reveal = (elements, threshold) => {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    }, {threshold});
    elements.forEach(element => observer.observe(element));
  };
  reveal(document.querySelectorAll('.evidence-reveal, .privacy-column'), 0.35);
  const foreground = document.querySelectorAll(
    '.hero-copy, .market-intro, .sectors > article, #buyers .section-intro, ' +
    '.trust-intro, .supplier-art, .seller-intro, ' +
    '.supplier-cards > article, .closing-copy'
  );
  foreground.forEach(element => element.classList.add('scroll-reveal'));
  reveal(foreground, 0.15);
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
  document.querySelectorAll('.hero, .supplier-art').forEach(element => observer.observe(element));
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
  document.fonts.ready.then(() => {
    const current = nav.querySelector('[aria-current="page"]');
    if (current && nav.scrollWidth > nav.clientWidth) {
      const link = current.getBoundingClientRect();
      const viewport = nav.getBoundingClientRect();
      nav.scrollLeft += link.left - viewport.left - (nav.clientWidth - link.width) / 2;
    }
    update();
  });
  update();
});

/* Market navigation lives inside the primary menu, with its own dismissal. */
(() => {
  const picker = document.querySelector('.market-picker');
  if (!picker) return;
  document.addEventListener('click', event => {
    if (!picker.contains(event.target)) picker.open = false;
  });
  picker.addEventListener('keydown', event => {
    if (event.key === 'Escape' && picker.open) {
      event.stopPropagation();
      picker.open = false;
      picker.querySelector('summary').focus();
    }
  });
})();

/* The brochure form is TrustedRouter's email-gated download, posted there from this
   domain; the server allows the exchange origins. Without JavaScript the button stays
   disabled and the noscript line offers email instead. */
(() => {
  const form = document.getElementById('brochure-form');
  if (!form || !window.fetch || !window.AbortController) return;
  const button = form.querySelector('button[type=submit]');
  const email = form.querySelector('input[name=email]');
  const status = document.getElementById('brochure-status');
  const label = button.innerHTML;
  const contact = 'enterprise@trustedrouter.com';
  let busy = false;
  button.disabled = false;
  email.addEventListener('input', () => email.removeAttribute('aria-invalid'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    busy = true;
    // Disabling the focused button drops focus to the page; it is restored afterwards.
    const refocus = document.activeElement === button;
    button.disabled = true;
    button.textContent = 'Preparing your brochure…';
    form.setAttribute('aria-busy', 'true');
    status.textContent = '';
    status.dataset.state = 'loading';
    email.removeAttribute('aria-invalid');
    let timeout;
    try {
      const controller = new AbortController();
      timeout = setTimeout(() => controller.abort(), 30000);
      const response = await fetch(form.action, {
        method: 'POST',
        mode: 'cors',
        credentials: 'omit',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email: email.value.trim(), website: form.elements.website.value, resource: 'brochure', campaign: campaignFields}),
        signal: controller.signal,
      });
      if (!response.ok) {
        const errors = {
          422: 'Please enter a valid email address.',
          429: `You've reached the download limit. Please try later or email ${contact}.`,
        };
        if (response.status === 422) email.setAttribute('aria-invalid', 'true');
        throw new Error(errors[response.status] || `Something went wrong. Try again or email ${contact}.`);
      }
      if (!(response.headers.get('content-type') || '').includes('application/pdf')) {
        throw new Error(`Please refresh the page and try again, or email ${contact}.`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const download = document.createElement('a');
      download.href = url;
      download.download = 'TrustedRouter-Token-Exchange-Brochure.pdf';
      document.body.appendChild(download);
      download.click();
      download.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      status.dataset.state = 'success';
      status.textContent = "Your download has started. Share the brochure with your team. We'd be happy to discuss your requirements.";
    } catch (error) {
      status.dataset.state = 'error';
      status.textContent = error.name === 'AbortError'
        ? `This is taking longer than expected. Please try again or email ${contact}.`
        : error instanceof TypeError
          ? `Something went wrong. Try again or email ${contact}.`
          : error.message;
    } finally {
      clearTimeout(timeout);
      busy = false;
      button.disabled = false;
      button.innerHTML = label;
      form.removeAttribute('aria-busy');
      if (refocus) button.focus();
    }
  });
})();
