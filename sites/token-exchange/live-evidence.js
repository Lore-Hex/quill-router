/* All markets use one request-time feed. Never retain values after a failed fetch. */
(() => {
  'use strict';
  const root = document.querySelector('[data-evidence-profile]');
  if (!root) return;
  const prices = document.querySelector('[data-live-prices]');
  const priceUnit = document.querySelector('[data-live-price-unit]');
  const services = root.querySelector('[data-live-services]');
  const state = root.querySelector('[data-live-state]');
  const attestation = root.querySelector('[data-live-attestation]');
  const caption = root.querySelector('[data-live-caption]');
  const statusCaption = caption?.textContent;
  const refreshMs = 60000;
  const staleMs = 360000; // Twice the three-minute core probe schedule.
  const endpoint = `https://trustedrouter.com/token-exchange/evidence/${root.dataset.evidenceProfile}.json`;
  let payload = null;
  let receivedAt = 0;
  let refreshTimer;
  let refreshing = false;
  let renderedKey;
  const node = (tag, text, cls) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  };
  const age = value => {
    const timestamp = typeof value === 'string' ? Date.parse(value) : NaN;
    return Number.isFinite(timestamp) && timestamp <= Date.now() + 30000 ? Math.max(0, Date.now() - timestamp) : Infinity;
  };
  const dated = value => age(value) !== Infinity;
  const fresh = value => age(value) <= staleMs;
  const lastCheck = value => {
    if (!dated(value)) return '';
    return `Last check ${new Date(value).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC')}`;
  };
  const link = (label, url) => {
    const el = node('a', label);
    el.href = url;
    return el;
  };
  const unavailable = () => {
    prices.replaceChildren(link('Browse Tinfoil model routes ↗', 'https://trustedrouter.com/models?filter=e2e'));
    services.replaceChildren();
    if (caption) caption.textContent = statusCaption;
    state.textContent = '';
    if (priceUnit) priceUnit.hidden = true;
    attestation.replaceChildren();
  };
  const presentable = c => c && typeof c.id === 'string' && typeof c.name === 'string'
    && c.status === 'up' && fresh(c.last_checked_at)
    && typeof c.uptime_24h_percent === 'number' && c.uptime_24h_percent >= 0
    && c.uptime_24h_percent <= 100 && c.sample_count_24h > 0;
  function render() {
    // Recheck age each second without replacing focused links every second.
    const dates = (Array.isArray(payload?.components) ? payload.components : []).map(c => fresh(c?.last_checked_at));
    const key = JSON.stringify([receivedAt, Boolean(payload), Date.now() - receivedAt > refreshMs + 15000,
      age(payload?.generated_at) > 2 * refreshMs + 15000, dates, fresh(payload?.attestation_check?.last_checked_at)]);
    if (key === renderedKey) return;
    renderedKey = key;
    if (!payload || Date.now() - receivedAt > refreshMs + 15000 || age(payload.generated_at) > 2 * refreshMs + 15000) {
      unavailable();
      return;
    }
    prices.replaceChildren();
    for (const row of (Array.isArray(payload.prices) ? payload.prices : [])) {
      if (!row || typeof row.model !== 'string' || typeof row.label !== 'string') continue;
      if (row.provider !== 'tinfoil' || !/^[\d]+(?:\.\d+)?$/.test(row.input) || !/^[\d]+(?:\.\d+)?$/.test(row.output)) continue;
      const col = link('', `https://trustedrouter.com/models/${encodeURI(row.model)}#provider-tinfoil`);
      col.className = 'privacy-column';
      const choice = node('div', undefined, 'privacy-choice');
      choice.append(node('h3', row.label + ' ↗'), node('p', 'Tinfoil'));
      col.append(choice, node('div', 'Confidential + E2EE', 'privacy-provider'), node('div', `$${row.input} / $${row.output}`, 'privacy-price'));
      prices.append(col);
    }
    if (priceUnit) priceUnit.hidden = !prices.childElementCount;
    if (!prices.childElementCount) prices.append(link('Browse Tinfoil model routes ↗', 'https://trustedrouter.com/models?filter=e2e'));
    services.replaceChildren();
    const components = Array.isArray(payload.components) ? payload.components : [];
    // A landing page is not an incident dashboard. If any source is unhealthy
    // or incomplete, use the existing status link instead of a partial all-clear.
    const showServices = components.length > 0 && components.every(presentable);
    if (caption) caption.textContent = showServices ? caption.dataset.uptimeLabel : statusCaption;
    state.textContent = '';
    for (const c of showServices ? components : []) {
      const row = node('div', undefined, 'service-history');
      const heading = node('div', undefined, 'service-heading');
      const value = node('strong', c.uptime_24h_percent.toFixed(2));
      value.append(node('small', '%'));
      heading.append(node('span', c.name), value);
      row.append(heading, node('p', lastCheck(c.last_checked_at), 'last-check'));
      const history = Array.isArray(c.history) ? c.history.slice(-24) : [];
      // Never recolor or drop individual failure buckets to imply a clean history.
      // Keep the measured percentage, but omit the whole chart when incomplete.
      if (history.length && history.every(bucket => bucket && dated(bucket.bucket_start)
          && bucket.sample_count > 0 && bucket.status === 'up')) {
        const bars = node('div', undefined, 'health-bars');
        bars.setAttribute('role', 'img');
        bars.setAttribute('aria-label', `${c.name}: hourly history`);
        for (const bucket of history) {
          const bar = node('span', undefined, 'health-bar up');
          bar.title = `${bucket.bucket_start}: Operational`;
          bars.append(bar);
        }
        row.append(bars);
      }
      services.append(row);
    }
    attestation.replaceChildren();
    const check = payload.attestation_check;
    const release = payload.release;
    if (!check || check.status !== 'up' || !fresh(check.last_checked_at) || !release) return;
    if (root.dataset.evidenceProfile === 'dubai') {
      const region = (Array.isArray(release.regions) ? release.regions : []).find(r => r && r.attestation_url === 'https://api-azure.trustedrouter.com/attestation' && typeof r.origin_hostname === 'string' && r.origin_hostname.endsWith('.uaenorth.azurecontainer.io'));
      if (release.platform !== 'azure-confidential-containers-sev-snp' || !region || typeof region.hostdata !== 'string' || !/^[a-f0-9]{64}$/.test(region.hostdata) || !Array.isArray(release.accepted_hostdata) || !release.accepted_hostdata.includes(region.hostdata)) return;
      attestation.append(node('p', 'Published policy measurement', 'digest-label'), node('code', region.hostdata, 'build-digest'));
    } else {
      if (release.platform !== 'gcp-confidential-space' || typeof release.image_digest !== 'string' || !/^sha256:[a-f0-9]{64}$/.test(release.image_digest)) return;
      attestation.append(node('p', 'Published build digest', 'digest-label'), node('code', release.image_digest, 'build-digest'));
    }
    attestation.prepend(node('p', lastCheck(check.last_checked_at), 'last-check'));
    if (typeof release.source_commit === 'string') attestation.append(node('p', `Published source ${release.source_commit.slice(0, 8)}`));
    attestation.append(node('p', 'Location: Not proven by attestation'));
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    clearTimeout(refreshTimer);
    try {
      const response = await fetch(endpoint, {cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(12000)});
      if (!response.ok) throw new Error('Evidence unavailable');
      payload = await response.json();
      receivedAt = Date.now();
    } catch (_) {
      payload = null;
    }
    render();
    refreshing = false;
    // Start the next interval AFTER receipt. A timer started before the initial
    // response can hit the server cache just before its one-minute expiry.
    refreshTimer = setTimeout(refresh, refreshMs);
  }
  refresh();
  setInterval(render, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { render(); refresh(); } });
})();
