const {test} = require('node:test');
const assert = require('node:assert/strict');
const {setup} = require('./exchange_analytics_harness.cjs');
test('visible page sends one view and one engaged event, with credentials omitted',()=>{
 const s=setup();
 assert.equal(s.requests.length,1);
 assert.equal(JSON.parse(s.requests[0].body).event,'exchange.landing_viewed');
 assert.equal(s.requests[0].credentials,'omit');
 assert.equal(s.requests[0].keepalive,true);
 assert.ok(!s.requests[0].body.includes('email'));
 const id=JSON.parse(s.requests[0].body).visitor_id;
 assert.equal(s.window.TokenExchangeAnalytics.context().visitor_id,id);
 [...s.timers.values()][0]();
 assert.equal(JSON.parse(s.requests[1].body).event,'exchange.landing_engaged');
 s.document.visibilitychange(); [...s.timers.values()][0]();
 assert.equal(s.requests.length,2);
 assert.equal(setup({storage:s.storage,local:s.local}).window.TokenExchangeAnalytics.context().visitor_id,id);
});
test('hidden tabs wait; hiding cancels the engagement timer',()=>{
 const s=setup({hidden:true}); assert.equal(s.requests.length,0);
 s.document.hidden=false;s.document.visibilitychange();assert.equal(s.requests.length,1);
 s.document.hidden=true;s.document.visibilitychange();assert.equal(s.timers.size,0);
});
for(const navigator of [{globalPrivacyControl:true},{doNotTrack:'1'}]){
 test('privacy signal suppresses requests, storage and brochure attribution '+JSON.stringify(navigator),()=>{
  const s=setup({navigator});assert.equal(s.requests.length,0);assert.equal(s.storage.size,0);
  assert.equal(s.window.TokenExchangeAnalytics.context().tracking_disabled,true);
  assert.deepEqual(Object.keys(s.window.TokenExchangeAnalytics.context().campaign),[]);
 });
}
test('blocked storage and failed transport leave the page usable',async()=>{
 const s=setup({noStorage:true,offline:true});await new Promise(setImmediate);
 assert.ok(s.window.TokenExchangeAnalytics.context().visitor_id);
 assert.equal(s.requests.length,1);
});

test('facility metadata reaches the collector without facility or contact details',()=>{
 const s=setup();
 s.context.location.pathname='/buy';
 for(const event of ['nyte.buy_step_2_viewed','nyte.buy_completed']) {
  s.window.TokenExchangeAnalytics.track(event,{unit:'blocks',underlying_kind:'facility',grade:'none'});
  const payload=JSON.parse(s.requests.at(-1).body);
  assert.equal(payload.event,event);
  assert.equal(payload.page,'/buy');
  assert.equal(payload.underlying_kind,'facility');
  assert.equal(payload.grade,'none');
  assert.deepEqual(Object.keys(payload).sort(),['event','visitor_id','campaign','unit','underlying_kind','grade','page'].sort());
 }
});

for(const pathname of ['/neocloud-accelerator','/neocloud-accelerator.html','/trusted-cowork','/trusted-cowork.html','/buy','/buy.html']) {
 test('NYTE visits retain their canonical page: '+pathname,()=>{
  const s=setup({pathname});
  [...s.timers.values()][0]();
  assert.deepEqual(s.requests.map(request=>{
   const {event,page}=JSON.parse(request.body);
   return {event,page};
  }),[
   {event:'exchange.landing_viewed',page:pathname.replace(/\.html$/, '')},
   {event:'exchange.landing_engaged',page:pathname.replace(/\.html$/, '')},
  ]);
 });
}

