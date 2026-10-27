// Local preview only. External catalogue, analytics and Calendly are intercepted;
// this checks the handoff without creating a booking or sending test contact details.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const base = process.argv[2] || 'http://127.0.0.1:8089';
assert.equal(new URL(base).hostname, '127.0.0.1');
const shots = process.env.NYTE_BUY_SHOTS || '/private/tmp/nyte-buy-shots';
fs.mkdirSync(shots, {recursive:true});
const name = 'José & Ada Test';
const email = 'nyte+test@example.com';
const company = 'Research & Co + 東京';
const paths = [
  {unit:'blocks', underlying:'index'},
  {unit:'blocks', underlying:'model', provider:true},
  {unit:'blocks', underlying:'model', failed:true},
  {unit:'throughput', underlying:'index'},
  {unit:'throughput', underlying:'model'},
  {unit:'unsure', underlying:'index'},
  {unit:'unsure', underlying:'model'},
];
async function localHtml(context) {
  for (const path of ['buy**', 'suppliers', 'providers']) {
    await context.route(`${base}/${path}`, async route => {
      const response = await route.fetch();
      await route.fulfill({response, contentType:'text/html'});
    });
  }
}
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined});
  try {
    for (const width of [390,1440,320,768]) {
      for (const [index, path] of (width === 390 || width === 1440 ? paths : paths.slice(0,1)).entries()) {
        const context = await browser.newContext({viewport:{width,height:1000}});
        await localHtml(context);
        const events = [], requests = [], errors = [];
        context.on('request', request => requests.push(request));
        await context.route('**/token-exchange/events', route => {
          events.push(route.request().postDataJSON());
          return route.fulfill({status:204});
        });
        await context.route('**/v1/models', route => route.fulfill(path.failed ? {status:503,body:''} :
          {json:{data:[{id:'public/model',name:'Public model'}]}}));
        await context.route('**/v1/providers', route => route.fulfill(path.failed ? {status:503,body:''} :
          {json:{data:[{id:'supplier',name:'Supplier',routing_status:'active'}, {id:'blocked',routing_status:'blocked'}]}}));
        await context.route('https://calendly.com/**', route => route.fulfill({contentType:'text/html',
          body:'<!doctype html><html lang="en"><title>Calendly test fixture</title><body style="font:18px Arial;padding:24px;color:#173b2c"><h1>Calendly booking frame</h1><p>Local verification fixture — no booking is made.</p><p>15-minute reserved tokens call with Joseph</p></body></html>'}));
        const page = await context.newPage();
        page.on('pageerror', error => errors.push(error.message));
        await page.goto(`${base}/buy?grade=A&unit=blocks&name=ignored&step=1`);
        const check = async step => {
          assert.equal(new URL(page.url()).searchParams.get('step'), String(step));
          assert.equal(await page.locator('.buy-step:visible').getAttribute('data-step'), String(step));
          assert.equal(await page.locator('.buy-steps [aria-current="step"]').count(),1);
          assert.equal(await page.evaluate(() => document.activeElement.id), `buy-title-${step}`);
          assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth &&
            document.getElementById('buy-flow').scrollWidth <= document.getElementById('buy-flow').clientWidth), `overflow ${width} step ${step}`);
          assert.equal(await page.locator('form').count(),0);
          if (index === 0 && [390,1440].includes(width)) {
            await page.evaluate(() => document.fonts.ready);
            await page.screenshot({path:`${shots}/step-${step}-${width}.png`,fullPage:true});
          }
        };
        await check(1);
        assert.equal(await page.title(), 'Reserved tokens | New York Token Exchange');
        if (width < 1000) {
          const wizard = await page.locator('.buy-workspace').boundingBox();
          const brand = await page.locator('.buy-brand').boundingBox();
          assert(wizard.y + wizard.height <= brand.y, 'wizard precedes brand on mobile');
        }
        assert.equal(new URL(page.url()).searchParams.has('name'), false);
        assert(await page.locator('[name="grade"][value="A"]').isChecked());
        assert.equal(await page.locator('.buy-seller a').first().getAttribute('href'),'/suppliers');
        assert.equal(await page.locator('.buy-seller a').last().getAttribute('href'),'/providers');
        if (index === 0 && [390,1440].includes(width)) {
          for (const [destination, kind] of [['suppliers','sup'], ['providers','providers']]) {
            await page.locator(`.buy-seller a[href="/${destination}"]`).click();
            await page.waitForURL(`**/${destination}`);
            assert.equal(await page.locator('body').getAttribute('data-nyte-page'), kind);
            await page.goBack();
            await check(1);
          }
        }
        await page.locator(`[name="unit"][value="${path.unit}"]`).check();
        await page.getByRole('button',{name:'Continue to details'}).click();
        await check(2);
        await page.goBack();
        await check(1);
        await page.goForward();
        await check(2);
        const expectedStart = await page.evaluate(() => {
          const today = new Date();
          const next = new Date(today.getFullYear(), today.getMonth() + 1, 1);
          return `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2,'0')}`;
        });
        assert.equal(await page.locator('#buy-start').inputValue(), expectedStart);
        if (path.unit !== 'throughput') {
          await page.getByRole('button',{name:'Continue to contact'}).click();
          await check(3); // The default date allows the first Continue to succeed.
          await page.getByRole('button',{name:'Back',exact:true}).click();
        }
        await page.locator('#buy-start').fill('');
        await page.getByRole('button',{name:'Continue to contact'}).click();
        assert.equal(new URL(page.url()).searchParams.get('step'),'2');
        await page.locator('#buy-start').fill('2027-02');
        const term = index === 0 ? '1' : '6';
        await page.locator('#buy-term').selectOption(term);
        if (path.unit === 'blocks') {
          await page.getByRole('button',{name:'Add one block'}).click();
          await page.getByRole('button',{name:'Add one block'}).click();
          assert.equal(await page.locator('#buy-blocks').inputValue(),'3');
          assert((await page.locator('#buy-total').innerText()).includes('$2,670'));
          await page.locator('#buy-blocks').fill('0');
          await page.getByRole('button',{name:'Continue to contact'}).click();
          assert.equal(new URL(page.url()).searchParams.get('step'),'2');
          await page.locator('#buy-blocks').fill('3');
        }
        if (path.unit === 'throughput') await page.locator('#buy-rate').fill('1200');
        // Check native hiding, disabled controls, rendering and accessible groups in both directions.
        for (const kind of ['model', 'index', path.underlying]) {
          await page.locator(`[name="underlying"][value="${kind}"]`).check();
          for (const [id, label, active] of [
            ['buy-grades', 'Choose a grade', kind === 'index'],
            ['buy-model-fields', 'Model and provider', kind === 'model'],
          ]) {
            assert.deepEqual(await page.locator(`#${id}`).evaluate(el => ({hidden:el.hidden, disabled:el.disabled})),
              {hidden:!active, disabled:!active});
            assert.equal(await page.locator(`#${id}`).isVisible(), active);
            assert.equal(await page.getByRole('group', {name:label, exact:true}).count(), active ? 1 : 0);
          }
        }
        if (path.underlying === 'model') {
          await page.getByRole('button',{name:'Continue to contact'}).click();
          assert.equal(new URL(page.url()).searchParams.get('step'),'2');
          await page.locator('#buy-model').fill(path.failed ? 'Custom model & test' : 'public/model');
          if (!path.failed) {
            await page.waitForFunction(() => document.querySelectorAll('#buy-models option').length === 1 &&
              document.querySelectorAll('#buy-providers option').length === 1);
            assert.equal(await page.locator('#buy-providers option').count(),1);
          }
          if (path.provider) await page.locator('#buy-provider').fill('supplier');
        }
        if (path.underlying === 'model' || path.unit !== 'blocks') assert(!(await page.locator('#buy-total').innerText()).includes('$'));
        await page.getByRole('button',{name:'Continue to contact'}).click();
        await check(3);
        await page.getByRole('button',{name:'Back',exact:true}).click();
        assert.equal(await page.locator('#buy-term').inputValue(),term);
        await page.getByRole('button',{name:'Continue to contact'}).click();
        await page.locator('#buy-name').fill(name);
        await page.locator('#buy-email').fill('invalid@work');
        await page.getByRole('button',{name:'Continue to scheduling'}).click();
        assert.equal(new URL(page.url()).searchParams.get('step'),'3');
        await page.locator('#buy-email').fill(email);
        await page.locator('#buy-company').fill(company);
        await page.getByRole('button',{name:'Continue to scheduling'}).click();
        await check(4);
        const src = await page.locator('#buy-calendar iframe').getAttribute('src');
        const booking = new URL(src);
        assert.equal(booking.origin,'https://calendly.com');
        assert.equal(booking.pathname,'/joseph-perla/15min');
        for (const [key,value] of Object.entries({embed_domain:'nytokenexchange.com',embed_type:'Inline',hide_gdpr_banner:'1',name,email})) {
          assert.equal(booking.searchParams.get(key), value);
        }
        const summary = booking.searchParams.get('a1');
        for (const part of [`Unit: ${path.unit}`, `term: ${term} ${term === '1' ? 'month' : 'months'};`, 'start: 2027-02', company]) assert(summary.includes(part));
        assert.equal(await page.locator('#buy-summary').innerText(),summary);
        assert.equal(await page.locator('#buy-calendly-link').getAttribute('href'),src);
        await page.goBack();
        await check(3);
        assert.equal(await page.locator('#buy-calendar iframe').count(),0);
        assert(!new URL(await page.locator('#buy-calendly-link').getAttribute('href')).searchParams.has('email'));
        assert.equal(await page.locator('#buy-name').inputValue(),name);
        await page.goForward();
        await check(4);
        // Back/forward keeps in-memory fields, but reload cannot retain contact data or skip validation.
        await page.reload();
        await page.waitForFunction(expected => new URLSearchParams(location.search).get('step') === expected,
          path.unit === 'throughput' ? '2' : '3');
        assert.equal(await page.locator('#buy-name').inputValue(),'');
        assert.equal(await page.locator('#buy-calendar iframe').count(),0);
        const buyEvents = events.filter(event => event.event.startsWith('nyte.buy_'));
        assert(buyEvents.some(event => event.event === 'nyte.buy_completed'));
        assert.equal(buyEvents.filter(event => event.event === 'nyte.buy_completed').length,1);
        for (const event of buyEvents) {
          assert.equal(event.page,'/buy');
          assert(['blocks','throughput','unsure'].includes(event.unit));
          assert(['index','model'].includes(event.underlying_kind));
          assert(['AAA','A','B','C','none'].includes(event.grade));
        }
        const storage = await page.evaluate(() => JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)]));
        for (const privateValue of [name,email,company]) {
          assert(!JSON.stringify(events).includes(privateValue));
          assert(!storage.includes(privateValue));
          for (const request of requests.filter(request => new URL(request.url()).hostname !== 'calendly.com')) {
            assert(!decodeURIComponent(request.url()).includes(privateValue));
            assert(!(request.postData() || '').includes(privateValue));
          }
        }
        assert.deepEqual(errors,[]);
        await context.close();
        console.log(`PASS ${width}px ${path.unit}/${path.underlying}${path.failed ? ' failed catalogue' : ''}: validation, history, pricing, iframe, privacy`);
      }
    }
    for (const width of [390,1440,320,768]) {
      const context = await browser.newContext({javaScriptEnabled:false, viewport:{width,height:1000}});
      await localHtml(context);
      const page = await context.newPage();
      await page.goto(`${base}/buy.html`);
      assert.equal(await page.locator('.buy-step:visible').count(),4);
      assert.equal(await page.locator('#buy-start').getAttribute('required'), null);
      assert(await page.locator('#buy-model').isVisible());
      assert(await page.locator('#buy-provider').isVisible());
      assert(await page.locator('#buy-calendly-link').isVisible());
      assert.equal(await page.locator('form,iframe').count(),0);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await context.close();
    }
    const context = await browser.newContext();
    await localHtml(context);
    await context.route('**/token-exchange/events', route => route.fulfill({status:204}));
    const page = await context.newPage();
    for (const grade of ['AAA','A','B','C']) {
      await page.goto(`${base}/buy?grade=${grade}&unit=blocks&step=2`);
      assert(await page.locator(`[name="grade"][value="${grade}"]`).isChecked());
      assert.equal(await page.locator('.buy-step:visible').getAttribute('data-step'),'2');
    }
    await page.goto(`${base}/buy?grade=INVALID&unit=INVALID&step=99`);
    assert(await page.locator('[name="grade"][value="AAA"]').isChecked());
    assert.equal(new URL(page.url()).searchParams.get('step'),'1');
    await page.locator('[name="unit"][value="blocks"]').focus();
    await page.keyboard.press('ArrowRight');
    assert(await page.locator('[name="unit"][value="throughput"]').isChecked());
    await context.close();
    console.log(`PASS grade allowlists, keyboard radios and no-JS fallback at all four widths. Screenshots: ${shots}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
