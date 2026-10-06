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
      // The hero and closing box render the river photograph; the provider marquee holds still for reduced motion.
      const heroPhoto = page.locator('.hero-photo');
      assert.equal(await heroPhoto.count(), 1);
      const heroImage = await heroPhoto.evaluate(el => getComputedStyle(el).backgroundImage);
      const closingImage = await page.locator('.closing-art').evaluate(el => getComputedStyle(el, '::before').backgroundImage);
      for (const image of [heroImage, closingImage]) {
        const url = image.match(/^url\("([^"]+)"\)$/)[1];
        assert((await page.request.get(url)).ok(), `${market.slug} artwork ${url} did not resolve at ${width}`);
      }
      assert.equal(await page.locator('.hero-provider-track').evaluate(el => getComputedStyle(el).animationName), 'none');
      assert.equal(await page.locator('.hero-provider-group:first-child a').count(), 6);
      assert.equal(await page.locator('.hero-provider-group[aria-hidden="true"] a:not([tabindex="-1"])').count(), 0);
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
      assert.equal(await page.locator('.closing [data-attribution]').count(), 1);
      assert.equal(await page.locator('a[href="#brochure"]').count(), 3);
      assert.equal(await page.locator('#brochure-form button[type=submit]').isDisabled(), false);
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
    const photo = hero.querySelector('.hero-photo');
    const art = photo.getBoundingClientRect();
    return {height:rect.height, actionsTop:actions.top - rect.top,
      artHeight:art.height, artTop:art.top - rect.top, artSize:getComputedStyle(photo).backgroundSize};
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
  assert(await fallback.locator('#brochure-form button[type=submit]').isDisabled());
  assert(await fallback.locator('#brochure-form a[href^="mailto:"]').isVisible());
  await fallback.close();
  // The brochure form against a stand-in for trustedrouter.com: preflight, a rejected address, then a download.
  const brochure = await browser.newPage({viewport:{width:1440,height:900}});
  let brochureRequests = 0;
  await brochure.route('https://trustedrouter.com/token-exchange/brief', async route => {
    const request = route.request();
    const cors = {'Access-Control-Allow-Origin': 'http://127.0.0.1:8089', 'Access-Control-Allow-Headers': 'content-type', 'Access-Control-Allow-Methods': 'POST, OPTIONS', 'Vary': 'Origin'};
    if (request.method() === 'OPTIONS') return route.fulfill({status:204, headers:cors});
    brochureRequests += 1;
    assert.equal(request.headers()['content-type'], 'application/json');
    const body = request.postDataJSON();
    assert.equal(body.resource, 'brochure');
    assert.equal(body.website, '');
    assert.deepEqual(body.campaign, {utm_source:'linkedin', utm_campaign:'ny-launch'});
    // A reply the page cannot read, as when an outer layer refuses the request.
    if (body.email === 'blocked@example.com') return route.abort();
    if (body.email === 'nope@invalid') return route.fulfill({status:422, headers:{...cors, 'Content-Type':'application/json'}, body:JSON.stringify({ok:false, error:'invalid_email'})});
    return route.fulfill({status:200, headers:{...cors, 'Content-Type':'application/pdf'}, body:Buffer.from('%PDF-1.4\n%stand-in\n')});
  });
  await brochure.goto('http://127.0.0.1:8089/new-york/?utm_source=linkedin&utm_campaign=ny-launch&secret=dropped');
  await brochure.getByRole('link', {name:'Get the overview', exact:true}).first().click();
  await brochure.waitForFunction(() => location.hash === '#brochure');
  const form = brochure.locator('#brochure-form');
  await form.locator('input[name=email]').fill('nope@invalid');
  await form.locator('button[type=submit]').click();
  await brochure.locator('#brochure-status[data-state="error"]').waitFor();
  assert.equal(await brochure.locator('#brochure-status').textContent(), 'Please enter a valid email address.');
  assert.equal(await form.locator('input[name=email]').getAttribute('aria-invalid'), 'true');
  await form.locator('input[name=email]').fill('blocked@example.com');
  await form.locator('button[type=submit]').click();
  await brochure.waitForFunction(() => document.querySelector('#brochure-status').dataset.state === 'error' && document.querySelector('#brochure-status').textContent.startsWith('Something went wrong'));
  assert.equal(await brochure.locator('#brochure-status').textContent(), 'Something went wrong. Try again or email enterprise@trustedrouter.com.');
  await form.locator('input[name=email]').fill('ada@example.com');
  const [download] = await Promise.all([brochure.waitForEvent('download'), form.locator('button[type=submit]').click()]);
  assert.equal(download.suggestedFilename(), 'TrustedRouter-Token-Exchange-Brochure.pdf');
  await brochure.locator('#brochure-status[data-state="success"]').waitFor();
  assert.equal(brochureRequests, 3);
  await brochure.close();
  assert.deepEqual(errors, []);
  await browser.close();
  console.log(`PASS: ${markets.length} markets x 3 viewports; images, overflow, attribution, FAQ, menu, reduced motion, mobile hero resize, no-JS navigation, brochure form (no-JS fallback, rejected address, unreadable reply, campaign fields, download); ${markets.length} reviewed OG images checked.`);
})().catch(error => {console.error(error); process.exit(1);});
