(() => {
  "use strict";
  const links = [...document.querySelectorAll('[data-homepage-status]')];
  if (!links.length) return;
  let snapshot = null, receivedAt = 0, pending = false;
  // The header shows one thing only: a green dot while the last check is
  // inside this window and the feed says up. Inside it the last result
  // stands, even past the server's own short freshness window, so routine
  // probe lag never hides a green dot. Every other state (degraded, down,
  // unknown, delayed, failed refresh) hides the dot and leaves the plain
  // Status link, so the header never names an outage; /status explains.
  const DELAYED_AFTER_SECONDS = 30 * 60;

  function render() {
    const freshness = snapshot?.monitor_freshness;
    const sampleAt = Date.parse(freshness?.latest_sample_at);
    const ttl = freshness?.stale_after_seconds;
    const reportedAge = freshness?.latest_sample_age_seconds;
    const valid = Number.isFinite(sampleAt) && sampleAt <= Date.now() + 60000 &&
      Number.isFinite(ttl) && ttl > 0 && Number.isFinite(reportedAge) && reportedAge >= 0 &&
      typeof freshness?.is_stale === 'boolean';
    // Cached responses and suspended tabs must never keep an old green dot.
    const age = valid ? Math.max((Date.now() - sampleAt) / 1000,
      reportedAge + (Date.now() - receivedAt) / 1000) : Infinity;
    const up = age <= DELAYED_AFTER_SECONDS && snapshot.overall_status === 'up';
    for (const link of links) {
      if (up) {
        link.dataset.state = 'up';
        link.setAttribute('aria-label', 'Public status: Operational. View status details.');
        link.setAttribute('title', `Operational · Last probe ${new Date(sampleAt).toUTCString()}`);
      } else {
        delete link.dataset.state;
        link.removeAttribute('aria-label');
        link.removeAttribute('title');
      }
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
