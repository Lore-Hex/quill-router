/* One request-time feed; dated measurements expire after one hour, including during fetch failures. */
(() => {
  'use strict';
  const root = document.querySelector('[data-evidence-profile]');
  if (!root) return;
  const prices = document.querySelector('[data-live-prices]');
  const priceUnit = document.querySelector('[data-live-price-unit]');
  const services = root.querySelector('[data-live-services]');
  const attestation = root.querySelector('[data-live-attestation]');
  const caption = root.querySelector('[data-live-caption]');
  // v3 row labels, shared by the loading state and the live render.
  const LABELS = {build: 'Matches published build', source: 'Source code version', place: 'Data centre'};
  const UNPROVEN = 'Not proven by attestation';
  // Loading placeholders only: the evidence feed's component names (live rows use the feed's own names).
  const SERVICE_NAMES = ['Canonical API', 'US East Regional API', 'Model Inference'];
  // v3: one 'Last checked' line under both columns (the oldest check shown).
  const checked = root.parentElement?.querySelector('[data-live-checked]');
  const setChecked = value => {
    if (!checked) return;
    const shown = checked.dataset.at;
    if (value && (!shown || Date.parse(value) < Date.parse(shown))) checked.dataset.at = value;
    checked.hidden = !checked.dataset.at;
    if (checked.dataset.at) checked.textContent = lastCheck(checked.dataset.at);
  };
  // The brand v3 Confidential page asks for labelled rows and uptime figures; other pages keep the original output.
  const v3 = root.dataset.evidenceStyle === 'v3';
  const statusCaption = caption?.textContent;
  const refreshMs = 60000;
  const staleMs = 60 * 60 * 1000; // Maximum display age, not a current-health guarantee.
  const endpoint = `https://trustedrouter.com/token-exchange/evidence/${root.dataset.evidenceProfile}.json`;
  let payload = null;
  let receivedAt = 0;
  let refreshTimer;
  let refreshing = false;
  let renderedKey;
  let settled = false;
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
  const checkedAt = value => new Date(value).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC');
  const lastCheck = (value, label = 'Last checked') => {
    if (!dated(value)) return '';
    return `${label} ${checkedAt(value)}`;
  };
  const link = (label, url) => {
    const el = node('a', label);
    el.href = url;
    return el;
  };
  const unavailable = () => {
    prices.replaceChildren(link('Browse Tinfoil model routes', 'https://trustedrouter.com/models?filter=e2e'));
    services.replaceChildren();
    if (caption) caption.textContent = statusCaption;
    if (priceUnit) priceUnit.hidden = true;
    if (checked) { delete checked.dataset.at; checked.hidden = true; }
    attestation.replaceChildren();
  };
  const presentable = c => c && typeof c.id === 'string' && typeof c.name === 'string'
    && c.status === 'up' && fresh(c.last_checked_at)
    && typeof c.uptime_24h_percent === 'number' && c.uptime_24h_percent >= 0
    && c.uptime_24h_percent <= 100 && c.sample_count_24h > 0;
  function render() {
    // v3: hold the loading layout until the first fetch has settled.
    if (v3 && !settled) return;
    // Recheck age each second without replacing focused links every second.
    const dates = (Array.isArray(payload?.components) ? payload.components : []).map(c => fresh(c?.last_checked_at));
    const key = JSON.stringify([receivedAt, Boolean(payload), Date.now() - receivedAt > staleMs,
      age(payload?.generated_at) > staleMs, dates, fresh(payload?.attestation_check?.last_checked_at)]);
    if (key === renderedKey) return;
    renderedKey = key;
    if (!payload || Date.now() - receivedAt > staleMs || age(payload.generated_at) > staleMs) {
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
      choice.append(node('h3', row.label), node('p', 'Tinfoil'));
      if (prices.dataset.priceLayout === 'table') {
        const provider = node('div', v3 ? undefined : 'Confidential + E2EE', 'privacy-provider');
        if (v3) provider.append(node('span', 'Confidential', 'v3-chip cc'), node('span', ' + ', 'v3-plus'), node('span', 'E2EE', 'v3-chip e2ee'));
        col.append(choice, provider);
        for (const [label, value] of [['Input / 1M', row.input], ['Output / 1M', row.output]]) {
          const price = node('div', `$${value}`, 'privacy-price');
          price.dataset.label = label;
          price.setAttribute('aria-label', `${label}: $${value}`);
          col.append(price);
        }
        col.target = '_blank';
        col.rel = 'noopener';
      } else {
      col.append(choice, node('div', 'Confidential + E2EE', 'privacy-provider'), node('div', `$${row.input} / $${row.output}`, 'privacy-price'));
      }
      prices.append(col);
    }
    if (priceUnit) {
      priceUnit.hidden = !prices.childElementCount;
      priceUnit.textContent = prices.dataset.priceLayout === 'table'
        ? lastCheck(payload.generated_at, 'Updated')
        : `USD / 1M tokens · input / output · ${lastCheck(payload.generated_at)}`;
    }
    if (!prices.childElementCount) prices.append(link('Browse Tinfoil model routes', 'https://trustedrouter.com/models?filter=e2e'));
    if (checked) { delete checked.dataset.at; checked.hidden = true; }
    services.replaceChildren();
    const components = Array.isArray(payload.components) ? payload.components : [];
    // A landing page is not an incident dashboard. If any source is unhealthy
    // or incomplete, use the existing status link instead of a partial all-clear.
    const showServices = components.length > 0 && components.every(presentable);
    if (caption) caption.textContent = showServices ? caption.dataset.uptimeLabel : statusCaption;
    for (const c of v3 && showServices ? components : []) {
      const row = node('div', undefined, 'service-history');
      row.append(node('span', c.name, 'uptime-name'), node('strong', `${c.uptime_24h_percent.toFixed(2)}%`, 'uptime-figure'));
      services.append(row);
    }
    if (v3 && showServices) {
      // One line for the set: the oldest check, so no row looks fresher than it is.
      const oldest = components.reduce((a, c) => (Date.parse(c.last_checked_at) < Date.parse(a) ? c.last_checked_at : a), components[0].last_checked_at);
      setChecked(oldest);
    }
    for (const c of !v3 && showServices ? components : []) {
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
    if (release.platform !== 'gcp-confidential-space' || typeof release.image_digest !== 'string' || !/^sha256:[a-f0-9]{64}$/.test(release.image_digest)) return;
    if (v3) {
      const rows = node('dl', undefined, 'att-rows');
      const row = (key, value, cls) => {
        const item = node('div', undefined, cls ? `att-row ${cls}` : 'att-row');
        const dd = node('dd');
        dd.append(value);
        item.append(node('dt', key), dd);
        rows.append(item);
      };
      row(LABELS.build, node('code', release.image_digest, 'build-digest'));
      if (typeof release.source_commit === 'string') row(LABELS.source, release.source_commit.slice(0, 8));
      row(LABELS.place, UNPROVEN, 'att-unproven');
      setChecked(check.last_checked_at);
      attestation.append(rows);
      return;
    }
    attestation.append(node('p', 'Published build digest', 'digest-label'), node('code', release.image_digest, 'build-digest'));
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
      // Keep the last response in this tab only. Original timestamps still
      // expire in render(); a successful newer response always replaces it.
    }
    settled = true;
    render();
    refreshing = false;
    // Start the next interval AFTER receipt. A timer started before the initial
    // response can hit the server cache just before its one-minute expiry.
    refreshTimer = setTimeout(refresh, refreshMs);
  }
  // v3 loading state: the final layout with dashes, so nothing jumps when figures arrive.
  // Built here (not in the HTML) so pages without JavaScript never show placeholders.
  if (v3) {
    const dash = (tag, text, cls) => { const el = node(tag, text, cls); el.classList.add('is-pending'); return el; };
    const rows = node('dl', undefined, 'att-rows');
    for (const [key, value, cls] of [[LABELS.build, 'sha256: ––––––––––––––––'], [LABELS.source, '––––––––'], [LABELS.place, UNPROVEN, 'att-unproven']]) {
      const item = node('div', undefined, cls ? `att-row ${cls}` : 'att-row');
      item.append(node('dt', key), cls ? node('dd', value) : dash('dd', value));
      rows.append(item);
    }
    attestation.replaceChildren(rows);
    services.replaceChildren(...SERVICE_NAMES.map(name => {
      const row = node('div', undefined, 'service-history');
      row.append(node('span', name, 'uptime-name'), dash('strong', '––.––%', 'uptime-figure'));
      return row;
    }));
    if (caption) caption.textContent = caption.dataset.uptimeLabel;
    if (checked) { checked.textContent = 'Checking now…'; checked.hidden = false; }
  }
  refresh();
  setInterval(render, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { render(); refresh(); } });
})();
