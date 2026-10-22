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
await page.locator('.privacy-price').first().waitFor();assert.equal(await page.locator('.health-bar').count(),24);assert.equal(await page.locator('.build-digest').count(),1);
assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
await page.locator('.hero-api a',{hasText:'API quick start'}).click();await page.waitForURL('**/confidential#start');
await page.getByRole('button',{name:'Get the buyer deck',exact:true}).click();assert(await page.locator('dialog').isVisible());
await page.getByRole('button',{name:'Download the deck',exact:true}).click();assert.equal(downloads,0);
await page.locator('input[name=email]').fill('preview@example.com');await page.getByRole('button',{name:'Download the deck',exact:true}).click();await page.waitForFunction(()=>document.getElementById('brochure-status').dataset.state==='error');assert.equal(downloads,1);assert(!(events.some(e=>e.event==='nyte.form_submitted')));
success=true;const download=page.waitForEvent('download');await page.getByRole('button',{name:'Download the deck',exact:true}).click();assert.equal((await download).suggestedFilename(),'NYTE-Buyer-Deck.pdf');await page.waitForFunction(()=>document.getElementById('brochure-status').dataset.state==='success');
await page.keyboard.press('Escape');assert(!(await page.locator('dialog').isVisible()));
await page.waitForTimeout(200);assert(events.some(e=>e.event==='nyte.form_submitted'&&e.form==='buyer_deck'));assert(events.some(e=>e.event==='nyte.cta_clicked'&&e.cta==='api_quick_start'));assert(events.every(e=>!('email' in e)));
evidence={...evidence,generated_at:'2000-01-01T00:00:00Z'};await page.reload();await page.waitForTimeout(200);assert.equal(await page.locator('.build-digest').count(),0);assert.equal(await page.locator('.health-bar').count(),0);assert(await page.getByRole('link',{name:'Published release'}).isVisible());
assert.deepEqual(failures,[]);console.log('PASS: mobile navigation, quick start, valid live evidence, stale fallback, PDF success/error, analytics, no PII');await browser.close();if(server)await new Promise(resolve=>server.close(resolve));
})().catch(error=>{console.error(error);process.exit(1);});
