// Run against a built preview with Playwright available in NODE_PATH.
const {chromium}=require('playwright');
const base=process.env.PREVIEW_URL || (process.argv[2] ? 'http://127.0.0.1:8096' : 'http://127.0.0.1:8098');const assert=require('assert');
(async()=>{
let server;
if(process.argv[2]){
 const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
 const root=path.resolve(process.argv[2]);
 server=http.createServer((req,res)=>{
  const pathname=new URL(req.url,base).pathname;
  const file=path.resolve(root,'.'+(pathname==='/'?'/new-york/index.html':pathname));
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
  const ext=path.extname(file);
  const mime={'.js':'text/javascript','.css':'text/css','.html':'text/html','.webp':'image/webp','.png':'image/png','.jpg':'image/jpeg','.svg':'image/svg+xml','.mp4':'video/mp4','.webm':'video/webm','.ttf':'font/ttf'}[ext]||'text/html';
  res.writeHead(200,{'Content-Type':mime});fs.createReadStream(file).pipe(res);
 });
 await new Promise(resolve=>server.listen(8096,'127.0.0.1',resolve));
}
const browser=await chromium.launch();const page=await browser.newPage({viewport:{width:390,height:844}});const events=[];const failures=[];
page.on('pageerror',e=>failures.push(e.message));
await page.route('**/token-exchange/events',async route=>{events.push(route.request().postDataJSON());await route.fulfill({status:204});});
const now=new Date().toISOString();let evidence={generated_at:now,prices:[{model:'openai/gpt-oss-120b',label:'GPT OSS 120B',provider:'tinfoil',input:'0.15825',output:'0.633'}],components:[{id:'canonical_api',name:'Canonical API',status:'up',last_checked_at:now,uptime_24h_percent:100,sample_count_24h:24,history:Array.from({length:24},()=>({bucket_start:now,sample_count:1,status:'up'}))}],attestation_check:{status:'up',last_checked_at:now},release:{platform:'gcp-confidential-space',image_digest:'sha256:'+'a'.repeat(64),source_commit:'abcdef12345'},catalogue:{models:620,providers:92}};
await page.route('**/token-exchange/evidence/**',r=>r.fulfill({json:evidence}));
let downloads=0;let success=false;
await page.route('**/token-exchange/brief',r=>{downloads++;const data=r.request().postDataJSON();assert.equal(data.resource,'brochure');assert.equal(data.website,'');return r.fulfill(success?{status:200,contentType:'application/pdf',body:'%PDF-1.4\n%%EOF'}:{status:429,body:''});});
let modelCount=731, providerRows=[{id:'one',routing_status:'active'},{id:'two',routing_status:'blocked'}];
let catalogueUnavailable=false;
await page.route('**/v1/models/count', r=>r.fulfill(catalogueUnavailable?{status:503,body:''}:{json:{data:{count:modelCount}}}));
await page.route('**/v1/providers', r=>r.fulfill(catalogueUnavailable?{status:503,body:''}:{json:{data:providerRows}}));
await page.clock.install();
await page.goto(base+'/');
for (const width of [1440,390]) {
 await page.setViewportSize({width,height:844});
 const rows=await page.locator('.r-row.skip').evaluateAll(elements=>elements.map(el=>({position:getComputedStyle(el).position,top:el.getBoundingClientRect().top})));
 assert.equal(rows.length,2);assert(rows.every(row=>row.position!=='absolute' && row.top>0));
}
await page.getByRole('button',{name:'Menu',exact:true}).click();
await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')),'/confidential');
await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.id),'menuBtn');
assert.equal(await page.locator('#menuBtn').getAttribute('aria-expanded'),'false');

