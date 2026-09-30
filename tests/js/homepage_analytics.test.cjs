const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/trusted_router/static/homepage/homepage.js'), 'utf8');
// Exercise the shipped transport in isolation from the page's UI bindings.
const transport = source.slice(source.indexOf('function track('), source.indexOf('let feedbackTimer;'));
function setup(navigator = {}, fetchError) {
 const sent = [], local = [];
 const context = vm.createContext({navigator, document: {dispatchEvent: e => local.push(e)},
  CustomEvent: class {constructor(name, payload) {this.detail = payload.detail;}},
  fetch: async (url, options) => {if(fetchError) throw fetchError; sent.push({url, options});},
 });
 vm.runInContext(transport, context);
 return {context, sent, local};
}
test('only event names go to the first-party endpoint; properties remain local', async () => {
 const {context, sent, local} = setup();
 vm.runInContext("track('home.catalog_row_clicked', {model_id:'private-id'})", context);
 await new Promise(setImmediate);
 assert.equal(sent.length, 1);
 assert.equal(sent[0].url, '/analytics/events');
 assert.deepEqual(JSON.parse(sent[0].options.body), {event:'home.catalog_row_clicked'});
 assert.equal(sent[0].options.credentials, 'same-origin');
 assert.equal(sent[0].options.keepalive, true);
 assert.equal(local[0].detail.properties.model_id, 'private-id');
});
for (const navigator of [{globalPrivacyControl:true}, {doNotTrack:'1'}]) {
 test(`privacy preference ${JSON.stringify(navigator)} suppresses transport`, async () => {
  const {context, sent} = setup(navigator);
  vm.runInContext("track('home.cta_clicked')", context);
  await new Promise(setImmediate);
  assert.equal(sent.length, 0);
 });
}
test('failed transport never rejects into UI handlers', async () => {
 const {context, local} = setup({}, new Error('offline'));
 vm.runInContext("track('home.cta_clicked')", context);
 await new Promise(setImmediate);
 assert.equal(local.length, 1);
});
test('initially open FAQ is not counted until a visitor reopens it', () => {
 const events = [];
 let toggle;
 const detail = {open:true, addEventListener: (_, handler) => {toggle=handler;}};
 const context = vm.createContext({$$: () => [detail], track: name => events.push(name)});
 const start = source.indexOf("$$('#faq details')");
 const end = source.indexOf("$$('.trc .m a')", start);
 vm.runInContext(source.slice(start, end), context);
 toggle();
 assert.equal(events.length, 0);
 detail.open = false;
 toggle();
 detail.open = true;
 toggle();
 assert.deepEqual(events, ['home.faq_opened']);
});
