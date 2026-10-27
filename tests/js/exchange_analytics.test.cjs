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
 assert.equal(setup({storage:s.storage}).window.TokenExchangeAnalytics.context().visitor_id,id);
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
