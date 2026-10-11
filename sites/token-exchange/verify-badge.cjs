const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = process.argv[2] || 'http://127.0.0.1:8098';
const output = process.env.NYTE_PREVIEW_DIR || require('node:os').tmpdir();

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [1440, 390, 320]) {
      const context = await browser.newContext({viewport: {width, height: 1000}});
      await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: base});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('response', response => { if (response.status() >= 400) errors.push(response.url()); });
      await page.goto(`${base}/badge.html`, {waitUntil: 'networkidle'});
      await page.evaluate(() => document.fonts.ready);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `badge overflow at ${width}`);
      await page.screenshot({path: `${output}/nyte-badge-${width}.png`, fullPage: true});
      assert.equal(await page.locator('input[name="role"]').count(), 0);
      assert.equal(await page.locator('[src*="nyte-provider"], a[href*="nyte-provider"]').count(), 0);
      for (const role of ['enterprise']) {
        for (const theme of ['dark', 'light']) {
          await page.locator(`input[value="${theme}"]`).check();
          const name = `nyte-${role}-${theme}.svg`;
          assert.ok((await page.locator('#badge-preview').getAttribute('src')).endsWith(name));
          await page.waitForFunction(() => document.getElementById('badge-preview').complete && document.getElementById('badge-preview').naturalWidth > 0);
          assert.ok((await page.locator('#download').getAttribute('href')).endsWith(name));
          assert.ok((await page.locator('#embed-code').textContent()).includes(name));
          await page.getByRole('button', {name: 'Copy HTML'}).click();
          await page.waitForFunction(() => document.getElementById('copy-status').textContent.startsWith('HTML copied'));
          assert.equal(await page.evaluate(() => navigator.clipboard.readText()), await page.locator('#embed-code').textContent());
        }
      }
      await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); }; });
      await page.getByRole('button', {name: 'Copy HTML'}).click();
      await page.waitForFunction(() => document.getElementById('copy-status').textContent.includes('copy manually'));
      await page.goto(`${base}/providers.html`, {waitUntil: 'networkidle'});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `provider overflow at ${width}`);
      assert.equal(await page.locator('a[href$=".pdf"],iframe,object,embed').count(), 0);
      assert.ok((await page.textContent('body')).includes('Updated seven-page PDF'));
      await page.screenshot({path: `${output}/nyte-providers-${width}.png`, fullPage: true});
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`PASS ${width}px: layout, enterprise-only styles, copy, fallback, private deck`);
    }
    const context = await browser.newContext({javaScriptEnabled: false});
    const page = await context.newPage();
    await page.goto(`${base}/badge.html`);
    assert.ok((await page.locator('#embed-code').textContent()).includes('nyte-enterprise-dark.svg'));
    await page.getByText('All badge downloads', {exact: true}).click();
    assert.equal(await page.locator('.all-assets a[download]').count(), 2);
    await context.close();
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
