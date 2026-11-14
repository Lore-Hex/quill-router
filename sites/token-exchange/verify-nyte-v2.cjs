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
 const heroActions=page.locator('.hero .btns, .v3-hero .btns');
 assert.deepEqual(await heroActions.locator('a,button').allTextContents(),['Reserved tokens','Get the buyer deck']);
 assert.equal(await heroActions.getByRole('link',{name:'Reserved tokens',exact:true}).getAttribute('href'),'/buy');
 assert.equal(await heroActions.getByRole('button',{name:'Get the buyer deck',exact:true}).getAttribute('data-open'),'deck');
 assert(await page.locator('header').getByRole('link',{name:'Start building',exact:true}).isVisible());
 // v2 home: the two skipped routes stay in flow below the hero. v3 home: the protection-layers section is gone
 // and the walkthrough shows a round play button.
 const v3=await page.locator('.v3-hero').count()>0;
 if(v3){
  assert.equal(await page.locator('.v3-layers').count(),0);
  assert(await page.locator('#filmPlay .pb').isVisible());
 }else{
  const rows=await page.locator('.r-row.skip').evaluateAll(elements=>elements.map(el=>({position:getComputedStyle(el).position,top:el.getBoundingClientRect().top})));
  assert.equal(rows.length,2);assert(rows.every(row=>row.position!=='absolute' && row.top>0));
 }
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
await page.getByRole('button',{name:'Menu',exact:true}).click();await page.locator('#nav').getByRole('link',{name:'Confidential API',exact:true}).click();await page.waitForURL('**/confidential');
await page.locator('.privacy-price').first().waitFor();assert.match(await page.locator('[data-live-price-unit]').textContent(),/^Updated .* UTC$/);assert.equal(await page.locator('.tbl-foot').count(),0);assert.equal(await page.locator('.health-bar').count(),0);assert.equal(await page.locator('.uptime-figure').count(),1);assert.equal(await page.locator('.uptime-figure').textContent(),'100.00%');assert.equal(await page.locator('.build-digest').count(),1);assert.equal(await page.locator('.att-unproven dd').textContent(),'Not proven by attestation');assert.equal(await page.locator('.privacy-provider').first().textContent(),'Confidential + E2EE');
assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
await page.locator('.hero-api a',{hasText:'Start building'}).click();await page.waitForURL('**/confidential#start');
await page.getByRole('button',{name:'Get the buyer deck',exact:true}).click();assert(await page.locator('dialog').isVisible());
await page.getByRole('button',{name:'Download the deck',exact:true}).click();assert.equal(downloads,0);
await page.locator('input[name=email]').fill('preview@example.com');await page.getByRole('button',{name:'Download the deck',exact:true}).click();await page.waitForFunction(()=>document.getElementById('brochure-status').dataset.state==='error');assert.equal(downloads,1);assert(!(events.some(e=>e.event==='nyte.form_submitted')));
success=true;const download=page.waitForEvent('download');await page.getByRole('button',{name:'Download the deck',exact:true}).click();assert.equal((await download).suggestedFilename(),'NYTE-Buyer-Deck.pdf');await page.waitForFunction(()=>document.getElementById('brochure-status').dataset.state==='success');
await page.keyboard.press('Escape');assert(!(await page.locator('dialog').isVisible()));
await page.waitForTimeout(200);assert(events.some(e=>e.event==='nyte.form_submitted'&&e.form==='buyer_deck'));assert(events.some(e=>e.event==='nyte.cta_clicked'&&e.cta==='api_quick_start'));assert(events.every(e=>!('email' in e)));
evidence={...evidence,generated_at:'2000-01-01T00:00:00Z'};await page.reload();await page.waitForTimeout(200);assert.equal(await page.locator('.build-digest').count(),0);assert.equal(await page.locator('.uptime-figure').count(),0);assert(await page.getByRole('link',{name:'Published release'}).isVisible());

