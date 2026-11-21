const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');

// Render the real Observability template through Jinja. The store, the session
// and ClickHouse are not involved: each test answers the usage endpoint itself.
let rendered;
function activityPage() {
  const context = {
    workspaces: [{ id: '0', name: 'Personal workspace' }], current_workspace_id: '0',
    console_next_path: '/console/activity', user_email: 'test@example.test', active: 'activity',
    page_title: 'Observability', page_subtitle: 'Per-request metadata, no prompt content.',
    provider_mode: false, credits_available: '$0.15', credits_empty: false,
    activity: [], activity_unavailable: false,
  };
  rendered ??= execFileSync(process.env.TEST_PYTHON || path.join(root, '.venv', 'bin', 'python'), ['-c', `
import json, sys
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
env = Environment(loader=ChoiceLoader([DictLoader({'console/_social_rewards.html': '', '_favicon.html': ''}), FileSystemLoader('src/trusted_router/templates')]), autoescape=True)
env.globals['csp_nonce'] = lambda: ''
print(env.get_template('console/activity.html').render(**json.loads(sys.argv[1])))
`, JSON.stringify(context)], { cwd: root, encoding: 'utf8' });
  return rendered;
}

// One bucket whose spend names the range, so a test can tell which answer is on screen.
const spend = { '1h': 1, '6h': 6, '24h': 24, '7d': 7, '30d': 30, '90d': 90 };
function usage(range) {
  const granularity = { '1h': 'minute', '6h': '5min', '24h': 'hour' }[range] || 'day';
  const bucket = { minute: '2026-11-01T00:05', '5min': '2026-11-01T00:05', hour: '2026-11-01T00', day: '2026-11-01' }[granularity];
  const row = { bucket, requests: 3, prompt_tokens: 100, completion_tokens: 50, reasoning_tokens: 0, cost_micro: spend[range] * 1000000, byok_micro: 0 };
  return { granularity, start_day: '2026-11-01', end_day: '2026-11-01', truncated: false, buckets: [row], by_model: { 'vendor/model-a': [row] }, range, latest_activity_at: null };
}

// `respond(route, range, attempt)` answers each usage request. Returns the ranges requested, in order.
// `reliability` is the client-reliability answer; by default the panel has no telemetry backend.
async function open(page, respond, reliability = { data: null, meta: { reason: 'unavailable' } }) {
  const requested = [];
  await page.route('http://console.test/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/static/')) return route.fulfill({ path: path.join(root, 'src/trusted_router', url.pathname) });
    if (url.pathname === '/console/activity/client-reliability.json') return route.fulfill({ json: reliability });
    if (url.pathname === '/console/activity/usage.json') {
      requested.push(url.searchParams.get('range'));
      return respond(route, url.searchParams.get('range'), requested.length);
    }
    return route.fulfill({ contentType: 'text/html', body: activityPage() });
  });
  await page.goto('http://console.test/console/activity');
  return requested;
}

const ok = (route, range) => route.fulfill({ json: usage(range) });
const parts = (page) => {
  const panel = page.locator('[data-usage-panel]');
  return {
    panel,
    chart: panel.locator('[data-usage-layout]'),
    loading: panel.locator('[data-usage-loading]'),
    failure: panel.locator('[data-usage-error]'),
    total: panel.locator('[data-usage-total]'),
    status: panel.locator('[data-usage-status]'),
    range: (value) => panel.locator(`input[name="usage-range"][value="${value}"]`),
    // The visible control is the label: its text sits above the radio input.
    choose: (value) => panel.locator(`label:has(input[name="usage-range"][value="${value}"])`).click(),
  };
};

