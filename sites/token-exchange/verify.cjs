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
    for (const width of [390, 768, 1440]) {
      await page.setViewportSize({width, height: 1000});
      await page.goto(`http://127.0.0.1:8089/${market.slug}/?utm_source=launch-test&utm_content=creative-a&secret=should-not-pass`);
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.locator('h1').textContent(), market.headline);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${market.slug} overflows at ${width}`);
      assert(await page.locator('img').evaluateAll(images => images.every(img => img.complete && img.naturalWidth > 0)));
      const mainNav = page.getByRole('navigation', {name:'Main navigation', exact:true});
      const menu = page.getByRole('button', {name:'Menu', exact:true});
      assert(await menu.isVisible());
      assert.equal(await mainNav.isVisible(), false);
      await menu.focus();
      await page.keyboard.press('Enter');
      assert.equal(await menu.getAttribute('aria-expanded'), 'true');
      assert(await mainNav.isVisible());
      await page.keyboard.press('Escape');
      assert.equal(await mainNav.isVisible(), false);
      assert(await menu.evaluate(el => el === document.activeElement));
      assert.equal(await page.locator('.hero').evaluate(el => getComputedStyle(el, '::before').animationName), 'none');
      const marketNav = page.getByRole('navigation', {name:'Market directory', exact:true});
      assert.equal(await marketNav.getByRole('link').count(), markets.length);
      assert.equal(await page.locator('.provider-strip a').count(), 6);
      const geometry = await page.evaluate(() => {
        const nav = document.querySelector('.market-directory').getBoundingClientRect();
        const hero = document.querySelector('.hero').getBoundingClientRect();
        const links = [...document.querySelectorAll('.market-directory a')].map(a => {
          const {x,y,width,height} = a.getBoundingClientRect();
          return {x,y,width,height};
        });
        return {navBottom:nav.bottom, heroTop:hero.top, links};
      });
      assert(geometry.navBottom <= geometry.heroTop + 1, `${market.slug}: cities overlap hero`);
      assert(geometry.navBottom < 360, `${market.slug}: cities buried at ${width}`);
      for (const [index, link] of geometry.links.entries()) {
        assert(link.height >= 40, `${market.slug}: navigation hit target too small`);
        assert(Math.abs(link.y - geometry.links[0].y) < 1, `${market.slug}: market directory wrapped`);
        for (const other of geometry.links.slice(index + 1)) {
          assert(!(link.x < other.x + other.width && link.x + link.width > other.x &&
            link.y < other.y + other.height && link.y + link.height > other.y),
          `${market.slug}: overlapping navigation links`);
        }
      }
      for (const label of ['Market directory', 'Exchange markets']) {
        const nav = page.getByRole('navigation', {name:label, exact:true});
        assert.equal(await nav.getByRole('link').count(), markets.length);
        assert.equal(await nav.locator('[aria-current="page"]').count(), 1);
        assert(await nav.evaluate(el => new Set([...el.children].map(a => a.offsetTop)).size === 1));
        if (await nav.evaluate(el => el.scrollWidth > el.clientWidth + 2)) {
          const controls = nav.locator('..');
          await controls.locator('.geo-next').click();
          assert(await nav.evaluate(el => el.scrollLeft > 0), `${market.slug}: market arrow did not scroll`);
          await controls.locator('.geo-prev').click();
          assert(await nav.evaluate(el => el.scrollLeft < 2), `${market.slug}: market arrow did not return`);
        }
        for (const link of await nav.getByRole('link').all()) {
          await link.scrollIntoViewIfNeeded();
          const box = await link.boundingBox();
          assert(box && box.x >= 0 && box.x + box.width <= width, `${market.slug}: market cannot be scrolled into view`);
        }
        await nav.evaluate(el => { el.scrollLeft = 0; });
      }
      await page.evaluate(() => window.scrollTo(0, 0));
      if (['global', 'new-york'].includes(market.slug)) {
        await page.screenshot({path: `/tmp/exchange-${market.slug}-${width}-first.png`});
      }
      for (const href of await page.locator('[data-attribution]').evaluateAll(links => links.map(link => link.href))) {
        assert.equal(new URL(href).searchParams.get('utm_source'), 'launch-test');
        assert.equal(new URL(href).searchParams.get('secret'), null);
      }
      assert.equal(await page.locator('.closing [data-attribution]').count(), 2);
      const diagram = page.getByRole('figure', {name:'Compare eligible providers through TrustedRouter for your workload', exact:true});
      await diagram.scrollIntoViewIfNeeded();
      await page.waitForFunction(() => document.querySelector('.route-flow').classList.contains('is-visible'));
      assert.equal(await diagram.locator('.route-node').count(), 3);
      assert.equal(await diagram.locator('.route-connector').first().evaluate(el => getComputedStyle(el, '::after').animationName), 'none');
      await page.locator('summary').first().click();
      assert(await page.locator('details').first().evaluate(el => el.open));
      if (['global', 'new-york'].includes(market.slug)) {
        await page.screenshot({path: `/tmp/exchange-${market.slug}-${width}.png`, fullPage: true});
      }
      await menu.click();
      await mainNav.getByRole('link', {name:'Supply tokens', exact:true}).click();
      assert.equal(new URL(page.url()).hash, '#sellers');
      assert.equal(await menu.getAttribute('aria-expanded'), 'false');
    }
    // A separate share image preserves the page's brand and typography.
    await page.setViewportSize({width:1200,height:630});
    await page.goto(`http://127.0.0.1:8089/${market.slug}/`);
    await page.addStyleTag({content: '.masthead,.market-directory,.proof,main>section:not(.hero),footer{display:none!important}.hero{height:630px}.hero-copy{padding:60px 70px 0}.hero h1{font-size:66px}.hero .lead,.hero .actions{display:none}.hero-headline{font-size:26px}.hero-art{bottom:-335px;width:1200px;height:800px}.hero-note{margin-top:25px}.hero-copy:after{content:"Powered by TrustedRouter";position:absolute;left:45px;bottom:28px;font:16px Archivo;color:#cce8d7}'});
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({path:path.join(output,'assets',`og-${market.slug}.png`)});
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
  await mobile.waitForFunction(() => !document.querySelector('.hero').classList.contains('hero-layout-locked'));
  await mobile.setViewportSize({width:390,height:700});
  await mobile.waitForFunction(() => document.querySelector('.hero').classList.contains('hero-layout-locked'));
  assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await mobile.close();
  const fallback = await browser.newPage({javaScriptEnabled:false, viewport:{width:390,height:1000}});
  await fallback.goto('http://127.0.0.1:8089/new-york/');
  assert(await fallback.getByRole('navigation', {name:'Main navigation',exact:true}).isVisible());
  assert.equal(await fallback.getByRole('button', {name:'Menu',exact:true}).isVisible(), false);
  assert(await fallback.getByRole('figure', {name:'Compare eligible providers through TrustedRouter for your workload',exact:true}).isVisible());
  await fallback.close();
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('PASS: 12 markets x 3 viewports; images, overflow, attribution, FAQ, menu, reduced motion, mobile hero resize, no-JS navigation; 12 OG images generated.');
})().catch(error => {console.error(error); process.exit(1);});
