// Requires stage_evidence.py on :8094 and a built static site on :8089.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext({reducedMotion:'reduce'});
    await context.route('https://trustedrouter.com/token-exchange/evidence/*.json',async route=>{
      const url='http://127.0.0.1:8094'+new URL(route.request().url()).pathname;
      await route.fulfill({response:await route.fetch({url})});
    });
    const pages=[];
    const renderedPrices=page=>page.locator('.privacy-column').evaluateAll(columns=>columns.map(col=>({
      href:col.href,
      label:col.querySelector('h3').textContent,
      price:col.querySelector('.privacy-price').textContent,
    })));
    for(const market of ['new-york','london']) {
      const page=await context.newPage();await page.goto('http://127.0.0.1:8089/'+market+'/');
      await page.waitForFunction(()=>document.querySelector('[data-live-services]').textContent.includes('99.95%'));
      assert.equal(await page.locator('.service-history').count(),market==='new-york'?2:1);
      const profile=await page.locator('[data-evidence-profile]').getAttribute('data-evidence-profile');
      const feed=await context.request.get(`http://127.0.0.1:8094/token-exchange/evidence/${profile}.json`);
      assert(feed.ok());
      const {prices}=await feed.json();
      // Catalog delistings legitimately change the number of available routes.
      // Verify exact catalog-backed prices, not a stale fixed model count.
      assert(Array.isArray(prices)&&prices.length>0,'Outage rig needs available catalog prices');
      const expectedPrices=prices.map(row=>({
        href:`https://trustedrouter.com/models/${encodeURI(row.model)}#provider-tinfoil`,
        label:row.label,
        price:`$${row.input} / $${row.output}`,
      }));
      assert.deepEqual(await renderedPrices(page),expectedPrices);
      pages.push({page,expectedPrices});
    }
    const response=await context.request.post('http://127.0.0.1:8094/__stage/stop-status');
    assert(response.ok());const stopped=Date.now();
    console.log('Actual upstream status HTTP server stopped; production backend and its 60s cache remain running.');
    await Promise.all(pages.map(async ({page,expectedPrices}) => {
      await page.waitForFunction(()=>document.querySelector('[data-live-state]').textContent === '' && document.querySelector('[data-live-services]').children.length === 0,{},{timeout:80000, polling:100});
      assert.equal(await page.locator('.service-history').count(),0);
      assert.equal(await page.locator('.build-digest').count(),0);
      // Pricing is independently sourced from the backend catalog and stays valid.
      assert.deepEqual(await renderedPrices(page),expectedPrices);
    }));
    assert(Date.now()-stopped<80000,'Exceeded the revalidation deadline');
    console.log(`PASS upstream failure through real backend cache: two markets returned to source links after ${((Date.now()-stopped)/1000).toFixed(1)}s; independent catalog prices remain.`);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
