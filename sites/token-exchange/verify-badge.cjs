const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = process.argv[2] || 'http://127.0.0.1:8098';
const output = process.env.NYTE_PREVIEW_DIR || require('node:os').tmpdir();

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    // The lime Band closes the hero inside the first screen at common window sizes,
    // and the nav's Start building is the only orange button there.
    for (const [width, height] of [[1440, 900], [1440, 800], [1280, 720], [1920, 1080], [390, 844], [375, 667], [360, 740]]) {
      const context = await browser.newContext({viewport: {width, height}});
      const page = await context.newPage();
      await page.goto(`${base}/badge.html`, {waitUntil: 'networkidle'});
      await page.evaluate(() => document.fonts.ready);
      const fold = await page.evaluate(() => {
        const header = document.querySelector('header').getBoundingClientRect();
        const hero = document.querySelector('.bv3-hero').getBoundingClientRect();
        const band = document.querySelector('.bv3-hero .v3-band').getBoundingClientRect();
        const orange = [...document.querySelectorAll('.btn')].filter(button => {
          const box = button.getBoundingClientRect();
          return box.height && box.top < innerHeight && getComputedStyle(button).backgroundColor === 'rgb(255, 107, 53)';
        }).map(button => button.textContent.trim());
        return {header: header.bottom, hero: [hero.top, hero.bottom], band: [band.top, band.bottom, band.height], orange, innerHeight};
      });
      assert.ok(Math.abs(fold.hero[0] - fold.header) < 1, `hero starts under the header at ${width}x${height}`);
      assert.ok(Math.abs(fold.hero[1] - height) < 1, `hero is exactly the first screen at ${width}x${height}: ${JSON.stringify(fold)}`);
      assert.ok(Math.abs(fold.band[1] - fold.hero[1]) < 1 && fold.band[1] <= height, `band in the first screen at ${width}x${height}`);
      assert.ok(fold.band[2] >= 96 && fold.band[2] <= 144, `band height ${fold.band[2]} at ${width}x${height}`);
      if (width < 600) assert.equal(fold.band[2], 96);
      assert.deepEqual(fold.orange, ['Start building'], `one orange button in the first screen at ${width}x${height}`);
      await context.close();
      console.log(`PASS ${width}x${height}: hero fills the first screen, Band ${fold.band[2]}px in view`);
    }
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
      // Kit CodeBlock: icon-only copy button in the panel's top-right corner, scroll arrow from app.js.
      const panel = await page.locator('.bv3-code').boundingBox();
      const copy = await page.locator('.bv3-code #copy').boundingBox();
      assert.ok(copy.width >= 44 && copy.height >= 44, `copy touch target at ${width}`);
      assert.ok(Math.abs(copy.x + copy.width - (panel.x + panel.width)) < 1 && Math.abs(copy.y - panel.y) < 1, `copy top-right at ${width}`);
      assert.equal(await page.locator('.bv3-actions .btn').count(), 1);
      const overflows = await page.evaluate(() => { const c = document.querySelector('.bv3-code .code'); return c.scrollWidth > c.clientWidth + 2; });
      assert.equal(await page.locator('.bv3-code .code-next').isVisible(), overflows, `scroll arrow follows overflow at ${width}`);
      // The three settings never break mid-word.
      for (const chip of await page.locator('.bv3-settings code').all()) {
        const box = await chip.boundingBox();
        assert.ok(box.height < 40, `setting on one line at ${width}`);
      }
      assert.deepEqual(await page.locator('.bv3-rule h3').allTextContents(), ['Who may show it', 'What the route must enforce', 'What the encryption claim covers', 'What the badge is not']);
      if (width <= 600) {
        // Phones: badge first in the hero; the main buttons take the full content width.
        const order = await page.evaluate(() => ['.bv3-hero-badge', '.bv3-hero .v3-bar'].map(s => document.querySelector(s).getBoundingClientRect().top));
        assert.ok(order[0] < order[1], `badge above the eyebrow at ${width}`);
        const content = await page.locator('.bv3-hero .wrap').evaluate(e => e.clientWidth - parseFloat(getComputedStyle(e).paddingLeft) - parseFloat(getComputedStyle(e).paddingRight));
        for (const selector of ['.bv3-hero .btn', '#download', '.bv3-join .btn']) {
          assert.ok(Math.abs((await page.locator(selector).boundingBox()).width - content) < 1, `${selector} full width at ${width}`);
        }
      }
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