test('reservation events are bounded, omit PII, and success has no URL order details',()=>{
 const s=setup({pathname:'/buy'});
 const analytics=s.window.TokenExchangeAnalytics;
 analytics.track('nyte.reserve_checkout_started',{unit:'blocks',underlying_kind:'index',grade:'AAA',email:'private',lots:2});
 let payload=JSON.parse(s.requests.at(-1).body);
 assert.equal(payload.event,'nyte.reserve_checkout_started');
 assert.deepEqual(Object.keys(payload).sort(),['event','visitor_id','campaign','unit','underlying_kind','grade','page'].sort());
 analytics.track('nyte.reserve_checkout_succeeded',{email:'private',session_id:'cs_private',grade:'A'});
 payload=JSON.parse(s.requests.at(-1).body);
 assert.equal(payload.event,'nyte.reserve_checkout_succeeded');
 assert.deepEqual(Object.keys(payload).sort(),['event','visitor_id','campaign','page'].sort());
 const count=s.requests.length;
 analytics.track('nyte.reserve_checkout_started',{unit:'blocks',underlying_kind:'facility',grade:'B'});
 analytics.track('nyte.reserve_unknown');
 assert.equal(s.requests.length,count);
});

test('resale events contain only bounded grade and never holder capability or email',()=>{
 const s=setup({pathname:'/manage-reservation'});
 for(const event of ['nyte.resale_listed','nyte.resale_withdrawn']) {
  s.window.TokenExchangeAnalytics.track(event,{grade:'A',email:'private@example.com',token:'secret',position:'cs_private',lots:7});
  const payload=JSON.parse(s.requests.at(-1).body);
  assert.equal(payload.event,event);
  assert.equal(payload.page,'/manage-reservation');
  assert.deepEqual(Object.keys(payload).sort(),['event','visitor_id','campaign','grade','page'].sort());
 }
 const count=s.requests.length;
 s.window.TokenExchangeAnalytics.track('nyte.resale_listed',{grade:'none'});
 s.window.TokenExchangeAnalytics.track('nyte.resale_unknown',{grade:'A'});
 assert.equal(s.requests.length,count);
 for(const navigator of [{globalPrivacyControl:true},{doNotTrack:'1'}]) {
  const disabled=setup({navigator});
  disabled.window.TokenExchangeAnalytics.track('nyte.resale_listed',{grade:'A'});
  assert.equal(disabled.requests.length,0);
 }
});

