const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../../sites/token-exchange/analytics.js'),'utf8');
function setup(options={}) {
 const requests=[], timers=new Map(), storage=options.storage||new Map(); let sequence=0;
 const document={hidden:!!options.hidden,addEventListener:(event,fn)=>document[event]=fn};
 const window={};
 const context=vm.createContext({window,document,navigator:options.navigator||{},URLSearchParams,Date,
  location:{search:'?utm_source=trustedrouter.com&utm_content=homepage-banner&email=private',pathname:'/'},
  crypto:{randomUUID:()=> '11111111-1111-4111-8111-111111111111'},
  sessionStorage:{getItem:k=>{if(options.noStorage)throw Error('blocked');return storage.get(k);},setItem:(k,v)=>storage.set(k,v)},
  setTimeout:fn=>{timers.set(++sequence,fn);return sequence;},clearTimeout:id=>timers.delete(id),
  fetch:async(url,opts)=>{requests.push({url,...opts});if(options.offline)throw Error('offline');},
 });
 vm.runInContext(source,context);
 return {requests,window,document,storage,timers,context};
}
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