const played=page.waitForRequest(request=>request.url().endsWith('/token-exchange/events') && request.postDataJSON()?.event==='nyte.video_played');
await page.getByRole('button',{name:'Play the 90-second walkthrough',exact:true}).click();
await page.waitForFunction(()=>document.getElementById('film').classList.contains('playing'));
assert.equal((await played).postDataJSON().event,'nyte.video_played');
const counts={models:731,providers:1};
await page.waitForFunction(()=>document.querySelector('[data-catalog-models]').textContent==='731');
assert.equal(await page.locator('[data-catalog-models]').textContent(),counts.models.toLocaleString());
assert.equal(await page.locator('[data-catalog-providers]').textContent(),counts.providers.toLocaleString());
assert.equal(await page.locator('.catalog-source').count(),0);
modelCount=812;providerRows.push({id:'three',routing_status:'active'});
await page.clock.fastForward(60000);
await page.waitForFunction(()=>document.querySelector('[data-catalog-models]').textContent==='812' && document.querySelector('[data-catalog-providers]').textContent==='2');
catalogueUnavailable=true;await page.clock.fastForward(60000);
await page.waitForFunction(()=>document.querySelector('[data-catalog-models]').textContent==='—' && document.querySelector('[data-catalog-providers]').textContent==='—');
catalogueUnavailable=false;modelCount=-1;providerRows=[{id:'unknown'}];await page.clock.fastForward(60000);
assert.equal(await page.locator('[data-catalog-models]').textContent(),'—');
assert.equal(await page.locator('[data-catalog-providers]').textContent(),'—');
modelCount=813;providerRows=[{id:'restored',routing_status:'active'}];await page.clock.fastForward(60000);
await page.waitForFunction(()=>document.querySelector('[data-catalog-models]').textContent==='813');
await page.clock.resume();
await page.getByRole('button',{name:'Menu',exact:true}).click();await page.locator('nav').getByRole('link',{name:'Confidential API',exact:true}).click();await page.waitForURL('**/confidential');
await page.locator('.privacy-price').first().waitFor();assert.match(await page.locator('[data-live-price-unit]').textContent(),/^Updated .* UTC$/);assert.equal(await page.locator('.tbl-foot').count(),0);assert.equal(await page.locator('.health-bar').count(),24);assert.equal(await page.locator('.build-digest').count(),1);
assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
await page.locator('.hero-api a',{hasText:'Start building'}).click();await page.waitForURL('**/confidential#start');
await page.getByRole('button',{name:'Get the buyer deck',exact:true}).click();assert(await page.locator('dialog').isVisible());
await page.getByRole('button',{name:'Download the deck',exact:true}).click();assert.equal(downloads,0);
await page.locator('input[name=email]').fill('preview@example.com');await page.getByRole('button',{name:'Download the deck',exact:true}).click();await page.waitForFunction(()=>document.getElementById('brochure-status').dataset.state==='error');assert.equal(downloads,1);assert(!(events.some(e=>e.event==='nyte.form_submitted')));
success=true;const download=page.waitForEvent('download');await page.getByRole('button',{name:'Download the deck',exact:true}).click();assert.equal((await download).suggestedFilename(),'NYTE-Buyer-Deck.pdf');await page.waitForFunction(()=>document.getElementById('brochure-status').dataset.state==='success');
await page.keyboard.press('Escape');assert(!(await page.locator('dialog').isVisible()));
await page.waitForTimeout(200);assert(events.some(e=>e.event==='nyte.form_submitted'&&e.form==='buyer_deck'));assert(events.some(e=>e.event==='nyte.cta_clicked'&&e.cta==='api_quick_start'));assert(events.every(e=>!('email' in e)));
evidence={...evidence,generated_at:'2000-01-01T00:00:00Z'};await page.reload();await page.waitForTimeout(200);assert.equal(await page.locator('.build-digest').count(),0);assert.equal(await page.locator('.health-bar').count(),0);assert(await page.getByRole('link',{name:'Published release'}).isVisible());

