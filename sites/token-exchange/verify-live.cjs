// Run against a built staging tree. Uses real HTTP and the real five-minute timer.
const {chromium} = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const output = process.argv[2] || '/tmp/token-exchange-live';
let mode = 'healthy';
let feedRequests = 0;
let states = ['up', 'degraded'];
let dateMode = 'normal';
let omitComponents = false;
const stamp = () => new Date().toISOString();
function evidence() {
  const checked = dateMode === 'missing' ? null : dateMode === 'future' ? new Date(Date.now()+86400000).toISOString() : mode === 'stale' ? new Date(Date.now() - 5 * 86400000).toISOString() : stamp();
  const component = (id, name, percent, status) => ({id, name, status, last_checked_at: checked, uptime_24h_percent: percent, sample_count_24h: 1000, history: [{bucket_start: checked, status, sample_count: 1000}]});
  return {generated_at: stamp(), prices: [
    {model:'z-ai/glm-5.3-flash', label:'GLM 5.3 Flash', provider:'tinfoil', input:'0.07385', output:'0.2532'},
    {model:'deepseek/deepseek-v4.1-flash', label:'DeepSeek V4.1 Flash', provider:'tinfoil', input:'0.68575', output:'1.52975'},
    {model:'openai/gpt-oss-120b', label:'GPT OSS 120B', provider:'tinfoil', input:'0.15825', output:'0.633'}],
    components: omitComponents ? [] : [component('canonical_api','Canonical API',99.9485,states[0]), component('us_east4_regional_api','US East Regional API',99.6910,states[1])],
    attestation_check: component('attestation','Attestation',100,'up'), release: {platform:'gcp-confidential-space', image_digest:'sha256:'+'a'.repeat(64),source_commit:'12345678'}};
}
const feed = http.createServer((req,res) => {
  feedRequests++;
  res.writeHead(200, {'Content-Type':'application/json','Access-Control-Allow-Origin':'*','Cache-Control':'no-store'});
  res.end(JSON.stringify(evidence()));
});
const site = http.createServer((req,res) => {
  let file = decodeURIComponent(new URL(req.url,'http://localhost').pathname);
  if (file === '/') file = req.headers.host.startsWith('nytokenexchange.com') ? '/new-york/index.html' : '/london/index.html';
  const target = path.join(output,file);
  if (!target.startsWith(path.resolve(output)+path.sep) || !fs.existsSync(target)) {res.writeHead(404);res.end();return;}
  const type = target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : target.endsWith('.html') ? 'text/html' : 'application/octet-stream';
  res.writeHead(200,{'Content-Type':type});fs.createReadStream(target).pipe(res);
});
(async () => {
  await new Promise(r=>feed.listen(8092,'127.0.0.1',r));
  await new Promise(r=>site.listen(8091,'127.0.0.1',r));
  const browser = await chromium.launch({headless:true, args:['--no-proxy-server','--host-resolver-rules=MAP nytokenexchange.com 127.0.0.1, MAP londontoken.exchange 127.0.0.1']});
  try {
    const context = await browser.newContext({reducedMotion:'reduce'});
    const errors=[];
    // The application script is unchanged. Redirect just the feed transport to staging.
    await context.route('https://trustedrouter.com/token-exchange/evidence/*.json', async route => {
      try { const response=await route.fetch({url:'http://127.0.0.1:8092/',timeout:3000}); await route.fulfill({response}); }
      catch { await route.abort('connectionrefused'); }
    });
    const pages=[];
    for (const domain of ['nytokenexchange.com','londontoken.exchange']) {
      const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
      await page.goto(`http://${domain}:8091/`);
      await page.waitForFunction(()=>document.querySelector('[data-live-services]').textContent.includes('99.95%'));
      assert.match(await page.locator('[data-live-services]').textContent(),/99.69%/);
      assert.equal(await page.locator('[data-live-state]').textContent(),'Degraded · US East Regional API');
      assert.match(await page.locator('[data-live-prices]').textContent(),/\$0.07385 \/ \$0.2532/);
      assert(!((await page.locator('[data-live-services]').textContent()).includes('Model Inference')));
      for (const width of [320,390,1440]) {
        await page.setViewportSize({width,height:1000});
        await page.locator('.trust-evidence').scrollIntoViewIfNeeded();
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`${domain} overflow ${width}`);
      }
      await page.locator('.trust-evidence').screenshot({path:`/tmp/te-${domain}-healthy.png`});
      pages.push(page);
    }
    console.log('Two staging domains show exact prices, rounded uptime, named worst state, dated attestation; responsive widths pass.');
    mode='stale';
    await pages[0].reload();
    await pages[0].waitForFunction(()=>document.querySelector('[data-live-services]').textContent.includes('Last check 5 days ago'));
    assert(!((await pages[0].locator('[data-live-services]').textContent()).includes('%')));
    assert.equal(await pages[0].locator('.health-bar').count(),0);
    assert.equal(await pages[0].locator('.build-digest').count(),0);
    mode='healthy';await pages[0].reload();
    await pages[0].waitForFunction(()=>document.querySelector('[data-live-services]').textContent.includes('99.95%'));
    // Exhaust all component-state combinations; exactly one worst-of label.
    const labels={up:'Operational',degraded:'Degraded',unknown:'No data',down:'Down'};
    const ordered=['up','degraded','unknown','down'];
    for (const a of ordered) for (const b of ordered) {
      states=[a,b];await pages[0].reload();
      const worst=ordered[Math.max(ordered.indexOf(a),ordered.indexOf(b))];
      const names=[a===worst?'Canonical API':null,b===worst?'US East Regional API':null].filter(Boolean);
      const expected=worst==='up'?'Operational':`${labels[worst]} · ${names.join(', ')}`;
      await pages[0].waitForFunction(value=>document.querySelector('[data-live-state]').textContent===value,expected);
    }
    for (const invalid of ['missing','future']) {
      dateMode=invalid;await pages[0].reload();
      await pages[0].waitForFunction(()=>document.querySelector('[data-live-services]').textContent.includes('Last check unavailable'));
      assert.equal(await pages[0].locator('[data-live-services] strong').count(),0);
      assert.equal(await pages[0].locator('.build-digest').count(),0);
    }
    dateMode='normal';omitComponents=true;await pages[0].reload();
    await pages[0].waitForFunction(()=>document.querySelector('[data-live-prices]').textContent.includes('$'));
    assert.equal(await pages[0].locator('.service-history').count(),0);
    omitComponents=false;states=['up','degraded'];
    console.log('PASS: all 16 state combinations, missing/future dates, and absent components fail closed.');
    if (process.argv.includes('--quick')) return;
    await pages[0].reload();await pages[0].waitForFunction(()=>document.querySelector('[data-live-services]').textContent.includes('99.95%'));
    const started=Date.now();const before=feedRequests;
    await new Promise(r=>feed.close(r));
    console.log('Staging feed stopped; waiting for the real 300-second revalidation (no clock acceleration).');
    for (const page of pages) {
      await page.waitForFunction(()=>document.querySelector('[data-live-state]').textContent.includes('Status unavailable'),{},{timeout:320000});
      assert.equal(await page.locator('.health-bar').count(),0);
      assert.equal(await page.locator('.build-digest').count(),0);
      assert(!((await page.locator('[data-live-prices]').textContent()).includes('$')));
      await page.locator('.trust-evidence').screenshot({path:`/tmp/te-${new URL(page.url()).hostname}-failed.png`});
    }
    assert.equal(feedRequests,before);
    assert.deepEqual(errors,[]);
    console.log(`PASS: feed stopped; both already-open domains visibly degraded after ${((Date.now()-started)/1000).toFixed(1)}s.`);
  } finally { await browser.close();site.close();feed.close(); }
})().catch(e=>{console.error(e);process.exit(1);});