test('shows a loading state until the usage answer arrives', async ({ page }) => {
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const requested = await open(page, async (route, range) => { await held; await ok(route, range); });
  const { panel, chart, loading, failure, total, status } = parts(page);

  await expect(loading).toBeVisible();
  await expect(loading).toHaveText('Loading usage…');
  await expect(panel).toHaveAttribute('aria-busy', 'true');
  await expect(chart).toBeHidden();
  await expect(failure).toBeHidden();

  release();
  await expect(chart).toBeVisible();
  await expect(loading).toBeHidden();
  await expect(panel).not.toHaveAttribute('aria-busy', 'true');
  await expect(total).toHaveText('$30.00');
  await expect(status).toHaveText('1 day buckets');
  expect(requested).toEqual(['30d']);
});

test('remembers the chosen range and opens on it with one request', async ({ page }) => {
  const requested = await open(page, ok);
  const { chart, total, range, choose } = parts(page);
  await expect(total).toHaveText('$30.00');
  await expect(range('30d')).toBeChecked();

  await choose('7d');
  await expect(total).toHaveText('$7.00');
  expect(requested).toEqual(['30d', '7d']);

  // A new page view: the remembered range only, not the default and then the choice.
  requested.length = 0;
  await page.reload();
  await expect(chart).toBeVisible();
  await expect(total).toHaveText('$7.00');
  await expect(range('7d')).toBeChecked();
  await expect(page.locator('[data-usage-range-label]')).toHaveText('Last 7 days');
  expect(requested).toEqual(['7d']);
});

test('cancels the request a newer choice supersedes', async ({ page }) => {
  const aborted = [];
  page.on('requestfailed', request => {
    if (request.url().includes('/console/activity/usage.json')) aborted.push([new URL(request.url()).searchParams.get('range'), request.failure().errorText]);
  });
  let release;
  const held = new Promise(resolve => { release = resolve; });
  // The first answer (30d) never arrives before the user picks 24h.
  const requested = await open(page, async (route, range, attempt) => {
    if (attempt === 1) { await held; return route.fulfill({ json: usage(range) }).catch(() => {}); }
    return ok(route, range);
  });
  const { chart, loading, failure, total, choose } = parts(page);
  await expect(loading).toBeVisible();

  await choose('24h');
  await expect(total).toHaveText('$24.00');
  await expect(chart).toBeVisible();
  expect(requested).toEqual(['30d', '24h']);
  await expect.poll(() => aborted).toEqual([['30d', 'net::ERR_ABORTED']]);
  // The cancelled request is not reported as a failure of the newer one.
  await expect(failure).toBeHidden();
  await expect(page.locator('[data-usage-range-label]')).toHaveText('Last 24 hours');
  release();
});

test('ignores a late answer for a superseded range when requests cannot be cancelled', async ({ page }) => {
  await page.addInitScript(() => { delete window.AbortController; });
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const requested = await open(page, async (route, range, attempt) => {
    if (attempt === 1) await held;
    return ok(route, range);
  });
  const { chart, failure, total, choose } = parts(page);

  await choose('24h');
  await expect(total).toHaveText('$24.00');
  expect(requested).toEqual(['30d', '24h']);

  // The 30-day answer arrives after the 24-hour chart is up. It must not replace it.
  const late = page.waitForResponse(response => response.url().includes('range=30d'));
  release();
  await (await late).finished();
  await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 50)));
  await expect(total).toHaveText('$24.00');
  await expect(page.locator('[data-usage-range-label]')).toHaveText('Last 24 hours');
  await expect(chart).toBeVisible();
  await expect(failure).toBeHidden();
});

test('changing the metric redraws the loaded range without a request', async ({ page }) => {
  const requested = await open(page, ok);
  const { panel, chart, total } = parts(page);
  await expect(total).toHaveText('$30.00');

  await panel.locator('label:has(input[name="usage-metric"][value="tokens"])').click();
  await expect(total).toHaveText('150');
  await panel.locator('label:has(input[name="usage-metric"][value="requests"])').click();
  await expect(total).toHaveText('3');
  await expect(chart).toBeVisible();
  expect(requested).toEqual(['30d']);
});

