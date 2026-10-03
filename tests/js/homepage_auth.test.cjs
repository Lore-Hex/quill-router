const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../..');
const dashboard = fs.readFileSync(path.join(root, 'src/trusted_router/static/dashboard.js'), 'utf8');
const auth = dashboard.slice(dashboard.indexOf('function hasSignedInHint()'), dashboard.indexOf('function init()'));
const homepage = fs.readFileSync(path.join(root, 'src/trusted_router/static/homepage/homepage.js'), 'utf8');
const cta = homepage.slice(homepage.indexOf('function trackHomepageCta('), homepage.indexOf("document.addEventListener('click',trackHomepageCta)"));
function setup(cookie) {
 const links = [];
 for (const name of ['top', 'migrate', 'closing']) {
  const html = fs.readFileSync(path.join(root, `src/trusted_router/templates/homepage/${name}.html`), 'utf8');
  for (const match of html.matchAll(/<a\b([^>]*href="\/console\/api-keys"[^>]*)>([^<]+)<\/a>/g)) {
   assert.match(match[1], /data-action="open-signin"/);
   const index = links.length;
   links.push({className: match[1].match(/class="([^"]+)"/)[1], textContent: match[2],
    replaceWith: replacement => {links[index] = replacement;}});
  }
 }
 const classes = links.map(a => a.className);
 const context = vm.createContext({document: {cookie,
  querySelectorAll: () => links,
  createElement: () => ({}),
 }});
 vm.runInContext(auth, context);
 vm.runInContext('applyAuthAwareChrome()', context);
 links.classes = classes;
 return links;
}
test('signed-out homepage keeps sign-in labels and normal navigation fallback', () => {
 const links = setup('');
 assert.equal(links.length, 5);
 assert.deepEqual(links.map(a => a.textContent), ['Sign in', 'Sign in', 'Get your API key', 'Get your API key', 'Get your API key']);
});
test('returning visitors get console labels and retain button styles', () => {
 const links = setup('other=1; tr_signed_in=1');
 assert.deepEqual(links.map(a => a.textContent), ['Console', 'Console', 'Open console', 'Open console', 'Open console']);
 links.forEach((a, index) => {
  assert.equal(a.href, '/console/api-keys');
  assert.equal(a.className, links.classes[index]);
  assert.match(a.className, /button|drawer-signin/);
  assert.equal(a['data-action'], undefined);
 });
});
test('delegated homepage analytics survives auth-aware replacement and excludes header sign-in', () => {
 const links = setup('tr_signed_in=1');
 const sent = [];
 const context = vm.createContext({track: (...args) => sent.push(args)});
 vm.runInContext(cta, context);
 links.forEach((link, index) => {
  link.closest = selector => selector === '#get-started' ? index === 4 : selector === '#migrate' ? index === 3 : null;
  context.event = {target: {closest: () => link.className.includes('button-primary') ? link : null}};
  vm.runInContext('trackHomepageCta(event)', context);
 });
 assert.equal(sent.length, 3);
 assert.deepEqual(sent.map(args => args[1].module), ['hero', 'migration', 'closing']);
 assert.ok(sent.every(args => args[0] === 'home.cta_clicked'));
});
