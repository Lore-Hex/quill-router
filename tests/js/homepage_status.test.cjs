const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const script = readFileSync('src/trusted_router/static/homepage/status.js', 'utf8');

async function setup(data, fail = false) {
  let now = Date.now(), response = data, failed = fail;
  const events = {}, timers = [];
  const links = [0, 1].map(() => ({dataset: {}, attributes: {},
    setAttribute(k, v) { this.attributes[k] = v; },
    removeAttribute(k) { delete this.attributes[k]; }
  }));
  const document = {hidden: false, querySelectorAll: () => links,
    addEventListener: (name, fn) => { events[name] = fn; }};
  let calls = 0;
  class Clock extends Date { static now() { return now; } }
  vm.runInNewContext(script, {document, Date: Clock, AbortSignal,
    addEventListener: (name, fn) => { events[name] = fn; },
    setInterval: fn => timers.push(fn),
    fetch: async () => { calls++; if (failed) throw Error('offline');
      return {ok: true, json: async () => ({data: response})}; }
  });
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  return {links, events, document, flush,
    get calls() { return calls; },
    setResponse(value, error = false) { response = value; failed = error; },
    advance(ms) { now += ms; }, tick: () => timers[0]()};
}
function snapshot(status = 'up', extra = {}) {
  return {overall_status: status, monitor_freshness: {
    latest_sample_at: new Date(Date.now() - 10000).toISOString(),
    latest_sample_age_seconds: 10, stale_after_seconds: 420, is_stale: false, ...extra
  }};
}
// The header's only state is the green dot; the Status link itself is static markup.
function assertGreen(link) {
  assert.equal(link.dataset.state, 'up');
  assert.equal(link.attributes['aria-label'], 'Public status: Operational. View status details.');
  assert.match(link.attributes.title, /^Operational · Last probe /);
}
function assertNoDot(link) {
  assert.equal(link.dataset.state, undefined);
  assert.deepEqual(link.attributes, {});
}

test('every status link shows the green dot when the feed is fresh and up', async () => {
  const app = await setup(snapshot('up'));
  app.links.forEach(assertGreen);
});
for (const status of ['degraded', 'routing_degraded', 'trust_degraded', 'down', 'unknown', 'unreachable']) {
  test(`${status} hides the dot and leaves the plain Status link, with no outage wording`, async () => {
    const app = await setup(snapshot(status));
    app.links.forEach(assertNoDot);
  });
}
test('a check inside 30 minutes keeps its green even past the server freshness window', async () => {
  for (const data of [snapshot('up', {is_stale: true}),
    snapshot('up', {latest_sample_at: new Date(Date.now() - 600000).toISOString(), latest_sample_age_seconds: 600, is_stale: true})]) {
    const app = await setup(data);
    assertGreen(app.links[0]);
  }
});
test('really delayed, missing and future samples never show green', async () => {
  for (const data of [
    snapshot('up', {latest_sample_at: new Date(Date.now() - 31 * 60000).toISOString(), latest_sample_age_seconds: 31 * 60, is_stale: true}),
    snapshot('up', {latest_sample_at: null}),
    snapshot('up', {latest_sample_at: new Date(Date.now() + 600000).toISOString()}),
    snapshot('up', {is_stale: undefined}), null]) {
    const app = await setup(data);
    assertNoDot(app.links[0]);
  }
});
test('failed refresh hides the dot, a bad status keeps it hidden and a good one recovers', async () => {
  const app = await setup(snapshot());
  assertGreen(app.links[0]);
  app.setResponse(null, true); app.tick(); await app.flush();
  assertNoDot(app.links[0]);
  app.setResponse(snapshot('down')); app.tick(); await app.flush();
  assertNoDot(app.links[0]);
  app.setResponse(snapshot()); app.tick(); await app.flush();
  assertGreen(app.links[0]);
});
test('suspended tab expires its dot without background requests', async () => {
  const app = await setup(snapshot());
  assertGreen(app.links[0]);
  app.document.hidden = true; app.advance(31 * 60000); app.tick(); await app.flush();
  assert.equal(app.calls, 1);
  assertNoDot(app.links[0]);
  app.document.hidden = false; app.events.visibilitychange(); await app.flush();
  assert.equal(app.calls, 2);
});