test('changing the metric in the error state loads again', async ({ page }) => {
  const requested = await open(page, (route, range, attempt) => (attempt === 1 ? route.abort('failed') : ok(route, range)));
  const { panel, chart, failure, total } = parts(page);
  await expect(failure).toBeVisible();

  await panel.locator('label:has(input[name="usage-metric"][value="requests"])').click();
  await expect(chart).toBeVisible();
  await expect(failure).toBeHidden();
  await expect(total).toHaveText('3');
  expect(requested).toEqual(['30d', '30d']);
});

test('a failed fetch shows an error with Retry, not an empty chart', async ({ page }) => {
  const requested = await open(page, (route, range, attempt) => (
    attempt === 1 ? route.fulfill({ status: 503, json: { error: { message: 'Usage data is temporarily unavailable; retry shortly.' } } }) : ok(route, range)
  ));
  const { panel, chart, loading, failure, total, status } = parts(page);

  await expect(failure).toBeVisible();
  await expect(failure).toContainText("Couldn't load usage right now.");
  await expect(status).toHaveText("couldn't load");
  await expect(chart).toBeHidden();
  await expect(loading).toBeHidden();
  await expect(panel.locator('[data-usage-empty]')).toBeHidden();
  await expect(panel).not.toHaveAttribute('aria-busy', 'true');

  await failure.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(chart).toBeVisible();
  await expect(failure).toBeHidden();
  await expect(total).toHaveText('$30.00');
  expect(requested).toEqual(['30d', '30d']);
});

test('a failure after a chart replaces the chart with the error state', async ({ page }) => {
  await open(page, (route, range) => (range === '90d' ? route.abort('failed') : ok(route, range)));
  const { chart, failure, total, choose } = parts(page);
  await expect(total).toHaveText('$30.00');

  await choose('90d');
  await expect(failure).toBeVisible();
  // Not the 30-day chart under a 90-day selection.
  await expect(chart).toBeHidden();

  await choose('30d');
  await expect(chart).toBeVisible();
  await expect(failure).toBeHidden();
});

test('works when the browser refuses storage', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('denied', 'SecurityError'); } });
  });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const requested = await open(page, ok);
  const { total, range, choose } = parts(page);
  await expect(total).toHaveText('$30.00');

  await choose('6h');
  await expect(total).toHaveText('$6.00');

  requested.length = 0;
  await page.reload();
  await expect(total).toHaveText('$30.00');
  await expect(range('30d')).toBeChecked();
  expect(requested).toEqual(['30d']);
  expect(errors).toEqual([]);
});

test('ignores a remembered value that is not a range', async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem('tr.console.activity.usage-range', 'constructor'));
  const requested = await open(page, ok);
  const { total, range } = parts(page);

  await expect(total).toHaveText('$30.00');
  await expect(range('30d')).toBeChecked();
  expect(requested).toEqual(['30d']);
});

// The client panel: what the SDKs saw, next to the server's median for the same window.
function reliability(serverMedian, meta = {}) {
  const summary = { requests: 200, successes: 198, tr_fault: 2, availability_percent: 99.0, retried_percent: 10.0, failover_percent: 5.0, p50_total_ms: 200, p95_total_ms: 800, p50_ttft_ms: 100 };
  return { data: { range: '24h', summary, server_p50_elapsed_ms: serverMedian, top_errors: [], recent_failures: [] }, meta: { scanned: 3, truncated: false, freshness_seconds: 0, ...meta } };
}

test('shows the server median next to the client telemetry', async ({ page }) => {
  await open(page, ok, reliability(20));
  const panel = page.locator('[data-client-panel]');

  await expect(panel.locator('[data-client-requests]')).toHaveText('200');
  await expect(panel.locator('[data-client-server]')).toHaveText('20 ms');
  await expect(panel.locator('[data-client-server-sub]')).toHaveText('server p50 elapsed for the same window');
});

