// Run with NODE_PATH pointing to the installed Playwright package directory.
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const markets = JSON.parse(fs.readFileSync(path.join(__dirname, 'markets.json')));
  const output = process.argv[2] || '/tmp/token-exchange-build';
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const market of markets) {
    for (const width of [390, 768, 1440]) {
      await page.setViewportSize({width, height: 1000});
      await page.goto(`http://127.0.0.1:8089/${market.slug}/?utm_source=launch-test&utm_content=creative-a&secret=should-not-pass`);
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.locator('h1').textContent(), market.name + '.');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${market.slug} overflows at ${width}`);
      assert(await page.locator('.hero-art').evaluate(img => img.complete && img.naturalWidth > 0));
      const cityNav = page.getByRole('navigation', {name:'Exchange cities', exact:true});
      assert.equal(await cityNav.getByRole('link').count(), markets.filter(m => m.scope === 'city').length);
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
        assert(link.x >= 0 && link.x + link.width <= width, `${market.slug}: clipped city`);
        for (const other of geometry.links.slice(index + 1)) {
          assert(!(link.x < other.x + other.width && link.x + link.width > other.x &&
            link.y < other.y + other.height && link.y + link.height > other.y),
          `${market.slug}: overlapping navigation links`);
        }
      }
      if (['global', 'new-york'].includes(market.slug)) {
        await page.screenshot({path: `/tmp/exchange-${market.slug}-${width}-first.png`});
      }
      const href = await page.locator('[data-attribution]').first().getAttribute('href');
      assert.equal(new URL(href).searchParams.get('utm_source'), 'launch-test');
      assert.equal(new URL(href).searchParams.get('secret'), null);
      await page.locator('summary').first().click();
      assert(await page.locator('details').first().evaluate(el => el.open));
      if (['global', 'new-york'].includes(market.slug)) {
        await page.screenshot({path: `/tmp/exchange-${market.slug}-${width}.png`, fullPage: true});
      }
    }
    // A separate share image preserves the page's brand and typography.
    await page.setViewportSize({width:1200,height:630});
    await page.goto(`http://127.0.0.1:8089/${market.slug}/`);
    await page.addStyleTag({content: '.masthead,.market-directory,.proof,main>section:not(.hero),footer{display:none!important}.hero{height:630px}.hero-copy{padding:60px 70px 0}.hero h1{font-size:66px}.hero .lead,.hero .actions{display:none}.hero-headline{font-size:26px}.hero-art{bottom:-335px;width:1200px;height:800px}.hero-note{margin-top:25px}.hero-copy:after{content:"Powered by TrustedRouter";position:absolute;left:45px;bottom:28px;font:16px Archivo;color:#cce8d7}'});
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({path:path.join(output,'assets',`og-${market.slug}.png`)});
  }
  assert.deepEqual(errors, []);
  await browser.close();
  console.log(`PASS: ${markets.length} markets x 3 viewports; images, overflow, attribution, FAQ; ${markets.length} OG images generated.`);
})().catch(error => {console.error(error); process.exit(1);});
