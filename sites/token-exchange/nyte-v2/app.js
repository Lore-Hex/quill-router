(() => {
'use strict';
const nav=document.getElementById('nav'), menu=document.getElementById('menuBtn');
menu.addEventListener('click',()=>{const open=nav.classList.toggle('open');menu.setAttribute('aria-expanded',String(open));});
document.addEventListener('keydown',e=>{if(e.key==='Escape' && nav.classList.contains('open')){nav.classList.remove('open');menu.setAttribute('aria-expanded','false');menu.focus();}});
document.addEventListener('click',e=>{if(!e.target.closest('header')){nav.classList.remove('open');menu.setAttribute('aria-expanded','false');}});
const active={home:'/',api:'/confidential',cowork:'/trusted-cowork',sup:'/suppliers',providers:'/providers',index:'/index'}[document.body.dataset.nytePage];
document.querySelectorAll('nav a').forEach(a=>{if(a.getAttribute('href')===active){a.setAttribute('aria-current','page');a.classList.add('on');}});
const dialog=document.getElementById('m-deck');
document.querySelectorAll('[data-open="deck"]').forEach(b=>b.addEventListener('click',()=>dialog.showModal()));
dialog?.querySelector('[data-close]')?.addEventListener('click',()=>dialog.close());
dialog?.addEventListener('click',e=>{if(e.target===dialog)dialog.close();});
const video=document.getElementById('filmVid');
document.getElementById('filmPlay')?.addEventListener('click',()=>{video.controls=true;video.play().catch(()=>{document.getElementById('film').classList.remove('playing');});});
video?.addEventListener('playing',()=>{document.getElementById('film').classList.add('playing');window.TokenExchangeAnalytics?.track('nyte.video_played');},{once:true});
const ctas={'Download for Apple silicon':'download_apple_silicon','Download for Intel Mac':'download_intel_mac','Start building':'api_quick_start','Get the buyer deck':'buyer_deck','Get the provider deck':'provider_deck','Get your API key':'api_key','Read documentation':'documentation','Apply as a provider':'apply_provider','List spare capacity':'spare_capacity','Get the open-source stack':'open_source_stack','Operator guide':'operator_guide','Explore the index':'index'};
document.addEventListener('click',e=>{
 const el=e.target.closest('a,button');if(!el)return;
 const cta=ctas[el.textContent.trim()];
 const position=el.closest('header')?'header':el.closest('.close-photo')?'close':el.closest('[class^="hero"]')?'hero':'section';
 if(cta)window.TokenExchangeAnalytics?.track('nyte.cta_clicked',{cta,position});
 if(el.tagName==='A'){const url=new URL(el.href,location.href);if(['trustedrouter.com','github.com'].includes(url.hostname))window.TokenExchangeAnalytics?.track('nyte.outbound_clicked',{destination_host:url.hostname,cta:cta||'source'});}
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
    button.textContent = 'Preparing your buyer deck…';
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
        body: JSON.stringify({email: email.value.trim(), website: form.elements.website.value, resource: 'brochure',
          campaign: {}, ...window.TokenExchangeAnalytics?.context()}),
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
      download.download = 'NYTE-Buyer-Deck.pdf';
      document.body.appendChild(download);
      download.click();
      download.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      status.dataset.state = 'success';
      window.TokenExchangeAnalytics?.track('nyte.form_submitted', {form:'buyer_deck'});
      status.textContent = "Your download has started. Share the buyer deck with your team. We'd be happy to discuss your requirements.";
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
  nav.addEventListener('focusin', event => {
    if (event.target.matches('a')) event.target.scrollIntoView({block:'nearest', inline:'nearest'});
  });
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

/* Read the public catalogue at runtime; never substitute a build-time snapshot. */
(() => {
  'use strict';
  const models = document.querySelector('[data-catalog-models]');
  const providers = document.querySelector('[data-catalog-providers]');
  if (!models || !providers) return;
  const interval = 60000;
  const count = value => Number.isSafeInteger(value) && value >= 0;
  const clear = element => { element.textContent = '—'; };
  const read = async (endpoint, element, select) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(`https://trustedrouter.com/v1/${endpoint}`, {
        mode: 'cors', credentials: 'omit', cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) throw new Error('Catalogue unavailable');
      const value = select(await response.json());
      if (!count(value)) throw new Error('Invalid catalogue count');
      element.textContent = value.toLocaleString('en-US');
    } catch {
      clear(element);
    } finally {
      clearTimeout(timeout);
    }
  };
  let busy = false;
  let expires = 0;
  const refresh = async () => {
    if (busy) return;
    busy = true;
    await Promise.all([
      read('models/count', models, payload => payload?.data?.count),
      read('providers', providers, payload => {
        if (!Array.isArray(payload?.data) || payload.data.some(provider =>
          !provider || typeof provider.id !== 'string' ||
          !['active', 'blocked'].includes(provider.routing_status))) return NaN;
        return new Set(payload.data.filter(provider => provider.routing_status === 'active')
          .map(provider => provider.id)).size;
      }),
    ]);
    expires = Date.now() + interval;
    busy = false;
  };
  const expire = () => {
    if (Date.now() >= expires) { clear(models); clear(providers); }
  };
  setInterval(() => { expire(); if (!document.hidden) void refresh(); }, interval);
  document.addEventListener('visibilitychange', () => {
    expire();
    if (!document.hidden) void refresh();
  });
  void refresh();
})();
