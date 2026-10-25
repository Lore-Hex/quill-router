const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const script = readFileSync('src/trusted_router/static/console.js', 'utf8');
const demoKey = 'sk-tr-demo-not-a-real-key';

function setup({key = demoKey, status = 200, payload, offline = false, telemetryFails = false} = {}) {
  const nodes = {};
  const node = selector => nodes[selector] ||= {hidden: true, textContent: '', classList: {add() {}, remove() {}}};
  const error = node('[data-call-error]');
  error.querySelector = node;
  const result = node('[data-call-result]');
  result.querySelector = node;
  const flow = {dataset: {keySource: 'secret', endpoint: '/v1/chat/completions'},
    querySelector: node, querySelectorAll: () => []};
  const button = {disabled: false, attributes: {}, closest: () => flow, querySelector: node,
    classList: {add() {}}, setAttribute(k, v) {this.attributes[k] = v;},
    removeAttribute(k) {delete this.attributes[k];}};
  const calls = [];
  const context = {document: {readyState: 'loading', addEventListener() {},
    getElementById: () => key ? {textContent: key, hasAttribute: () => false} : null},
    window: {setTimeout: () => 1, clearTimeout() {}},
    crypto: {randomUUID: () => 'attempt-123'}, performance: {now: () => 100}, AbortController,
    fetch: async (url, options) => {
      calls.push({url, options});
      if (url === '/analytics/events') {
        if (telemetryFails) throw Error('telemetry unavailable');
        return {ok: true};
      }
      if (offline) throw Error('offline');
      return {status, ok: status === 200, headers: {get: () => null}, json: async () => payload};
    }};
  vm.createContext(context);
  vm.runInContext(script, context);
  return {context, button, nodes, calls, async run() {
    await context.runFirstActivationCall(button);
    await new Promise(resolve => setImmediate(resolve));
  }, events() {return calls.filter(call => call.url === '/analytics/events').map(call => JSON.parse(call.options.body));}};
}

const pong = {model: 'served/model', choices: [{message: {content: 'PONG'}, finish_reason: 'stop'}],
  trustedrouter: {routing: {selected_provider: 'provider'}}, usage: {total_cost_microdollars: 42}};

test('missing key records one failed attempt without sending inference', async () => {
  const app = setup({key: ''});
  await app.run();
  assert.equal(app.calls.length, 2);
  assert.equal(app.events()[1].failure_reason, 'missing_key');
  assert.equal(app.events()[0].attempt_id, app.events()[1].attempt_id);
  assert.equal(app.nodes['[data-call-error-title]'].textContent, 'This key was not accepted');
  assert.equal(app.nodes['[data-call-error-action]'].href, '/console/api-keys#new-api-key');
});

test('test request authenticates inference only and renders response metadata safely', async () => {
  const app = setup({payload: pong});
  await app.run();
  const inference = app.calls.find(call => call.url === '/v1/chat/completions');
  assert.equal(inference.options.headers.Authorization, `Bearer ${demoKey}`);
  assert.equal(inference.options.headers['Idempotency-Key'], 'welcome-attempt-123');
  assert.deepEqual(JSON.parse(inference.options.body), {model: 'trustedrouter/cheap',
    messages: [{role: 'user', content: 'Reply with exactly PONG.'}], temperature: 0, max_tokens: 512, stream: false});
  assert.equal(app.nodes['[data-result-output]'].textContent, 'PONG');
  assert.equal(app.nodes['[data-result-model]'].textContent, 'served/model');
  assert.equal(app.nodes['[data-result-provider]'].textContent, 'provider');
  assert.equal(app.nodes['[data-result-cost]'].textContent, '$0.000042');
  assert.equal(app.nodes['[data-call-result]'].hidden, false);
  assert.equal(app.button.disabled, true);
  assert.equal(app.button.attributes['aria-busy'], undefined);
  assert.equal(app.events()[1].event, 'onboarding_call_succeeded');
  for (const call of app.calls.filter(call => call.url === '/analytics/events')) {
    assert.equal(call.options.credentials, 'same-origin');
    assert.equal(call.options.headers.Authorization, undefined);
    assert.equal(call.options.body.includes(demoKey), false);
    assert.equal(call.options.body.includes('PONG'), false);
  }
});

for (const [status, title, href] of [[401, 'This key was not accepted', '/console/api-keys#new-api-key'],
  [402, 'Credits are required', '/console/credits'], [429, 'The request was rate limited', undefined]]) {
  test(`HTTP ${status} provides recovery and allows retry`, async () => {
    const app = setup({status});
    await app.run();
    assert.equal(app.nodes['[data-call-error-title]'].textContent, title);
    assert.equal(app.nodes['[data-call-error-action]'].href, href);
    assert.equal(app.nodes['[data-call-error]'].hidden, false);
    assert.equal(app.button.disabled, false);
    assert.equal(app.events()[1].http_status, status);
    assert.equal(app.events()[1].failure_reason, 'http_error');
  });
}

test('empty truncated output records output exhaustion and permits retry', async () => {
  const app = setup({payload: {choices: [{message: {content: ''}, finish_reason: 'length'}]}});
  await app.run();
  assert.equal(app.events()[1].failure_reason, 'output_budget_exhausted');
  assert.equal(app.events()[1].finish_reason, 'length');
  assert.equal(app.button.disabled, false);
  assert.equal(app.nodes['[data-call-result]'].hidden, true);
});

test('network errors allow retry and telemetry failure never blocks successful setup', async () => {
  const offline = setup({offline: true});
  await offline.run();
  assert.equal(offline.events()[1].failure_reason, 'network_error');
  assert.equal(offline.button.disabled, false);
  const app = setup({payload: pong, telemetryFails: true});
  await app.run();
  assert.equal(app.nodes['[data-call-result]'].hidden, false);
});

test('metadata prefers response headers and preserves exact microdollars', () => {
  const {context} = setup();
  const metadata = context.completionMetadata(pong, {headers: {get: name => ({
    'x-trustedrouter-served-model': 'header/model', 'x-trustedrouter-provider': 'header/provider'})[name]}});
  assert.equal(metadata.model, 'header/model');
  assert.equal(metadata.provider, 'header/provider');
  assert.equal(context.microdollarsDisplay('9007199254740993'), '$9007199254.740993');
  assert.equal(context.microdollarsDisplay(-1), '-$0.000001');
  assert.equal(context.microdollarsDisplay('invalid'), '$0.000000');
  assert.equal(context.completionText({choices: [{message: {content: [
    {type: 'text', text: 'PO'}, {type: 'output_text', text: 'NG'}, {type: 'image', text: 'ignore'}]}}]}), 'PONG');
});
