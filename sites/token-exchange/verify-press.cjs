const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = process.argv[2] || 'http://127.0.0.1:8099';
const output = process.env.NYTE_PREVIEW_DIR || require('node:os').tmpdir();

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [1440, 768, 390, 320]) {
      const context = await browser.newContext({viewport: {width, height: 1000}, reducedMotion: 'reduce'});
      await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: base});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('response', response => { if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`); });
      await page.goto(`${base}/media-kit.html`, {waitUntil: 'networkidle'});
      await page.evaluate(() => document.fonts.ready);
      // Full-page captures must load images outside the viewport too.
      await page.evaluate(() => document.querySelectorAll('img[loading="lazy"]').forEach(image => { image.loading = 'eager'; }));
      await page.locator('#contact').scrollIntoViewIfNeeded();
      await page.waitForFunction(() => [...document.images].every(image => image.complete && image.naturalWidth > 0));
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`);
      for (const image of await page.locator('.image-grid img').all()) {
        const box = await image.boundingBox();
        assert.ok(Math.abs(box.width / box.height - 16 / 9) < 0.02, `image framing at ${width}`);
      }
      await page.screenshot({path: `${output}/nyte-press-${width}.png`, fullPage: true});
      for (const id of ['one-sentence', 'boilerplate', 'founder-bio']) {
        await page.locator(`[data-copy="${id}"]`).click();
        await page.waitForFunction(() => document.getElementById('copy-status').textContent === 'Copied to clipboard.');
        assert.equal(await page.evaluate(() => navigator.clipboard.readText()), (await page.locator(`#${id}`).textContent()).trim());
      }
      await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); }; });
      await page.locator('[data-copy="one-sentence"]').click();
      await page.waitForFunction(() => document.getElementById('copy-status').textContent.includes('copy it manually'));
      assert.equal(await page.evaluate(() => window.getSelection().toString()), await page.locator('#one-sentence').textContent());
      const question = page.getByText('What is a token?', {exact: true});
      await question.click();
      assert.ok(await question.evaluate(node => node.parentElement.open));
      const downloadEvent = page.waitForEvent('download');
      await page.getByRole('link', {name: 'Download the press kit'}).click();
      const download = await downloadEvent;
      assert.equal(download.suggestedFilename(), 'nyte-media-kit.zip');
      assert.equal(await download.failure(), null);
      const video = page.locator('video');
      await video.evaluate(element => element.load());
      await page.waitForFunction(() => document.querySelector('video').readyState >= 1);
      assert.ok(await video.evaluate(element => element.duration > 80 && element.duration < 95));
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`PASS ${width}px: images, layout, clipboard, fallback, FAQ, ZIP and video metadata`);
    }
    const context = await browser.newContext({javaScriptEnabled: false});
    const page = await context.newPage();
    await page.goto(`${base}/media-kit.html`);
    assert.equal(await page.locator('[data-copy]:visible').count(), 0);
    assert.ok(await page.getByRole('link', {name: 'Download the press kit'}).isVisible());
    await page.getByText('What is a token?', {exact: true}).click();
    assert.ok(await page.locator('details[open]').count());
    await context.close();
    console.log('PASS no-JavaScript reading, FAQ and downloads');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
