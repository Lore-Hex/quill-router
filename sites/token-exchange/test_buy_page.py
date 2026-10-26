"""Public reserved tokens inquiries: static build, index references and privacy boundaries."""
import json
import re
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from build import build, load_markets
from test_provider_page import PageLinks

HERE = Path(__file__).resolve().parent


class BuyPageTests(unittest.TestCase):
    output: ClassVar[tempfile.TemporaryDirectory[str]]
    root: ClassVar[Path]
    page: ClassVar[str]
    parser: ClassVar[PageLinks]

    @classmethod
    def setUpClass(cls) -> None:
        cls.output = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.output.cleanup)
        cls.root = Path(cls.output.name)
        build(cls.root)
        cls.page = (cls.root / "buy").read_text()
        cls.parser = PageLinks()
        cls.parser.feed(cls.page)

    def test_routes_shell_metadata_and_sitemap(self) -> None:
        self.assertEqual(self.page, (self.root / "buy.html").read_text())
        self.assertIn('<title>Reserved tokens | New York Token Exchange</title>', self.page)
        self.assertIn('Reserve your next<br><em>billion tokens.</em>', self.page)
        self.assertIn('This is a request to discuss a reservation, not an order.', self.page)
        self.assertNotIn('capacity request', self.page)
        self.assertIn('<link rel="canonical" href="https://nytokenexchange.com/buy">', self.page)
        self.assertIn('/assets/nyte-v2/styles.css?', self.page)
        self.assertIn('/assets/nyte-v2/buy.css?', self.page)
        self.assertIn('/assets/nyte-v2/buy.js?', self.page)
        for market in load_markets():
            sitemap = (self.root / market['slug'] / 'sitemap.xml').read_text()
            self.assertEqual('https://nytokenexchange.com/buy' in sitemap, market['slug'] == 'new-york')
        for name in ['buy.css', 'buy.js']:
            self.assertTrue((self.root / 'assets/nyte-v2' / name).is_file())

    def test_entry_points_and_existing_homepage_contract(self) -> None:
        home = (self.root / 'new-york/index.html').read_text()
        self.assertIn('<a class="btn btn-primary" href="/buy">Reserved tokens</a>', home)
        for name in ['new-york/index.html', 'index', 'confidential', 'suppliers', 'providers', 'buy']:
            self.assertIn('<a href="/buy">Reserved tokens</a>', (self.root / name).read_text())
        for market in load_markets():
            page = (self.root / market['slug'] / 'index.html').read_text()
            parser = PageLinks()
            parser.feed(page)
            self.assertEqual(len(parser.forms), 1)
            self.assertNotIn('calendly', page.lower())
        for grade in ['AAA', 'A', 'B', 'C']:
            self.assertIn(f'/buy?grade={grade}&amp;unit=blocks">Reserve {grade} tokens</a>', (self.root / 'index').read_text())

    def test_prices_and_dated_non_executable_offer(self) -> None:
        data = runpy.run_path(str(HERE.parents[1] / 'src/trusted_router/content/token_index.py'))
        for grade in data['GRADES']:
            price = data['QUOTES'][grade['code']]['value']
            self.assertIn(f'value="{grade["code"]}" data-price="{price}"', self.page)
            self.assertIn(f'${price:,}', self.page)
        self.assertIn(data['AS_OF'], self.page)
        self.assertIn(data['AS_OF_ISO'], self.page)
        self.assertIn('Indicative, not an executable quote', self.page)
        self.assertIn('not live products', self.page)
        self.assertIn('not a securities or cryptocurrency exchange', self.page)

    def test_no_submission_no_widget_and_direct_booking(self) -> None:
        self.assertFalse(self.parser.forms)
        self.assertNotIn('widget.js', self.page)
        self.assertNotIn('<iframe', self.page)  # Loaded only after contact validation.
        links = [urlparse(link) for link in self.parser.links if 'calendly.com' in link]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].path, '/joseph-perla/15min')
        self.assertEqual(parse_qs(links[0].query), {
            'embed_domain': ['nytokenexchange.com'], 'embed_type': ['Inline'], 'hide_gdpr_banner': ['1'],
        })
        self.assertIn('<noscript>', self.page)
        script = (HERE / 'nyte-v2/buy.js').read_text()
        self.assertNotIn('Storage', script)
        self.assertNotIn('JSON.stringify', script)
        self.assertIn('history[push', script)

    def test_pricing_visibility_executes_production_update(self) -> None:
        script = (HERE / 'nyte-v2/buy.js').read_text()
        update = script.split('  function update() {', 1)[1].split('\n  function fieldsValid(', 1)[0]
        # Use every rendered price line, including its no-JS hidden state.
        prices = re.findall(r'<span class="buy-price"([^>]*)>(.*?)</span>', self.page)
        self.assertEqual(len(prices), 4)
        self.assertIn('name="unit" value="blocks" checked', self.page)
        self.assertIn('<p class="buy-fine" id="buy-price-caption">', self.page)
        for attrs, _ in prices:
            self.assertNotIn('hidden', attrs)
        harness = 'const prices = ' + json.dumps([
            {'hidden': 'hidden' in attrs, 'textContent': text} for attrs, text in prices
        ]) + ';\n'
        harness += r'''
const assert = require('node:assert/strict');
const fields = Object.fromEntries(['grades','model-fields','model','block-fields','rate-field',
  'total','price-caption','as-of'].map(id => [id, {hidden:false, disabled:false, textContent:''}]));
fields.blocks = {valueAsNumber:3, validity:{valid:true}};
fields['as-of'].textContent = 'October 19, 2026';
let unit = 'blocks', underlying = 'index', grade = 'AAA';
const grades = {AAA:1799, A:890, B:166, C:28};
const get = id => fields[id];
const selected = name => ({value:{unit, underlying, grade}[name], dataset:{price:grades[grade]}});
const priceLines = selector => {
  assert.equal(selector, '.buy-price');
  return prices;
};
fields.grades.querySelectorAll = priceLines;
const flow = {querySelector: () => ({})};
const money = new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:0});
const catalogue = () => {};
'''
        harness += 'function update() {' + update
        harness += r'''
for (const nonBlock of ['throughput', 'unsure']) {
  for (grade of Object.keys(grades)) {
    unit = 'blocks';
    update();
    assert.equal(fields.total.hidden, false);
    assert(fields.total.textContent.includes(money.format(3 * grades[grade])));
    unit = nonBlock;
    update();
    assert.equal(fields.grades.hidden, false, `${unit}: grades remain visible`);
    assert.equal(fields.grades.disabled, false, `${unit}: grades remain selectable`);
    for (const price of prices) assert.equal(price.hidden, true, `${unit}: hide ${price.textContent}`);
    assert.equal(fields['price-caption'].hidden, true, `${unit}: hide pricing caption`);
    assert.equal(fields.total.hidden, true, `${unit}: hide indicative total`);
    const visiblePricing = [...prices, fields.total].filter(node => !node.hidden);
    assert(!visiblePricing.some(node => node.textContent.includes('$')));
    unit = 'blocks';
    update();
    for (const price of prices) assert.equal(price.hidden, false, 'restore all four prices');
    assert.equal(fields['price-caption'].hidden, false);
    assert.equal(fields.total.hidden, false);
  }
}
underlying = 'model';
update();
assert.equal(fields.grades.hidden, true);
assert.equal(fields.total.hidden, true);
'''
        result = subprocess.run(['node', '-e', harness], check=False, capture_output=True, text=True)  # noqa: S603,S607 — fixed local test harness
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_start_month_defaults_in_local_time_and_is_optional_without_js(self) -> None:
        self.assertIn('id="buy-start" type="month">', self.page)
        script = (HERE / 'nyte-v2/buy.js').read_text()
        initialize = script.split('  function initializeStart() {', 1)[1].split('\n  function update(', 1)[0]
        harness = r'''
const assert = require('node:assert/strict');
const NativeDate = globalThis.Date;
const start = {};
const get = () => start;
let now;
const Date = class extends NativeDate {
  constructor(...args) { super(...(args.length ? args : [now])); }
};
'''
        harness += 'function initializeStart() {' + initialize
        harness += r'''
for (const [year, month, day, expected] of [
  [2026, 0, 31, '2026-02'], [2026, 11, 31, '2027-01'], [2028, 1, 29, '2028-03']
]) {
  for (const hour of [0, 23]) {
    now = new NativeDate(year, month, day, hour, 30).getTime();
    initializeStart();
    assert.equal(start.value, expected);
    assert.equal(start.required, true);
  }
}
'''
        subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)  # noqa: S603,S607 — fixed local test harness

    def test_calendly_encoding_executes_the_production_url_builder(self) -> None:
        # Exercise the actual scheduling function with a small DOM fixture, not a second URL builder.
        script = (HERE / 'nyte-v2/buy.js').read_text()
        scheduling = script.split('  function scheduling() {', 1)[1].split('\n  function show(', 1)[0]
        harness = r'''
const assert = require('node:assert/strict');
const fields = Object.fromEntries(Object.entries({model:'vendor/model & special', provider:'A/B',
  blocks:'3', rate:'1000', term:'6', start:'2027-02', company:'Research & Co + 東京', name:'José & Ada',
  email:'jose+capacity@example.com'}).map(([key,value]) => [key,{value}]));
for (const key of ['calendly-link','summary','calendar']) fields[key] = {};
let iframe;
fields.calendar.replaceChildren = value => { iframe = value; };
const get = key => fields[key];
let unit = 'blocks', underlying = 'model';
const selected = key => ({value:{unit,underlying,grade:'AAA'}[key], dataset:{price:'1799'}});
const clean = value => value.trim().replace(/\s+/g, ' ');
const document = {createElement: () => ({})};
'''
        harness += 'function scheduling() {' + scheduling + '\nscheduling();\n'
        harness += r'''
const url = new URL(iframe.src);
assert.equal(url.origin, 'https://calendly.com');
assert.equal(url.pathname, '/joseph-perla/15min');
assert.equal(url.searchParams.get('name'), fields.name.value);
assert.equal(url.searchParams.get('email'), fields.email.value);
assert.equal(url.searchParams.get('embed_domain'), 'nytokenexchange.com');
assert.equal(url.searchParams.get('embed_type'), 'Inline');
assert.equal(url.searchParams.get('hide_gdpr_banner'), '1');
assert(url.searchParams.get('a1').includes('company: Research & Co + 東京'));
assert(url.searchParams.get('a1').includes('quantity: 3 x 1B tokens'));
assert(url.searchParams.get('a1').includes('provider A/B'));
assert.equal(fields['calendly-link'].href, iframe.src);
assert(!fields.summary.textContent.includes(fields.name.value));
assert(!fields.summary.textContent.includes(fields.email.value));
assert.equal(iframe.referrerPolicy, 'no-referrer');
for (const term of ['1', '3', '6', '12']) {
  fields.term.value = term;
  scheduling();
  const expected = `term: ${term} ${term === '1' ? 'month' : 'months'};`;
  assert(fields.summary.textContent.includes(expected));
  assert(new URL(iframe.src).searchParams.get('a1').includes(expected));
}
for (unit of ['throughput', 'unsure']) {
  for (underlying of ['index', 'model']) {
    scheduling();
    const summary = new URL(iframe.src).searchParams.get('a1');
    assert(summary.includes(`Unit: ${unit};`));
    assert(summary.includes(unit === 'throughput' ? '1000 tokens per second' : 'quantity: to discuss'));
    assert(!/\$|USD|1,799|1799|indicative|as of/i.test(summary), 'no price in Calendly a1');
    assert.equal(fields.summary.textContent, summary);
    assert.equal(fields['calendly-link'].href, iframe.src);
  }
}
'''
        subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)  # noqa: S603,S607 — fixed local test harness


if __name__ == '__main__':
    unittest.main()
