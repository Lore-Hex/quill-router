const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const script = readFileSync('src/trusted_router/static/homepage/status.js', 'utf8');

async function setup(data, fail = false) {
  let now = Date.now(), response = data, failed = fail;
  const events = {}, timers = [];
  const links = [0, 1].map(() => ({dataset: {}, attributes: {}, text: {},
    setAttribute(k, v) { this.attributes[k] = v; },
    querySelector() { return this.text; }
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
for (const [status, state, label] of [
  ['up', 'up', 'Operational'], ['routing_degraded', 'degraded', 'Routing degraded'],
  ['trust_degraded', 'degraded', 'Trust degraded'], ['down', 'down', 'Major outage'],
  ['unknown', 'unknown', 'Status unavailable']]) {
  test(`both indicators explain ${status}`, async () => {
    const app = await setup(snapshot(status));
    for (const link of app.links) {
      assert.equal(link.dataset.state, state);
      assert.equal(link.text.textContent, label);
      assert.match(link.attributes['aria-label'], new RegExp(label));
    }
  });
}
test('stale, cached-old, missing and future samples never show green', async () => {
  for (const data of [snapshot('up', {is_stale: true}),
    snapshot('up', {latest_sample_at: new Date(Date.now() - 600000).toISOString()}),
    snapshot('up', {latest_sample_at: null}),
    snapshot('up', {latest_sample_at: new Date(Date.now() + 600000).toISOString()}),
    snapshot('up', {is_stale: undefined}), null]) {
    const app = await setup(data);
    assert.equal(app.links[0].dataset.state, 'unknown');
  }
});
test('failed refresh clears green and subsequent refresh recovers', async () => {
  const app = await setup(snapshot());
  app.setResponse(null, true); app.tick(); await app.flush();
  assert.equal(app.links[0].text.textContent, 'Status unavailable');
  app.setResponse(snapshot()); app.tick(); await app.flush();
  assert.equal(app.links[0].dataset.state, 'up');
});
test('suspended tab expires its badge without background requests', async () => {
  const app = await setup(snapshot());
  app.document.hidden = true; app.advance(500000); app.tick(); await app.flush();
  assert.equal(app.calls, 1);
  assert.equal(app.links[0].text.textContent, 'Status delayed');
  app.document.hidden = false; app.events.visibilitychange(); await app.flush();
  assert.equal(app.calls, 2);
});
