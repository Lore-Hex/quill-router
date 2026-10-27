/* Execute the shipped wizard against rendered controls, without a browser or dependencies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const source = fs.readFileSync(process.argv[3] || `${__dirname}/nyte-v2/buy.js`, 'utf8');

function setup(search = '') {
  const nodes = new Map(), radios = [], buttons = new Map(), events = [], handlers = {};
  class Node {
    constructor(attrs = {}) {
      Object.assign(this, attrs);
      this.dataset = Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith('data-'))
        .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()), value]));
      this.hidden = 'hidden' in attrs;
      this.disabled = 'disabled' in attrs;
      this.required = 'required' in attrs;
      this.value = attrs.value || '';
      this.textContent = '';
      this._checked = 'checked' in attrs;
    }
    get checked() { return this._checked; }
    set checked(value) {
      if (value) for (const node of radios) if (node.name === this.name) node._checked = false;
      this._checked = value;
    }
    get valueAsNumber() { return this.value === '' ? NaN : Number(this.value); }
    get validity() {
      const n = this.valueAsNumber;
      return {valid: this.disabled || (!this.error && (!this.required || !!this.value.trim()) &&
        (this.type !== 'number' || (Number.isInteger(n) && n >= Number(this.min) && n <= Number(this.max))))};
    }
    setCustomValidity(error) { this.error = error; }
    checkValidity() { return this.validity.valid; }
    reportValidity() { this.reported = true; }
    setAttribute(key, value) { this[key] = value; }
    removeAttribute(key) { delete this[key]; }
    focus() {}
    scrollIntoView() {}
    replaceChildren(...children) { this.children = children; }
    closest() { return this; }
  }
  function attributes(tag) {
    const attrs = {};
    for (const match of tag.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[match[1]] = match[2] || '';
    return attrs;
  }
  for (const match of html.matchAll(/<(?:input|select|fieldset|p|div|label|h2|time|a|button|section)\b([^>]*)>/g)) {
    const attrs = attributes(match[1]);
    const node = new Node(attrs);
    if (attrs.id) nodes.set(attrs.id, node);
    if (attrs.type === 'radio') radios.push(node);
    if ('data-block-delta' in attrs) buttons.set(attrs['data-block-delta'], node);
  }
  const get = id => {
    assert(nodes.has(`buy-${id}`), `rendered control buy-${id} exists`);
    return nodes.get(`buy-${id}`);
  };
  get('term').value = '1';
  const prices = [...html.matchAll(/<span class="buy-price"([^>]*)>/g)].map(match => new Node(attributes(match[1])));
  get('grades').querySelectorAll = selector => {
    assert.equal(selector, '.buy-price');
    return prices;
  };
  const steps = [1, 2, 3, 4].map(step => new Node({'data-step': String(step)}));
  const progress = steps.map(() => new Node());
  const flow = nodes.get('buy-flow');
  flow.querySelector = selector => {
    const radio = selector.match(/^input\[name="(\w+)"\](?::checked|\[value="([^"]+)"\])$/);
    if (radio) return radios.find(node => node.name === radio[1] && (radio[2] ? node.value === radio[2] : node.checked));
    const button = selector.match(/^\[data-block-delta="(-?1)"\]$/);
    assert(button, selector);
    return buttons.get(button[1]);
  };
  flow.querySelectorAll = selector => {
    if (selector === '[data-step]') return steps;
    if (selector === '.buy-steps li') return progress;
    if (selector === '[data-js]') return [];
    throw Error(selector);
  };
  flow.addEventListener = (event, handler) => { handlers[event] = handler; };
  const location = {search, pathname:'/buy', origin:'https://nytokenexchange.com'};
  const history = Object.fromEntries(['pushState', 'replaceState'].map(key => [key, (_, __, url) => {
    location.search = url.search;
  }]));
  const document = {
    getElementById: id => nodes.get(id),
    createElement: () => new Node(),
    addEventListener: (_, callback) => callback(),
  };
  const window = {addEventListener: (event, handler) => { handlers[event] = handler; },
    TokenExchangeAnalytics: {track: (event, properties) => events.push({event, ...properties})},
    NYTECatalog: {read: async () => ({data:[]})}};
  vm.runInNewContext(source, {document, window, location, history, URL, URLSearchParams, Intl, Date});
  const radio = (name, value) => radios.find(node => node.name === name && node.value === value);
  const change = (name, value) => { radio(name, value).checked = true; handlers.change(); };
  const input = value => { get('blocks').value = value; handlers.input({target:get('blocks')}); };
  const click = attr => handlers.click({target: {closest: () => ({hasAttribute: key => key === attr})}});
  const delta = value => handlers.click({target: {closest: () => ({hasAttribute: key => key === 'data-block-delta', dataset:{blockDelta:String(value)}})}});
  return {get, radio, change, input, click, delta, prices, steps, events, location, buttons};
}

for (const query of ['', '?facility=unlisted', '?facility=__proto__', '?facility=submerge%22%5D']) {
  const s = setup(query);
  assert.equal(s.steps[0].hidden, false);
  assert.equal(s.radio('underlying', 'index').checked, true);
  assert.equal(s.get('facility-fields').hidden, true);
  assert.equal(s.get('facility-fields').disabled, true);
}
for (const [id, name, cap, launch] of [
  ['greenference', 'Greenference', 1000, 'February 2027'],
  ['submerge', 'Submerge', 50000, 'August 2027'],
]) {
  const s = setup(`?facility=${id}&unit=throughput&grade=AAA&step=4&email=private`);
  assert.equal(s.steps[1].hidden, false, 'facility preselect opens step 2');
  assert.equal(s.radio('facility', id).checked, true);
  assert.equal(s.radio('underlying', 'facility').checked, true);
  assert.equal(s.radio('unit', 'blocks').checked, true);
  for (const unit of ['throughput', 'unsure']) assert.equal(s.radio('unit', unit).disabled, true);
  assert.equal(s.get('rate').disabled, true);
  assert.equal(s.get('rate-field').hidden, true);
  assert.equal(s.get('facility-fields').hidden, false);
  assert.equal(s.get('facility-fields').disabled, false);
  for (const key of ['grades', 'model-fields', 'price-caption', 'total', 'term-field', 'start-field']) {
    assert.equal(s.get(key).hidden, true, key);
  }
  assert(s.prices.every(price => price.hidden));
  assert.equal(s.get('start').disabled, true);
  assert.equal(s.get('term').disabled, true);
  assert.equal(s.get('facility-unit-note').hidden, false);
  assert.equal(s.get('quantity-label').textContent, 'Number of 1B-token lots');
  assert.equal(Number(s.get('blocks').max), cap);
  assert.equal(s.get('lot-cap').textContent, `Maximum ${cap.toLocaleString('en-US')} lots in the first offering.`);
  assert.equal(new URLSearchParams(s.location.search).get('facility'), id);
  assert(!s.location.search.includes('private'));
  assert.deepEqual(Object.keys(s.events[0]).sort(), ['event','unit','underlying_kind','grade'].sort());
  assert.equal(s.events[0].underlying_kind, 'facility');
  assert.equal(s.events[0].grade, 'none');
  s.input(String(cap + 1));
  assert.equal(s.get('blocks').value, String(cap));
  assert.equal(s.buttons.get('1').disabled, true);
  s.delta(1);
  assert.equal(s.get('blocks').value, String(cap));
  for (const invalid of ['', '0', '1.5']) {
    s.input(invalid);
    s.click('data-next');
    assert.equal(s.steps[1].hidden, false, 'invalid lot count cannot advance');
  }
  s.input('1');
  s.delta(-1);
  assert.equal(s.get('blocks').value, '1');
  s.delta(1);
  assert.equal(s.get('blocks').value, '2');
  s.get('start').value = ''; // No editable start date is required for planned facilities.
  s.click('data-next');
  assert.equal(s.steps[2].hidden, false);
  s.click('data-next');
  assert.equal(s.steps[3].hidden, false);
  const summary = new URL(s.get('calendly-link').href).searchParams.get('a1');
  assert(summary.includes(`underlying: Planned ${name}; targeted launch: ${launch}`));
  assert(summary.includes('quantity: 2 x 1B-token lots'));
  assert(summary.includes('Grade B · Professional (DeepSeek-class models)'));
  assert(summary.includes('over the facility’s first twelve months of operation, not per month'));
  assert(!/\$|USD|indicative|as of|term:|start:|guarantee|loan|deposit|\bLOI\b|fundrais|financ/i.test(summary));
  assert.equal(s.get('summary').textContent, summary);
  assert.equal(s.get('calendar').children[0].src, s.get('calendly-link').href);
  assert.equal(s.events.at(-1).event, 'nyte.buy_completed');
  assert.equal(s.events.at(-1).underlying_kind, 'facility');
  s.click('data-back');
  s.click('data-back');
  s.change('underlying', 'index');
  assert.equal(s.get('facility-fields').hidden, true);
  assert.equal(s.get('facility-fields').disabled, true);
  assert.equal(s.radio('unit', 'throughput').disabled, false);
  assert.equal(s.radio('unit', 'unsure').disabled, false);
  assert.equal(s.get('price-caption').hidden, false);
  assert.equal(s.get('total').hidden, false);
  assert.equal(s.get('start').disabled, false);
  assert.equal(s.get('term').disabled, false);
  assert.equal(s.get('blocks').max, '1000000');
  s.change('underlying', 'model');
  assert.equal(s.get('model-fields').hidden, false);
  assert.equal(s.get('model-fields').disabled, false);
  assert.equal(s.get('model').required, true);
}
const switched = setup('?unit=throughput');
switched.change('underlying', 'facility');
assert.equal(switched.radio('unit', 'blocks').checked, true);
switched.change('facility', 'submerge');
switched.input('50000');
switched.change('facility', 'greenference');
assert.equal(switched.get('blocks').value, '1000');
console.log('Facility wizard: preselection, caps, units, pricing, timing, Calendly and analytics passed.');
