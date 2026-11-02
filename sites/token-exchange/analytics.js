/* First-party, metadata-only analytics shared by every Token Exchange site.
   No cookies, ad pixels or content capture. A random visitor ID lives 90 days in this
   browser and travels only on links to trustedrouter.com and sister exchange sites. */
(() => {
  'use strict';
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const PAGE = /^\/[a-z0-9][a-z0-9-]{0,39}$/;
  const VISITOR_MS = 90 * 86400000, VISIT_MS = 30 * 60000;
  const now = Date.now();
  const query = new URLSearchParams(location.search);
  // Storage is optional: a blocked store only shortens memory to this page.
  const area = name => name === 'local' ? localStorage : sessionStorage;
  const read = (name, key) => { try { return JSON.parse(area(name).getItem(key) || 'null'); } catch { return null; } };
  const write = (name, key, value) => { try { if (value === null) area(name).removeItem(key); else area(name).setItem(key, JSON.stringify(value)); } catch { /* optional */ } };
  // ?tx_internal=1 marks this browser as the team's on this domain; ?tx_internal=0 clears it.
  if (query.get('tx_internal') === '1') write('local', 'exchange-internal', true);
  if (query.get('tx_internal') === '0') write('local', 'exchange-internal', null);
  const disabled = () => !!(navigator.globalPrivacyControl || navigator.doNotTrack === '1' || read('local', 'exchange-internal') === true);
  const market = (document.body && document.body.dataset && document.body.dataset.market) || '';
  const currentPage = () => { const path = location.pathname.replace(/\.html$/, ''); return path === '/' || PAGE.test(path) ? path : '/'; };
  const campaign = {};
  for (const key of ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term']) {
    const value = query.get(key);
    if (value && value.length <= 128) campaign[key] = value;
  }
  // The referring site's hostname only, never the page it came from.
  let referrer = '';
  try {
    const host = document.referrer ? new URL(document.referrer).hostname.toLowerCase() : '';
    if (host && host !== location.hostname && host.length <= 253) referrer = host;
  } catch { /* opaque referrer */ }
  // A visit keeps its landing source for 30 idle minutes, so later pages carry the same source.
  let visit = read('session', 'exchange-visit');
  if (!visit || typeof visit !== 'object' || typeof visit.campaign !== 'object' || !(now - visit.at >= 0 && now - visit.at < VISIT_MS)) visit = {campaign: {}, referrer_host: ''};
  if (Object.keys(campaign).length) visit = {campaign, referrer_host: referrer};
  else if (referrer) visit = {campaign: {}, referrer_host: referrer};
  visit.at = now;
  let visitor;
  function context() {
    if (disabled()) return {tracking_disabled:true, campaign:{}};
    if (!visitor) {
      const incoming = (query.get('tx_vid') || '').toLowerCase();
      const saved = read('local', 'exchange-visitor');
      if (UUID.test(incoming)) visitor = incoming;
      else if (saved && UUID.test(saved.id) && now - saved.at >= 0 && now - saved.at < VISITOR_MS) visitor = saved.id;
      else if (crypto.randomUUID) visitor = crypto.randomUUID();
      if (visitor) { write('local', 'exchange-visitor', {id:visitor, at:now}); write('session', 'exchange-visit', visit); }
    }
    if (!visitor) return {tracking_disabled:true, campaign:{}};
    const payload = {visitor_id:visitor, campaign:visit.campaign, page:currentPage()};
    if (visit.referrer_host) payload.referrer_host = visit.referrer_host;
    return payload;
  }
  window.TokenExchangeAnalytics = {context, track};
  const sent = new Set();
  function track(event, properties = {}) {
    if (event.startsWith('nyte.resale_')) {
      if (!['nyte.resale_listed', 'nyte.resale_withdrawn'].includes(event) || !['AAA', 'A', 'B', 'C'].includes(properties.grade)) return;
      properties = {grade:properties.grade};
    }
    if (event.startsWith('nyte.reserve_')) {
      if (!['nyte.reserve_checkout_started', 'nyte.reserve_checkout_succeeded'].includes(event)) return;
      if (event === 'nyte.reserve_checkout_started') {
        if (properties.unit !== 'blocks' || properties.underlying_kind !== 'index' || !['AAA', 'A', 'B', 'C'].includes(properties.grade)) return;
        properties = {unit:'blocks', underlying_kind:'index', grade:properties.grade};
      } else properties = {};
    }
    const payload = context();
    if (payload.tracking_disabled || (event.startsWith("exchange.") && sent.has(event))) return;
    sent.add(event);
    try {
      Promise.resolve(fetch('https://trustedrouter.com/token-exchange/events', {
        method:'POST', mode:'cors', credentials:'omit', keepalive:true,
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({event, ...payload, ...properties, page:currentPage()}),
      })).catch(() => {});
    } catch { /* Analytics never blocks the page or brochure download. */ }
  }
  /* Clicks. A button's name comes from data-cta or its label; links out to trustedrouter.com,
     GitHub, the sales calendar and sister markets log where they went, and the visitor ID
     rides on links to trustedrouter.com and sister markets so the journeys join up. */
  const CTA_TEXT = {'Download for Apple silicon':'download_apple_silicon', 'Download for Intel Mac':'download_intel_mac', 'Start building':'api_quick_start', 'Get the buyer deck':'buyer_deck', 'Get the provider deck':'provider_deck', 'Get your API key':'api_key', 'Read documentation':'documentation', 'Apply as a provider':'apply_provider', 'Apply as an inference provider':'apply_provider', 'List spare capacity':'spare_capacity', 'Get the open-source stack':'open_source_stack', 'Operator guide':'operator_guide', 'Explore the index':'index'};
  const OUTBOUND = new Set(['trustedrouter.com', 'github.com', 'calendly.com']);
  const sisterHosts = () => {
    const hosts = new Set();
    if (typeof document.querySelectorAll !== 'function') return hosts;
    for (const link of document.querySelectorAll('.market-links a, nav[aria-label="Exchange markets"] a')) {
      try { hosts.add(new URL(link.href, location.href).hostname.toLowerCase()); } catch { /* skip */ }
    }
    hosts.delete(location.hostname);
    return hosts;
  };
  document.addEventListener('click', e => {
    const el = e.target && e.target.closest ? e.target.closest('a,button') : null;
    if (!el) return;
    const cta = (el.dataset && el.dataset.cta) || CTA_TEXT[(el.textContent || '').trim()];
    const position = el.closest('header') ? 'header' : el.closest('.close-photo,.cw-download,.closing,.closing-copy') ? 'close' : el.closest('[class^="hero"],.hero') ? 'hero' : 'section';
    if (el.tagName !== 'A') { if (cta) track('nyte.cta_clicked', {cta, position}); return; }
    let url;
    try { url = new URL(el.href, location.href); } catch { return; }
    if (url.protocol === 'mailto:') { track('nyte.cta_clicked', {cta:cta || 'contact_email', position}); return; }
    if (cta) track('nyte.cta_clicked', {cta, position});
    const host = url.hostname.toLowerCase();
    if (host === location.hostname) return;
    const sister = sisterHosts().has(host);
    if (!OUTBOUND.has(host) && !sister) return;
    if (host === 'trustedrouter.com' || sister) {
      const c = context();
      if (!c.tracking_disabled) {
        url.searchParams.set('tx_vid', c.visitor_id);
        if (market) url.searchParams.set('exchange_market', market);
        el.href = url.href;
      }
    }
    track('nyte.outbound_clicked', {destination_host:host, cta:cta || 'source'});
  }, true);
  let timer;
  function visible() {
    clearTimeout(timer);
    if (document.hidden || disabled()) return;
    track('exchange.landing_viewed');
    timer = setTimeout(() => {
      if (!document.hidden) track('exchange.landing_engaged');
    }, 10000);
  }
  document.addEventListener('visibilitychange', visible);
  visible();
})();
