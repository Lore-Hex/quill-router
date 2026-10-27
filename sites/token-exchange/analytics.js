/* First-party, session-scoped metadata. No cookies, ad pixels, or content capture. */
(() => {
  'use strict';
  const disabled = () => navigator.globalPrivacyControl || navigator.doNotTrack === '1';
  const campaign = {};
  const query = new URLSearchParams(location.search);
  for (const key of ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term']) {
    const value = query.get(key);
    if (value && value.length <= 128) campaign[key] = value;
  }
  let visitor;
  function context() {
    if (disabled()) return {tracking_disabled:true, campaign:{}};
    if (!visitor) {
      try {
        const saved = JSON.parse(sessionStorage.getItem('exchange-visit') || 'null');
        if (saved && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(saved.id)
            && Date.now() - saved.at >= 0 && Date.now() - saved.at < 1800000) visitor = saved.id;
      } catch { /* Storage is optional; keep the visit in this page's memory. */ }
      if (!visitor && crypto.randomUUID) {
        visitor = crypto.randomUUID();
        try { sessionStorage.setItem('exchange-visit', JSON.stringify({id:visitor, at:Date.now()})); } catch {}
      }
    }
    return visitor ? {visitor_id:visitor, campaign} : {tracking_disabled:true, campaign:{}};
  }
  window.TokenExchangeAnalytics = {context, track};
  const sent = new Set();
  function track(event, properties = {}) {
    const payload = context();
    if (payload.tracking_disabled || (event.startsWith("exchange.") && sent.has(event))) return;
    sent.add(event);
    try {
      Promise.resolve(fetch('https://trustedrouter.com/token-exchange/events', {
        method:'POST', mode:'cors', credentials:'omit', keepalive:true,
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({event, ...payload, ...properties,
          page: ['/trusted-cowork', '/providers', '/badge', '/confidential', '/suppliers', '/index', '/buy'].includes(location.pathname.replace(/\.html$/, ''))
            ? location.pathname.replace(/\.html$/, '') : '/'}),
      })).catch(() => {});
    } catch { /* Analytics never blocks the page or brochure download. */ }
  }
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