await page.setViewportSize({width:1440,height:900});
await page.goto(base+'/index');
await page.getByRole('link',{name:'Start building',exact:true}).click();await page.waitForURL('**/confidential#start');assert.equal(await page.locator('#start').count(),1);
await page.goto(base+'/index');
await page.getByRole('link',{name:'Explore the open-source inference stack',exact:true}).click();await page.waitForURL('**/suppliers#open-stack');assert.equal(await page.locator('#open-stack').count(),1);
// New York's pages list no other markets.
for (const path of ['/', '/confidential', '/suppliers', '/providers', '/trusted-cowork', '/buy', '/neocloud-accelerator', '/index', '/media-kit']) {
 await page.goto(base+path);
 assert.equal(await page.locator('.market-links, nav[aria-label="Exchange markets"], .footer-markets').count(),0,`${path} lists no other markets`);
 const others=await page.locator('a[href]').evaluateAll(links=>links.map(a=>new URL(a.href).hostname).filter(h=>/tokenexchange\.com$|token\.exchange$/.test(h)&&h!=='nytokenexchange.com'&&!h.endsWith('.nytokenexchange.com')));
 assert.deepEqual(others,[],`${path} links no other exchange`);
}
// Narrow-phone buttons keep their approved labels inside their touch targets.
for (const path of ['/', '/confidential', '/suppliers', '/providers', '/trusted-cowork', '/buy', '/neocloud-accelerator']) {
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
}

