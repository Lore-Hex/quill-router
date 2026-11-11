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
      assert.ok(!(await page.textContent('body')).includes('Privacy Secured'));
      // Desktop hero fills the first screen above the fold, Band on its bottom edge.
      const hero = await page.locator('.bv3-hero').boundingBox();
      const band = await page.locator('.bv3-hero .v3-band').boundingBox();
      assert.ok(Math.abs(band.y + band.height - (hero.y + hero.height)) < 1, `band at hero bottom at ${width}`);
      assert.ok(band.height >= 96 && band.height <= 144, `band height ${band.height} at ${width}`);
      if (width >= 1440) assert.ok(Math.abs(hero.y + hero.height - 1000) < 1, `hero fills the first screen at ${width}`);
      const tabs = page.getByRole('tab');
      assert.deepEqual(await tabs.allTextContents(), ['Light background', 'Dark background']);
      for (const [theme, name, keyed] of [['dark', 'nyte-verified-secure-white.svg', false], ['light', 'nyte-verified-secure-black.svg', true], ['dark', 'nyte-verified-secure-white.svg', true]]) {
        const tab = page.getByRole('tab', {name: theme === 'light' ? 'Light background' : 'Dark background'});
        if (keyed) { await page.getByRole('tab', {selected: true}).focus(); await page.keyboard.press('ArrowRight'); }
        else await tab.click();
        assert.equal(await tab.getAttribute('aria-selected'), 'true');
        if (keyed) assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-selected')), 'true');
        assert.equal(await page.locator('[role="tab"][aria-selected="true"]').count(), 1);
        assert.ok((await page.locator('#badge-preview').getAttribute('src')).endsWith(name));
        await page.waitForFunction(() => document.getElementById('badge-preview').complete && document.getElementById('badge-preview').naturalWidth > 0);
        assert.ok((await page.locator('#download').getAttribute('href')).endsWith(name));
        assert.ok((await page.locator('#embed-code').textContent()).includes(name));
        assert.equal(await page.locator('#embed-code .s').count(), 5);
        await page.getByRole('button', {name: 'Copy HTML'}).click();
        await page.waitForFunction(() => document.getElementById('copy-status').textContent.startsWith('HTML copied'));
        assert.equal(await page.evaluate(() => navigator.clipboard.readText()), await page.locator('#embed-code').textContent());
      }
      await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); }; });
      await page.getByRole('button', {name: 'Copy HTML'}).click();
      await page.waitForFunction(() => document.getElementById('copy-status').textContent.includes('copy manually'));
      await page.goto(`${base}/providers.html`, {waitUntil: 'networkidle'});
      assert.equal(await page.locator('#nav [aria-current="page"]').getAttribute('href'), '/providers');
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `provider overflow at ${width}`);
      assert.equal(await page.locator('a[href$=".pdf"],iframe,object,embed').count(), 0);
      assert.ok((await page.textContent('body')).includes('What’s in the deck'));
      await page.screenshot({path: `${output}/nyte-providers-${width}.png`, fullPage: true});
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`PASS ${width}px: layout, background tabs, copy, fallback, private deck`);
    }
    const context = await browser.newContext({javaScriptEnabled: false});
    const page = await context.newPage();
    await page.goto(`${base}/badge.html`);
    assert.ok((await page.locator('#embed-code').textContent()).includes('nyte-verified-secure-black.svg'));
    assert.ok((await page.locator('#download').getAttribute('href')).endsWith('nyte-verified-secure-black.svg'));
    assert.equal(await page.locator('main a[download][href$="nyte-verified-secure-white.svg"]').count(), 1);
    await context.close();
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