test('keeps the client telemetry and says so when the server median could not be read', async ({ page }) => {
  await open(page, ok, reliability(null, { server_elapsed: 'unavailable' }));
  const panel = page.locator('[data-client-panel]');

  await expect(panel.locator('[data-client-requests]')).toHaveText('200');
  await expect(panel.locator('[data-client-availability]')).toHaveText('99.0000%');
  await expect(panel.locator('[data-client-server]')).toHaveText('\u2014');
  await expect(panel.locator('[data-client-server-sub]')).toHaveText('server p50 is unavailable right now');
});

// Other parts of the page follow the usage range through this event:
// `activity:range-change` on document, detail { range }.
async function listen(page) {
  await page.addInitScript(() => {
    window.rangeEvents = [];
    document.addEventListener('activity:range-change', event => window.rangeEvents.push(event.detail));
  });
}
const published = (page) => page.evaluate(() => window.rangeEvents);
const current = (page) => page.locator('[data-usage-panel]').getAttribute('data-usage-range');

test('publishes the range on load and on a radio change, before the answer arrives', async ({ page }) => {
  await listen(page);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await open(page, async (route, range) => { await held; return route.fulfill({ json: usage(range) }).catch(() => {}); });
  const { loading, total, choose } = parts(page);

  await expect(loading).toBeVisible();
  expect(await published(page)).toEqual([{ range: '30d' }]);
  expect(await current(page)).toBe('30d');

  await choose('7d');
  expect(await published(page)).toEqual([{ range: '30d' }, { range: '7d' }]);
  expect(await current(page)).toBe('7d');

  release();
  await expect(total).toHaveText('$7.00');
  expect(await published(page)).toEqual([{ range: '30d' }, { range: '7d' }]);
});

test('publishes the remembered range once on load', async ({ page }) => {
  await listen(page);
  await page.addInitScript(() => window.localStorage.setItem('tr.console.activity.usage-range', '7d'));
  await open(page, ok);
  const { total } = parts(page);

  await expect(total).toHaveText('$7.00');
  // The restored range only, not the default and then the restored one.
  expect(await published(page)).toEqual([{ range: '7d' }]);
  expect(await current(page)).toBe('7d');
});

test('publishes the range the "Show 90 days" control selects, even when that request fails', async ({ page }) => {
  await listen(page);
  const fortyDaysAgo = new Date(Date.now() - 40 * 24 * 60 * 60 * 1000).toISOString();
  const requested = await open(page, (route, range) => (
    range === '90d'
      ? route.fulfill({ status: 500, json: { error: { message: 'failed' } } })
      : route.fulfill({ json: { ...usage(range), buckets: [], by_model: {}, latest_activity_at: fortyDaysAgo } })
  ));
  const { panel, failure, range } = parts(page);
  const expand = panel.locator('[data-usage-expand-range]');

  await expect(expand).toHaveText('Show 90 days');
  expect(await published(page)).toEqual([{ range: '30d' }]);

  await expand.click();
  await expect(failure).toBeVisible();
  await expect(range('90d')).toBeChecked();
  expect(requested).toEqual(['30d', '90d']);
  // No change event fired and the request failed: the range was still published.
  expect(await published(page)).toEqual([{ range: '30d' }, { range: '90d' }]);
  expect(await current(page)).toBe('90d');
});

test('publishes the range when usage.json fails on load, and not again on Retry', async ({ page }) => {
  await listen(page);
  await open(page, (route, range, attempt) => (
    attempt === 1 ? route.fulfill({ status: 500, json: { error: { message: 'failed' } } }) : ok(route, range)
  ));
  const { failure, total } = parts(page);

  await expect(failure).toBeVisible();
  expect(await published(page)).toEqual([{ range: '30d' }]);

  await failure.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(total).toHaveText('$30.00');
  // The same range again is not a change.
  expect(await published(page)).toEqual([{ range: '30d' }]);
});