// Keep both downloads in the first screen, including short phones.
// Brand v3 puts the screenshot inside the hero, 40px under the downloads (the approved phone layout).
for (const [width,height] of [[320,568],[390,667],[390,844]]) {
 await page.setViewportSize({width,height});await page.goto(base+'/trusted-cowork');await page.evaluate(()=>document.fonts.ready);
 const layout=await page.evaluate(()=>({bottom:document.querySelector('.cw-hero .btn-secondary').getBoundingClientRect().bottom,image:document.querySelector('.cw-screenshot').getBoundingClientRect().top,v3:!!document.querySelector('.cw-hero .cw-screenshot')}));
 assert(layout.bottom<=height-24,`downloads must fit ${width}x${height}`);
 assert(layout.image<=height-80 && (layout.v3 || layout.image>=height-140),'recognizable screenshot preview fits in the first screen');
 assert.equal(layout.image-layout.bottom,layout.v3?40:32,'space separates the screenshot from the CTAs');
 assert.equal(await page.locator('.cw-meta').count(),0);
 const top=await page.locator('.cw-hero h1').evaluate(el=>el.getBoundingClientRect().top+scrollY);
 await page.setViewportSize({width,height:height+80});
 await page.evaluate(()=>scrollTo(0,120));
 assert.equal(await page.locator('.cw-hero h1').evaluate(el=>el.getBoundingClientRect().top+scrollY),top);
}
// Compare each shell's menu with the homepage; Python checks it against the registry.
await page.goto(base+'/');
const expectedNavigation=await page.locator('#nav .nav-panel a').evaluateAll(items=>items.map(item=>({
 href:item.getAttribute('href').split('?')[0],group:item.closest('.nav-group').dataset.group,
 label:item.querySelector('.nav-label').textContent
})));
const shellRoutes=['/',...expectedNavigation.map(item=>item.href).filter(href=>href.startsWith('/')&&!href.includes('#')),'/reserved-tokens-terms'];
// Every shell page uses the same two menus across the collapse boundary (900px, kit Nav).
for (const path of shellRoutes) {
 for (const width of [390,768,900,901,1024,1440]) {
  await page.setViewportSize({width,height:900});await page.goto(base+path);await page.evaluate(()=>document.fonts.ready);
  assert.equal(await page.locator('#menuBtn').isVisible(),width<=900);
  if(width<=900){
   await page.locator('#menuBtn').click();
   // Phone menu: one list of three groups, every link showing; Sign in moves into the bar.
   assert.deepEqual(await page.locator('#nav .nav-heading').allTextContents(),['Products','Market','Resources']);
   for(const item of await page.locator('#nav a').all())assert(await item.isVisible());
   assert(await page.locator('.hdr-signin').isVisible());
   await page.locator('#menuBtn').click();
  }else{
   for(const group of ['buy','exchange']){
    await page.locator(`[data-group=${group}] .nav-toggle`).click();
    for(const item of await page.locator(`#nav-${group} a`).all())assert(await item.isVisible());
    await page.locator(`[data-group=${group}] .nav-toggle`).click();
   }
  }
  assert.deepEqual(await page.locator('#nav .nav-panel a').evaluateAll(items=>items.map(item=>({
   href:item.getAttribute('href').split('?')[0],group:item.closest('.nav-group').dataset.group,
   label:item.querySelector('.nav-label').textContent
  }))),expectedNavigation);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 }
}
// Shared interaction behavior is exercised once per desktop width, not on every route.
await page.goto(base+'/');
for(const width of [901,1024,1440]){
 await page.setViewportSize({width,height:900});
 for(const group of ['buy','exchange']){
  const button=page.locator(`[data-group=${group}] .nav-toggle`);
  await button.focus();await page.keyboard.press('Enter');
  assert.equal(await button.getAttribute('aria-expanded'),'true');
  assert(await page.locator(`#nav-${group}`).isVisible());
  assert.equal(await page.locator('.nav-group.open').count(),1);
  await page.keyboard.press('Escape');
  assert(!(await page.locator(`#nav-${group}`).isVisible()));
  assert.equal(await button.getAttribute('aria-expanded'),'false');
  // Menus open on click only (kit NavMenu), and a click outside closes them.
  await page.locator(`[data-group=${group}]`).hover();
  assert(!(await page.locator(`#nav-${group}`).isVisible()));
  await button.click();
  assert(await page.locator(`#nav-${group}`).isVisible());
  // The full-width panel covers the top of the page, so click below it.
  await page.mouse.click(4,860);
  assert(!(await page.locator(`#nav-${group}`).isVisible()));
 }
}
await page.goto(base+'/trusted-cowork');
assert.equal(await page.locator('#nav a[href="/trusted-cowork"]').getAttribute('aria-current'),'page');
assert.match(await page.locator('[data-group=buy] .nav-toggle').getAttribute('class'),/on/);
await page.setViewportSize({width:390,height:844});
assert(await page.locator('.cw-screenshot img').evaluate(el=>el.complete&&el.naturalWidth===1600));
await page.keyboard.press('Tab');
await page.locator('.cw-hero a').first().focus();
assert.equal(await page.locator('.cw-hero a').first().evaluate(el=>getComputedStyle(el).outlineStyle),'solid');
await page.locator('.cw-hero a').first().evaluate(el=>el.addEventListener('click',e=>e.preventDefault()));
await page.locator('.cw-hero a').first().click();
await page.waitForTimeout(100);
assert(events.some(e=>e.event==='nyte.cta_clicked'&&e.cta==='download_apple_silicon'&&e.page==='/trusted-cowork'&&e.position==='hero'));
for (const [selector,cta] of [['.btn-primary','download_apple_silicon'],['.btn-secondary','download_intel_mac']]) {
 const button=page.locator('.cw-download '+selector);
 await button.evaluate(el=>el.addEventListener('click',e=>e.preventDefault()));
 await button.click();
 await page.waitForTimeout(100);
 assert(events.some(e=>e.event==='nyte.cta_clicked'&&e.cta===cta&&e.page==='/trusted-cowork'&&e.position==='close'));
}
// Date behavior uses independent sample links and a fixed clock, not the page roster.
await page.clock.install({time:new Date('2026-10-27T12:00:00')});
let sampleDates=['2026-10-14','2026-10-15','2026-10-13','2026-10-28',null];
await page.route(base+'/',async route=>{
 const response=await route.fetch();
 const items=sampleDates.map((date,i)=>`<a href="/sample-${i}" ${date?`data-added="${date}"`:''}>Sample ${i}<span class="tag cc" hidden>NEW</span></a>`).join('');
 const body=(await response.text()).replace(/(<nav class="nav"[^>]*>)[\s\S]*?<\/nav>/,`$1${items}</nav>`);
 await route.fulfill({response,body});
});
const visibleChips=()=>page.locator('#nav a:has(.tag:not([hidden]))').evaluateAll(items=>items.map(item=>item.getAttribute('href')));
await page.goto(base+'/');
assert.deepEqual(await visibleChips(),['/sample-0','/sample-1']);
await page.clock.fastForward(24*60*60*1000);
assert.deepEqual(await visibleChips(),['/sample-1','/sample-3']);
await page.clock.fastForward(24*60*60*1000);
assert.deepEqual(await visibleChips(),['/sample-3']);
sampleDates=['2026-10-28','2026-10-26','2026-10-29','2026-10-27'];
await page.reload();
assert.deepEqual(await visibleChips(),['/sample-0','/sample-2','/sample-3']);
// Same-day additions have a stable cap, independent of the real page roster.
sampleDates=Array(4).fill('2026-10-29');
await page.reload();
assert.deepEqual(await visibleChips(),['/sample-0','/sample-1','/sample-2']);
// A tab returning after sleep must expire chips without waiting for a timer.
await page.clock.setSystemTime(new Date('2026-11-12T12:00:00'));
await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
assert.deepEqual(await visibleChips(),[]);
// Builds without Git omit dates, so every chip stays hidden.
sampleDates=Array(4).fill(null);
await page.reload();
assert.deepEqual(await visibleChips(),[]);
await page.clock.resume();await page.unroute(base+'/');
// Both direct and same-page deck links open the existing dialog.
await page.goto(base+'/#deck');await page.locator('#m-deck').waitFor({state:'visible'});
await page.keyboard.press('Escape');await page.goto(base+'/');
await page.setViewportSize({width:1440,height:900});
const exchange=page.locator('[data-group=exchange] .nav-toggle');
await exchange.click();await page.locator('#nav a[href="/#deck"]').click();
await page.locator('#m-deck').waitFor({state:'visible'});await page.keyboard.press('Escape');await page.locator('#m-deck').waitFor({state:'hidden'});
// The menu link opens the form in place: no navigation, no hash, and it reopens on a second click.
assert.equal(new URL(page.url()).hash,'');
await exchange.click();await page.locator('#nav a[href="/#deck"]').click();
await page.locator('#m-deck').waitFor({state:'visible'});
await page.keyboard.press('Escape');await page.locator('#m-deck').waitFor({state:'hidden'});
// Every page carries the buyer deck form: the menu opens it over that page, and #deck opens it on load.
for(const route of ['/suppliers','/confidential','/buy']){
 await page.goto(base+route);
 await exchange.click();await page.locator('#nav a[href="/#deck"]').click();
 await page.locator('#m-deck').waitFor({state:'visible'});
 assert.equal(new URL(page.url()).pathname,route,`menu Buyer deck stays on ${route}`);
 assert.equal(await page.locator('#m-deck #brochure-form').count(),1);
 await page.keyboard.press('Escape');await page.locator('#m-deck').waitFor({state:'hidden'});
 await page.goto(base+route+'#deck');await page.locator('#m-deck').waitFor({state:'visible'});
 await page.keyboard.press('Escape');await page.locator('#m-deck').waitFor({state:'hidden'});
}
await page.goto(base+'/');
// Escape from a panel item returns focus without reopening the panel.
await page.locator('[data-group=buy] .nav-toggle').click();await page.locator('#nav a[href="/confidential"]').focus();
await page.keyboard.press('Escape');assert(!(await page.locator('#nav-buy').isVisible()));
const buyToggle=page.locator('[data-group=buy] .nav-toggle');
await buyToggle.click();assert(await page.locator('#nav-buy').isVisible());
await buyToggle.click();assert(!(await page.locator('#nav-buy').isVisible()));
if(process.env.NYTE_NAV_SHOTS){
 const fs=require('node:fs');fs.mkdirSync(process.env.NYTE_NAV_SHOTS,{recursive:true});
 for(const width of [390,768,1024,1440]){
  await page.setViewportSize({width,height:1000});await page.goto(base+'/');await page.evaluate(()=>document.fonts.ready);
  if(width<=900)await page.locator('#menuBtn').click();else await page.locator('[data-group=buy] .nav-toggle').click();
  await page.screenshot({path:`${process.env.NYTE_NAV_SHOTS}/nav-${width}.png`});
 }
}
// Cities on this design: their own home page, and New York's shared pages under the city's name.
if(process.argv[2]){
 const fs=require('node:fs'),path=require('node:path');const root=path.resolve(process.argv[2]);
 const markets=JSON.parse(fs.readFileSync(path.join(__dirname,'markets.json')));
 const cities=JSON.parse(fs.readFileSync(path.join(__dirname,'nyte-cities.json')));
 for(const slug of Object.keys(cities)){
  const market={...markets.find(m=>m.slug===slug),...cities[slug]};
  const city=await browser.newContext();
  await city.route(`https://${market.domain}/**`,r=>{const p=new URL(r.request().url()).pathname;const file=path.join(root,p==='/'?`${slug}/index.html`:p);
   if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile())return r.fulfill({status:404,body:''});
   return r.fulfill({path:file,contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'text/javascript':path.extname(file)?undefined:'text/html'});});
  await city.route(url=>!url.hostname.endsWith(market.domain),r=>r.abort());
  const cityPage=await city.newPage();cityPage.on('pageerror',e=>failures.push(`${slug}: ${e.message}`));
  for(const width of [390,1280]){
   await cityPage.setViewportSize({width,height:900});
   await cityPage.goto(`https://${market.domain}/`);
   assert.equal(await cityPage.locator('.hdr .brand-place').innerText(),market.logo_place||market.region);
   assert.equal(await cityPage.locator('.brand-by').count(),0,'the city home never names NYTE');
   assert.equal(await cityPage.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${slug} home fits at ${width}px`);
   // Keyboard focus reveals even the partly clipped final market link.
   await cityPage.evaluate(()=>document.fonts.ready);
   await cityPage.locator('.market-links a').first().focus();
   const links=await cityPage.locator('.market-links a').count();
   for (let index=1;index<links;index++) await cityPage.keyboard.press('Tab');
   const focused=await cityPage.locator('.market-links').evaluate(nav=>{
    const link=document.activeElement, box=link.getBoundingClientRect(), rail=nav.getBoundingClientRect();
    return {last:link===nav.lastElementChild,visible:box.left>=rail.left-1 && box.right<=rail.right+1};
   });
   assert(focused.last && focused.visible,`${slug} footer market focus must be visible at ${width}px`);
   await cityPage.goto(`https://${market.domain}/suppliers`);
   assert.equal(await cityPage.locator('.hdr .brand-place').innerText(),market.logo_place||market.region);
   assert.equal(await cityPage.locator('.brand-by').count(),0,'pages outside checkout keep the city name alone');
   await cityPage.goto(`https://${market.domain}/confidential`);
   assert.equal(await cityPage.evaluate(()=>document.body.dataset.market),slug,`${slug} analytics market`);
   const signup=new URL(await cityPage.locator('a[href^="https://trustedrouter.com/"][href*="exchange_market="]').first().getAttribute('href'));
   assert.equal(signup.searchParams.get('exchange_market'),slug);assert.equal(signup.searchParams.get('utm_source'),market.domain);
   const place=market.faq_place??market.region;
   assert.equal(await cityPage.locator('#place-faq').count(),place?1:0,`${slug} data-location FAQ`);
   if(place){assert.equal(await cityPage.locator('#place-faq summary').textContent(),`Does ${place} mean our data stays in ${place}?`);
    assert.equal(await cityPage.locator('#place-faq [data-us-only]').count(),market.us?1:0);}
   await cityPage.goto(`https://${market.domain}/buy`);
   assert.equal(await cityPage.locator('.hdr .brand-place').innerText(),market.logo_place||market.region);
   assert.equal(await cityPage.locator('.hdr .brand-by').innerText(),'by NYTE');
   assert.equal(await cityPage.locator('.buy-intro a, a[href*="manage-reservation"]').first().getAttribute('href'),'https://nytokenexchange.com/manage-reservation');
   assert.equal(await cityPage.evaluate(()=>document.documentElement.dataset.billedBy),'New York Token Exchange (NYTE)','city checkout names who bills the card');
   assert.equal(await cityPage.locator('[data-exchange-name]').textContent(),`${market.name} · by NYTE`);
   assert.equal(await cityPage.locator('.market-links').count(),0,'shared pages list no other markets');
   assert.equal(await cityPage.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${slug} /buy fits at ${width}px`);
  }
  await city.close();
 }
}
const noJs=await browser.newContext({javaScriptEnabled:false,viewport:{width:1100,height:900}});
const noJsPage=await noJs.newPage();
await noJsPage.goto(base+'/trusted-cowork');
assert.equal(await noJsPage.locator('#menuBtn').isVisible(),false);
for(const group of ['buy','exchange']){
 await noJsPage.locator(`[data-group=${group}] .nav-toggle`).focus();
 assert(await noJsPage.locator(`#nav-${group}`).isVisible());
}
await noJsPage.setViewportSize({width:390,height:844});
for(const link of await noJsPage.locator('#nav a').all())assert(await link.isVisible(),'no-JS phone navigation stays available');
await noJs.close();
assert.deepEqual(failures,[]);console.log('PASS: mobile navigation, quick start, valid live evidence, stale fallback, PDF success/error, analytics, no PII');await browser.close();if(server)await new Promise(resolve=>server.close(resolve));
})().catch(error=>{console.error(error);process.exit(1);});