await page.setViewportSize({width:1440,height:900});
await page.goto(base+'/index');
await page.getByRole('link',{name:'Start building',exact:true}).click();await page.waitForURL('**/confidential#start');assert.equal(await page.locator('#start').count(),1);
await page.goto(base+'/index');
await page.getByRole('link',{name:'Explore the open-source inference stack',exact:true}).click();await page.waitForURL('**/suppliers#open-stack');assert.equal(await page.locator('#open-stack').count(),1);
// Keyboard focus reveals even the partly clipped final market link.
for (const width of [1440,390]) {
 await page.setViewportSize({width,height:900});await page.goto(base+'/');
 await page.evaluate(()=>document.fonts.ready);
 await page.locator('.market-links a').first().focus();
 const links=await page.locator('.market-links a').count();
 for (let index=1;index<links;index++) await page.keyboard.press('Tab');
 const focused=await page.locator('.market-links').evaluate(nav=>{
  const link=document.activeElement, box=link.getBoundingClientRect(), rail=nav.getBoundingClientRect();
  return {last:link===nav.lastElementChild,visible:box.left>=rail.left-1 && box.right<=rail.right+1};
 });
 assert(focused.last && focused.visible,`footer market focus must be visible at ${width}px`);
}
// Narrow-phone buttons keep their approved labels inside their touch targets.
for (const path of ['/', '/confidential', '/suppliers']) {
 await page.setViewportSize({width:320,height:844});
 await page.goto(base+path);await page.evaluate(()=>document.fonts.ready);
 const overflowing=await page.locator('.btns .btn').evaluateAll(elements=>elements
  .filter(el=>el.getClientRects().length && el.scrollWidth>el.clientWidth+1)
  .map(el=>el.textContent.trim()));
 assert.deepEqual(overflowing,[],`${path}: button text must fit at 320px`);
}
// The shared mobile header retains its primary CTA, and the API hero follows its content.
for (const width of [320, 360, 390, 430, 768, 880]) {
 await page.setViewportSize({width,height:844});await page.goto(base+'/confidential');await page.evaluate(()=>document.fonts.ready);
 const cta=page.locator('header .btn-sm');assert(await cta.isVisible());
 assert.equal(await cta.getAttribute('href'),'/confidential#start');
 const layout=await page.evaluate(()=>{
  const header=document.querySelector('.hdr'),hero=document.querySelector('.hero-api');
  return {overflow:document.documentElement.scrollWidth>innerWidth || header.scrollWidth>header.clientWidth,
   brandGap:document.querySelector('.hdr>.btn-sm').getBoundingClientRect().left-header.querySelector('.brand').getBoundingClientRect().right,
   gap:hero.getBoundingClientRect().bottom-hero.querySelector('.btns').getBoundingClientRect().bottom};
 });
 assert(!layout.overflow,`header must fit at ${width}px`);
 if(width<=600)assert(layout.brandGap>=24,`wordmark needs breathing room at ${width}px`);
 assert(layout.gap<=(width<=600?41:81),`API hero must follow its content at ${width}px`);
}
// Published index dates and movements remain readable on phones and desktop.
for (const width of [320,390,768,1440]) {
 await page.setViewportSize({width,height:900});await page.goto(base+'/suppliers');await page.evaluate(()=>document.fonts.ready);
 const card=page.locator('.idx');
 const iso=await card.locator('time').getAttribute('datetime');
 assert.equal(await card.locator('time').textContent(),`${iso.slice(5,7)}/${iso.slice(8,10)}/${iso.slice(2,4)}`);
 assert.equal(await card.locator('.idx-change').count(),5);
 assert(await card.locator('.idx-change').evaluateAll(elements=>elements.every(el=>{
  const r=el.getBoundingClientRect();return r.width>0 && el.scrollWidth<=el.clientWidth && r.right<=innerWidth;
 })),`index changes must fit at ${width}px`);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.goto(base+'/');await page.evaluate(()=>document.fonts.ready);
 assert(await page.locator('.stat .v').evaluateAll(elements=>elements.every(el=>el.scrollWidth<=el.clientWidth)),`stats must fit at ${width}px`);
 const fee=page.locator('.stat .v span');assert.equal(await fee.textContent(),'max fee');
 assert.equal(await fee.evaluate(el=>el.getClientRects().length),1);
}
assert.deepEqual(failures,[]);console.log('PASS: mobile navigation, quick start, valid live evidence, stale fallback, PDF success/error, analytics, no PII');await browser.close();if(server)await new Promise(resolve=>server.close(resolve));
})().catch(error=>{console.error(error);process.exit(1);});
