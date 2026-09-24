// Run with NODE_PATH pointing to the installed Playwright package directory.
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const markets = JSON.parse(fs.readFileSync(path.join(__dirname, 'markets.json')));
  const output = process.argv[2] || '/tmp/token-exchange-build';
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({reducedMotion: 'reduce'});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const market of markets) {
    for (const width of [320, 390, 1440]) {
      await page.setViewportSize({width, height: 1000});
      await page.goto(`http://127.0.0.1:8089/${market.slug}/?utm_source=launch-test&utm_content=creative-a&secret=should-not-pass`);
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.locator('h1').textContent(), market.headline);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${market.slug} overflows at ${width}`);
      assert(await page.locator('img').evaluateAll(images => images.every(img => img.complete && img.naturalWidth > 0)));
      const mainNav = page.getByRole('navigation', {name:'Main navigation', exact:true});
      const menu = page.getByRole('button', {name:'Menu', exact:true, includeHidden:true});
      const mobileMenu = width <= 1100;
      assert.equal(await menu.isVisible(), mobileMenu);
      if (mobileMenu) {
        assert.equal(await mainNav.isVisible(), false);
        await menu.focus();
        await page.keyboard.press('Enter');
        assert.equal(await menu.getAttribute('aria-expanded'), 'true');
      }
      assert(await mainNav.isVisible());
      await page.locator('.market-picker > summary').click();
      const marketNav = page.getByRole('navigation', {name:'Market directory', exact:true});
      assert.equal(await marketNav.getByRole('link').count(), markets.length);
      assert.equal(await marketNav.locator('[aria-current="page"]').textContent(), market.region);
      assert.equal(await marketNav.getByRole('link', {name:'Riyadh', exact:true}).count(), 1);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('.market-picker').evaluate(el => el.open), false);
      if (mobileMenu) {
        await page.keyboard.press('Escape');
        assert.equal(await mainNav.isVisible(), false);
        assert(await menu.evaluate(el => el === document.activeElement));
      }
      assert.equal(await page.locator('.hero').evaluate(el => getComputedStyle(el, '::before').animationName), 'none');
      assert.equal(await page.locator('.provider-strip a').count(), 6);
      const footerNav = page.getByRole('navigation', {name:'Exchange markets', exact:true});
      assert.equal(await footerNav.getByRole('link').count(), markets.length);
      assert.equal(await footerNav.locator('[aria-current="page"]').textContent(), market.region);
      await footerNav.scrollIntoViewIfNeeded();
      if (await footerNav.evaluate(el => el.scrollWidth > el.clientWidth + 2)) {
        await footerNav.evaluate(el => { el.scrollLeft = 0; });
        await page.locator('.footer-markets .geo-next').click();
        assert(await footerNav.evaluate(el => el.scrollLeft > 0));
      }
      if (width <= 600) {
        await page.getByRole('link', {name:'Back to top', exact:true}).click();
        await page.waitForFunction(() => window.scrollY < 2);
      } else {
        await page.evaluate(() => window.scrollTo(0, 0));
      }
      if (['global', 'new-york'].includes(market.slug)) {
        await page.screenshot({path: `/tmp/exchange-${market.slug}-${width}-first.png`});
      }
      for (const href of await page.locator('[data-attribution]').evaluateAll(links => links.map(link => link.href))) {
        assert.equal(new URL(href).searchParams.get('utm_source'), 'launch-test');
        assert.equal(new URL(href).searchParams.get('secret'), null);
      }
      assert.equal(await page.locator('.closing [data-attribution]').count(), 2);
      assert.equal(await page.locator('.trust-evidence .catalogue').count(), 1);
      const supplierArt = page.locator('.supplier-art');
      await supplierArt.scrollIntoViewIfNeeded();
      assert.equal(await supplierArt.locator('.supply-signals').evaluate(el => getComputedStyle(el).display), 'none');
      await page.locator('.faq summary').first().click();
      assert(await page.locator('.faq details').first().evaluate(el => el.open));
      if (['global', 'new-york'].includes(market.slug)) {
        await page.screenshot({path: `/tmp/exchange-${market.slug}-${width}.png`, fullPage: true});
      }
      if (mobileMenu) await menu.click();
      await mainNav.getByRole('link', {name:'For suppliers', exact:true}).click();
      assert.equal(new URL(page.url()).hash, '#sellers');
      assert.equal(await menu.getAttribute('aria-expanded'), 'false');
    }
    // Reviewed social cards are prepared by social.py and copied by build.py.
    // Do not overwrite them with the legacy hero screenshot treatment.
    const socialImage = fs.readFileSync(path.join(output, 'assets', `og-${market.slug}.png`));
    assert.equal(socialImage.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
    assert.equal(socialImage.readUInt32BE(16), 1200);
    assert.equal(socialImage.readUInt32BE(20), 630);
  }
  // Address-bar height changes must not stretch the hero or move its actions.
  const mobile = await browser.newPage({viewport:{width:390,height:700}, reducedMotion:'reduce'});
  const heroGeometry = () => mobile.locator('.hero').evaluate(hero => {
    const rect = hero.getBoundingClientRect();
    const actions = hero.querySelector('.actions').getBoundingClientRect();
    const art = getComputedStyle(hero, '::before');
    return {height:rect.height, actionsTop:actions.top - rect.top,
      artHeight:art.height, artLeft:art.left, mask:art.maskImage};
  });
  for (const [width, height] of [[390,700], [320,600]]) {
    await mobile.setViewportSize({width,height});
    await mobile.goto('http://127.0.0.1:8089/new-york/');
    await mobile.evaluate(() => document.fonts.ready);
    const initial = await heroGeometry();
    await mobile.evaluate(() => window.scrollTo({top:120,behavior:'instant'}));
    await mobile.setViewportSize({width,height:height + 100});
    assert.deepEqual(await heroGeometry(), initial, 'mobile hero stretches when browser chrome hides');
    await mobile.setViewportSize({width,height});
    assert.deepEqual(await heroGeometry(), initial, 'mobile hero jumps when browser chrome returns');
  }
  await mobile.setViewportSize({width:844,height:390});
  await mobile.evaluate(() => document.fonts.ready);
  await mobile.setViewportSize({width:390,height:700});
  await mobile.evaluate(() => document.fonts.ready);
  assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await mobile.close();
  const fallback = await browser.newPage({javaScriptEnabled:false, viewport:{width:390,height:1000}});
  await fallback.goto('http://127.0.0.1:8089/new-york/');
  assert(await fallback.getByRole('navigation', {name:'Main navigation',exact:true}).isVisible());
  assert.equal(await fallback.getByRole('button', {name:'Menu',exact:true}).isVisible(), false);
  assert(await fallback.locator('.supplier-art').isVisible());
  await fallback.close();
  assert.deepEqual(errors, []);
  await browser.close();
  console.log(`PASS: ${markets.length} markets x 3 viewports; images, overflow, attribution, FAQ, menu, reduced motion, mobile hero resize, no-JS navigation; ${markets.length} reviewed OG images checked.`);
})().catch(error => {console.error(error); process.exit(1);});
