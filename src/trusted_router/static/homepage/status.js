(() => {
  "use strict";
  const links = [...document.querySelectorAll('[data-homepage-status]')];
  if (!links.length) return;
  const labels = {
    up: 'Operational', degraded: 'Degraded', routing_degraded: 'Routing degraded',
    trust_degraded: 'Trust degraded', down: 'Major outage', unknown: 'Status unavailable'
  };
  let snapshot = null, receivedAt = 0, pending = false;

  function render() {
    let state = 'unknown', label = 'Status unavailable';
    const freshness = snapshot?.monitor_freshness;
    const sampleAt = Date.parse(freshness?.latest_sample_at);
    const ttl = freshness?.stale_after_seconds;
    const reportedAge = freshness?.latest_sample_age_seconds;
    const valid = Number.isFinite(sampleAt) && sampleAt <= Date.now() + 60000 &&
      Number.isFinite(ttl) && ttl > 0 && Number.isFinite(reportedAge) && reportedAge >= 0 &&
      typeof freshness?.is_stale === 'boolean';
    if (valid) {
      // Cached responses and suspended tabs must never keep an old green badge.
      const age = Math.max((Date.now() - sampleAt) / 1000,
        reportedAge + (Date.now() - receivedAt) / 1000);
      if (freshness.is_stale || age > ttl) {
        label = 'Status delayed';
      } else if (Object.hasOwn(labels, snapshot.overall_status)) {
        const status = snapshot.overall_status;
        label = labels[status];
        state = status.endsWith('degraded') ? 'degraded' : status;
      }
    }
    for (const link of links) {
      link.dataset.state = state;
      link.setAttribute('aria-label', `Public status: ${label}. View status details.`);
      link.title = valid ? `${label} · Last probe ${new Date(sampleAt).toUTCString()}` : label;
      const text = link.querySelector('[data-status-label]');
      if (text) text.textContent = label;
    }
  }

  async function refresh() {
    if (document.hidden || pending) return;
    pending = true;
    try {
      const response = await fetch('/status.json', {
        headers: {Accept: 'application/json'}, signal: AbortSignal.timeout(8000)
      });
      if (!response.ok) throw new Error('Status unavailable');
      snapshot = (await response.json()).data;
      receivedAt = Date.now();
    } catch {
      snapshot = null;
    } finally {
      pending = false;
      render();
    }
  }
  void refresh();
  setInterval(() => { render(); void refresh(); }, 60000);
  document.addEventListener('visibilitychange', () => {
    render();
    if (!document.hidden) void refresh();
  });
  addEventListener('pageshow', () => { render(); void refresh(); });
})();
