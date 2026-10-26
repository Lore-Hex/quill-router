const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = process.argv[2] || 'http://127.0.0.1:8089';
const local = new URL(base).hostname === '127.0.0.1';
const output = process.env.NYTE_PREVIEW_DIR || require('node:os').tmpdir();

async function indexMime(context) {
  // Python's preview server does not know the production GCS object's HTML metadata.
  if (local) await context.route(`${base}/index`, async route => {
    const response = await route.fetch();
    await route.fulfill({response, contentType: 'text/html'});
  });
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [1440, 768, 390, 320]) {
      const context = await browser.newContext({viewport: {width, height: 1000}});
      await context.route('**/token-exchange/events', route => route.fulfill({status:204}));
      await indexMime(context);
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('response', response => { if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`); });
      await page.goto(`${base}/index`, {waitUntil: 'networkidle'});
      await page.waitForFunction(() => document.querySelectorAll('.ti-chart.is-live').length === 2);
      await page.evaluate(() => document.fonts.ready);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `index overflow ${width}`);
      assert.ok((await page.locator('.ti-asof').innerText()).includes('As of'));
      assert.equal(await page.locator('#nav a[href="/index"]').getAttribute('aria-current'), 'page');
      assert.equal(await page.locator('.site-footer').count(), 1);
      if (width <= 880) {
        await page.getByRole('button', {name:'Menu',exact:true}).click();
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')), '/confidential');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#menuBtn').getAttribute('aria-expanded'), 'false');
      }
      const first = page.locator('[data-ti-chart="nyte"]');
      const month = first.getByRole('button', {name: '1M', exact: true});
      await month.click();
      assert.equal(await month.getAttribute('aria-pressed'), 'true');
      const svg = first.locator('svg');
      await svg.focus();
      await svg.press('Home');
      assert.ok(await first.locator('.ti-tip').isVisible());
      await svg.press('Escape');
      assert.equal(await first.locator('.ti-tip').isVisible(), false);
      const grades = page.locator('[data-ti-chart="grades"]');
      await grades.getByRole('button', {name: 'Linear', exact: true}).click();
      assert.ok((await grades.locator('svg').getAttribute('aria-label')).startsWith('Linear'));
      const frontier = grades.getByRole('button', {name: 'Frontier', exact: true});
      await frontier.click();
      assert.equal(await frontier.getAttribute('aria-pressed'), 'false');
      assert.ok(await page.locator('.index-actions .btn').evaluate(el => el.scrollWidth <= el.clientWidth), 'supplier CTA clipped');
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({path: `${output}/nyte-index-${width}.png`, fullPage: true});
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`PASS index ${width}px: charts, controls, keyboard, layout and assets`);
    }
    const context = await browser.newContext({javaScriptEnabled: false});
    await indexMime(context);
    const page = await context.newPage();
    await page.goto(`${base}/index`, {waitUntil: 'networkidle'});
    await page.locator('.ti-chart-grades').scrollIntoViewIfNeeded();
    await page.waitForFunction(() => [...document.querySelectorAll('.ti-img-dark')].every(image => image.complete && image.naturalWidth));
    assert.equal(await page.locator('.ti-img-dark:visible').count(), 2);
    assert.ok(await page.getByRole('link', {name: 'List spare capacity', exact: true}).isVisible());
    await context.close();
    console.log('PASS index no-JavaScript chart and supplier links');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
