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
test('clipboard icons keep accessible feedback and restore without losing SVG', async () => {
 const callbacks = [], copied = [], feedback = [];
 const button = {innerHTML:'<svg>clipboard</svg>', disabled:false, title:'Copy base URL',
  classList:{contains:()=>true}, attrs:{'aria-label':'Copy base URL'},
  getAttribute(name){return this.attrs[name];},setAttribute(name,value){this.attrs[name]=value;}};
 const context=vm.createContext({navigator:{clipboard:{writeText:async text=>copied.push(text)}},
  feedback:text=>feedback.push(text),setTimeout:fn=>callbacks.push(fn)});
 const start=source.indexOf('async function copyText(');
 const end=source.indexOf("$$('.trc .fl button')",start);
 vm.runInContext(source.slice(start,end),context);
 assert.equal(await context.copyText('https://api.trustedrouter.com/v1',button),true);
 assert.deepEqual(copied,['https://api.trustedrouter.com/v1']);
 assert.equal(button.attrs['aria-label'],'Copied');
 assert.equal(button.disabled,true);
 assert.match(button.innerHTML,/<svg/);
 callbacks[0]();
 assert.equal(button.innerHTML,'<svg>clipboard</svg>');
 assert.equal(button.attrs['aria-label'],'Copy base URL');
 assert.equal(button.disabled,false);
});
