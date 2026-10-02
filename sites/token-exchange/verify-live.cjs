// Run against a built staging tree. Uses real HTTP and the real one-minute timer.
const {chromium} = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const output = process.argv[2] || '/tmp/token-exchange-live';
let mode = 'healthy';
let feedRequests = 0;
let states = ['up', 'up'];
let attestationState = 'up';
let historyState = 'up';
let dateMode = 'normal';
let omitComponents = false;
const stamp = () => new Date().toISOString();
function evidence() {
  const checked = dateMode === 'missing' ? null : dateMode === 'future' ? new Date(Date.now()+86400000).toISOString() : mode === 'stale' ? new Date(Date.now() - 5 * 86400000).toISOString() : stamp();
  const component = (id, name, percent, status) => ({id, name, status, last_checked_at: checked, uptime_24h_percent: percent, sample_count_24h: 1000, history: [{bucket_start: checked, status:historyState, sample_count: 1000}]});
  return {generated_at: stamp(), prices: [
    {model:'z-ai/glm-5.3-flash', label:'GLM 5.3 Flash', provider:'tinfoil', input:'0.07385', output:'0.2532'},
    {model:'deepseek/deepseek-v4.1-flash', label:'DeepSeek V4.1 Flash', provider:'tinfoil', input:'0.68575', output:'1.52975'},
    {model:'openai/gpt-oss-120b', label:'GPT OSS 120B', provider:'tinfoil', input:'0.15825', output:'0.633'}],
    components: omitComponents ? [] : [component('canonical_api','Canonical API',99.9485,states[0]), component('us_east4_regional_api','US East Regional API',99.6910,states[1])],
    attestation_check: component('attestation','Attestation',100,attestationState), release: {platform:'gcp-confidential-space', image_digest:'sha256:'+'a'.repeat(64),source_commit:'12345678'}};
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
      assert.equal(await page.locator('[data-live-state]').textContent(),'');
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
    console.log('Two staging domains show exact prices, rounded uptime and dated attestation; responsive widths pass.');
    const reload = async page => {
      await page.reload();
      await page.waitForFunction(()=>document.querySelector('[data-live-prices]').textContent.includes('$'));
    };
    const quiet = async page => {
      assert.equal(await page.locator('[data-live-state]').textContent(),'');
      assert.equal(await page.locator('.service-history').count(),0);
      assert.equal(await page.locator('.health-bar').count(),0);
      assert.match(await page.locator('[data-live-caption]').textContent(),/service status/);
      assert(await page.getByRole('link',{name:'Service status ↗',exact:true}).isVisible());
    };
    mode='stale';await reload(pages[0]);await quiet(pages[0]);
    assert.equal(await pages[0].locator('[data-live-attestation]').textContent(),'');
    mode='healthy';
    // Every real upstream status (plus an unexpected value) must fall back
    // quietly without a partial "Operational" claim or a misleading green bar.
    const ordered=['up','degraded','routing_degraded','trust_degraded','unknown','down','stale','unexpected'];
    for (const a of ordered) for (const b of ordered) {
      states=[a,b];await reload(pages[0]);
      if (a==='up' && b==='up') assert.equal(await pages[0].locator('.service-history').count(),2);
      else await quiet(pages[0]);
    }
    states=['up','up'];
    for (const status of ordered) {
      attestationState=status;await reload(pages[0]);
      assert.equal(await pages[0].locator('.build-digest').count(),status==='up'?1:0);
      if(status!=='up') assert.equal(await pages[0].locator('[data-live-attestation]').textContent(),'');
    }
    attestationState='up';
    for (const invalid of ['missing','future']) {
      dateMode=invalid;await reload(pages[0]);await quiet(pages[0]);
      assert.equal(await pages[0].locator('[data-live-attestation]').textContent(),'');
    }
    dateMode='normal';omitComponents=true;await reload(pages[0]);await quiet(pages[0]);
    omitComponents=false;
    for (const status of ['down','trust_degraded','unknown']) {
      historyState=status;await reload(pages[0]);
      assert.equal(await pages[0].locator('.service-history').count(),2);
      assert.equal(await pages[0].locator('.health-bar').count(),0);
      assert.match(await pages[0].locator('[data-live-services]').textContent(),/99.95%/);
    }
    historyState='up';
    // Already-cached evidence must be refreshed before its component expires.
    const timedContext=await browser.newContext({reducedMotion:'reduce'});
    const timed=await timedContext.newPage();
    const base=new Date();let timedRequests=0;
    await timed.clock.install({time:base});await timed.clock.pauseAt(base);
    await timed.route('https://trustedrouter.com/token-exchange/evidence/*.json',route=>{
      timedRequests++;
      const data=evidence();
      const checked=new Date(base.getTime()+(timedRequests===1?-270000:60000)).toISOString();
      data.generated_at=new Date(base.getTime()+(timedRequests===1?-50000:60000)).toISOString();
      data.components.forEach(c=>{c.last_checked_at=checked;});
      data.attestation_check.last_checked_at=checked;
      return route.fulfill({json:data});
    });
    await timed.goto('http://nytokenexchange.com:8091/');
    await timed.waitForFunction(()=>document.querySelector('.service-history'));
    await timed.clock.runFor(61000);
    await timed.waitForFunction(stamp=>document.querySelector('[data-live-services]').textContent.includes(stamp),new Date(base.getTime()+60000).toISOString().replace('T',' ').replace(/\.\d+Z$/,' UTC'));
    assert.equal(timedRequests,2);
    assert.equal(await timed.locator('.service-history').count(),2);
    await timedContext.close();
    // Capture the quiet fallback and its recovery at each supported width.
    for(const page of pages) {
      states=['up','trust_degraded'];attestationState='trust_degraded';await reload(page);
      for(const width of [320,390,1440]) {
        await page.setViewportSize({width,height:1000});
        await page.locator('.trust-evidence').scrollIntoViewIfNeeded();
        await quiet(page);
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        await page.locator('.trust-evidence').screenshot({path:`/tmp/te-${new URL(page.url()).hostname}-quiet-${width}.png`});
      }
      states=['up','up'];attestationState='up';await reload(page);
      assert.equal(await page.locator('.service-history').count(),2);
      assert.equal(await page.locator('.build-digest').count(),1);
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: 64 status pairs, attestation failures, stale/missing/future dates, honest history fallback, cache-age refresh, responsive fallback and recovery.');
    if (process.argv.includes('--quick')) return;
    await pages[0].reload();await pages[0].waitForFunction(()=>document.querySelector('[data-live-services]').textContent.includes('99.95%'));
    const started=Date.now();const before=feedRequests;
    await new Promise(r=>feed.close(r));
    console.log('Staging feed stopped; waiting for the real 60-second revalidation (no clock acceleration).');
    for (const page of pages) {
      await page.waitForFunction(()=>document.querySelector('[data-live-state]').textContent === '' && document.querySelector('[data-live-services]').children.length === 0,{},{timeout:80000});
      assert.equal(await page.locator('.health-bar').count(),0);
      assert.equal(await page.locator('.build-digest').count(),0);
      assert(!((await page.locator('[data-live-prices]').textContent()).includes('$')));
      await page.locator('.trust-evidence').screenshot({path:`/tmp/te-${new URL(page.url()).hostname}-failed.png`});
    }
    assert.equal(feedRequests,before);
    assert.deepEqual(errors,[]);
    console.log(`PASS: feed stopped; both already-open domains returned to source links after ${((Date.now()-started)/1000).toFixed(1)}s.`);
  } finally { await browser.close();site.close();feed.close(); }
})().catch(e=>{console.error(e);process.exit(1);});
