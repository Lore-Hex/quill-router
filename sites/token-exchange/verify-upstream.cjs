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
    for(const market of ['new-york','london']) {
      const page=await context.newPage();await page.goto('http://127.0.0.1:8089/'+market+'/');
      await page.waitForFunction(()=>document.querySelector('[data-live-services]').textContent.includes('99.95%'));
      assert.equal(await page.locator('.service-history').count(),market==='new-york'?2:1);
      assert.equal(await page.locator('.privacy-column').count(),3);
      pages.push(page);
    }
    const response=await context.request.post('http://127.0.0.1:8094/__stage/stop-status');
    assert(response.ok());const stopped=Date.now();
    console.log('Actual upstream status HTTP server stopped; production backend and its 60s cache remain running.');
    await Promise.all(pages.map(async page => {
      await page.waitForFunction(()=>document.querySelector('[data-live-state]').textContent === '' && document.querySelector('[data-live-services]').children.length === 0,{},{timeout:80000, polling:100});
      assert.equal(await page.locator('.service-history').count(),0);
      assert.equal(await page.locator('.build-digest').count(),0);
      // Pricing is independently sourced from the backend catalog and stays valid.
      assert.equal(await page.locator('.privacy-column').count(),3);
    }));
    assert(Date.now()-stopped<80000,'Exceeded the revalidation deadline');
    console.log(`PASS upstream failure through real backend cache: two markets returned to source links after ${((Date.now()-stopped)/1000).toFixed(1)}s; independent catalog prices remain.`);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