test('the referring site is recorded as a hostname and the visit keeps its landing source',()=>{
 const s=setup({search:'',referrer:'https://news.ycombinator.com/item?id=1'});
 const first=JSON.parse(s.requests[0].body);
 assert.equal(first.referrer_host,'news.ycombinator.com');
 assert.deepEqual(first.campaign,{});
 assert.ok(!s.requests[0].body.includes('item?id'));
 const next=setup({search:'',referrer:'https://nytokenexchange.com/',storage:s.storage,local:s.local,pathname:'/buy'});
 const second=JSON.parse(next.requests[0].body);
 assert.equal(second.referrer_host,'news.ycombinator.com');
 assert.equal(second.page,'/buy');
 assert.equal(second.visitor_id,first.visitor_id);
});
test('a tagged arrival replaces the visit source; the site itself is never a referrer',()=>{
 const s=setup({search:'?utm_source=linkedin&utm_medium=social',referrer:'https://nytokenexchange.com/'});
 const payload=JSON.parse(s.requests[0].body);
 assert.deepEqual(payload.campaign,{utm_source:'linkedin',utm_medium:'social'});
 assert.equal(payload.referrer_host,undefined);
 const direct=JSON.parse(setup({search:'',referrer:''}).requests[0].body);
 assert.equal(direct.referrer_host,undefined);
 assert.deepEqual(direct.campaign,{});
});
test('new pages keep their own slug; anything else counts as the home page',()=>{
 assert.equal(JSON.parse(setup({pathname:'/a-page-joseph-adds.html'}).requests[0].body).page,'/a-page-joseph-adds');
 assert.equal(JSON.parse(setup({pathname:'/two/segments'}).requests[0].body).page,'/');
 assert.equal(JSON.parse(setup({pathname:'/Has%20Spaces'}).requests[0].body).page,'/');
});
test('the visitor id lasts 90 days, is adopted from a sister-site link, and rejects junk',()=>{
 const s=setup();
 const id=JSON.parse(s.requests[0].body).visitor_id;
 assert.equal(JSON.parse(s.local.get('exchange-visitor')).id,id);
 const later=setup({local:new Map([['exchange-visitor',JSON.stringify({id,at:Date.now()-80*86400000})]])});
 assert.equal(JSON.parse(later.requests[0].body).visitor_id,id);
 const old='22222222-2222-4222-8222-222222222222';
 const expired=setup({local:new Map([['exchange-visitor',JSON.stringify({id:old,at:Date.now()-91*86400000})]])});
 assert.notEqual(JSON.parse(expired.requests[0].body).visitor_id,old);
 const carried=setup({search:'?tx_vid=33333333-3333-4333-8333-333333333333&exchange_market=new-york',hostname:'thetokenexchange.com'});
 assert.equal(JSON.parse(carried.requests[0].body).visitor_id,'33333333-3333-4333-8333-333333333333');
 assert.equal(JSON.parse(setup({search:'?tx_vid=not-a-uuid'}).requests[0].body).visitor_id,'11111111-1111-4111-8111-111111111111');
});
test('?tx_internal=1 switches this browser off on this domain until ?tx_internal=0',()=>{
 const s=setup({search:'?tx_internal=1'});
 assert.equal(s.requests.length,0);
 assert.equal(s.window.TokenExchangeAnalytics.context().tracking_disabled,true);
 assert.equal(setup({local:s.local}).requests.length,0);
 assert.equal(setup({local:s.local,search:'?tx_internal=0'}).requests.length,1);
});
const click=(s,el)=>s.document.click({target:{closest:()=>el}});
const link=(href,text,extra={})=>({tagName:'A',href,textContent:text,dataset:{},closest:()=>null,...extra});
test('links to trustedrouter.com carry the visitor and market and log the click',()=>{
 const s=setup();
 const id=JSON.parse(s.requests[0].body).visitor_id;
 const el=link('https://trustedrouter.com/console/api-keys?utm_source=nytokenexchange.com','Get your API key');
 click(s,el);
 const url=new URL(el.href);
 assert.equal(url.searchParams.get('tx_vid'),id);
 assert.equal(url.searchParams.get('exchange_market'),'new-york');
 assert.equal(url.searchParams.get('utm_source'),'nytokenexchange.com');
 const events=s.requests.slice(1).map(r=>JSON.parse(r.body));
 assert.deepEqual(events.map(e=>[e.event,e.cta]),[['nyte.cta_clicked','api_key'],['nyte.outbound_clicked','api_key']]);
 assert.equal(events[0].position,'section');
 assert.equal(events[1].destination_host,'trustedrouter.com');
});
test('calendar and email clicks are leads; other hosts and the site itself are not logged',()=>{
 const s=setup();
 const calendar=link('https://calendly.com/joseph-perla/15min','Book a call',{dataset:{cta:'book_pilot_call'}});
 click(s,calendar);
 let payload=JSON.parse(s.requests.at(-1).body);
 assert.deepEqual([payload.event,payload.destination_host,payload.cta],['nyte.outbound_clicked','calendly.com','book_pilot_call']);
 assert.ok(!calendar.href.includes('tx_vid'));
 const mail=link('mailto:enterprise@trustedrouter.com','Email us');
 click(s,mail);
 payload=JSON.parse(s.requests.at(-1).body);
 assert.deepEqual([payload.event,payload.cta],['nyte.cta_clicked','contact_email']);
 assert.ok(!s.requests.at(-1).body.includes('enterprise@'));
 const count=s.requests.length;
 click(s,link('https://example.com/','Elsewhere'));
 click(s,link('https://nytokenexchange.com/buy','Reserve'));
 click(s,{tagName:'BUTTON',textContent:'Menu',dataset:{},closest:()=>null});
 assert.equal(s.requests.length,count);
 const disabled=setup({navigator:{doNotTrack:'1'}});
 const product=link('https://trustedrouter.com/','Get your API key');
 click(disabled,product);
 assert.ok(!product.href.includes('tx_vid'));
 assert.equal(disabled.requests.length,0);
});
