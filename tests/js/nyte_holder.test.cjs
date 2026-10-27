const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../../sites/token-exchange/nyte-v2/manage.js'), 'utf8');
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.handlers={}; this.hidden=false; this.disabled=false; this.value=''; this.textContent=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children=[]; }
  addEventListener(event, handler) { this.handlers[event]=handler; }
  querySelector(tag) { return this.children.find(child=>child.tag===tag); }
  fire(type) { this.handlers[type]({target:this, preventDefault(){}}); }
}
function setup(api) {
  const ids={};
  for (const id of ['holder-status','holder-positions','holder-redeem','holder-more','holder-email-form','holder-email','holder-backfill','holder-old-email']) ids[id]=new Element();
  ids['holder-email-form'].append(new Element('button'));
  ids['holder-backfill'].append(new Element('button'));
  const requests=[], analytics=[], history=[];
  let n=0;
  const context=vm.createContext({document:{getElementById:id=>ids[id],createElement:tag=>new Element(tag)},
    location:{hash:'#signed-one-use-secret',pathname:'/manage-reservation'},history:{replaceState:(...args)=>history.push(args)},
    Date,Intl,crypto:{randomUUID:()=>`request-${++n}`},window:{TokenExchangeAnalytics:{track:(...args)=>analytics.push(args)}},
    fetch:async(url,options)=>{
      const body=JSON.parse(options.body);requests.push({url,options,body});
      const response=await api(body);
      return {ok:!response.error,json:async()=>response};
    },
  });
  vm.runInContext(source,context);
  return {ids,requests,analytics,history};
}
const tick=()=>new Promise(setImmediate);
function position() {
  return {session_id:'cs_private',id:'nyte_pos_private',grade:'A',lots:3,per_lot_cents:10000,
    sold_lots:0,listed_lots:0,status:'held',purchase_completed_at:1,listing_rows:[]};
}
test('private fragment is removed, redemption is explicit, and mutations preserve their retry ID',async()=>{
  let attempts=0;
  const s=setup(body=>{
    if(body.action==='redeem') return {session:'short-lived-session'};
    if(body.action==='positions') return {resale_enabled:true,positions:[position()],cursor:null};
    if(body.action==='list') return ++attempts===1 ? {error:'Network retry required'} : {status:'active'};
    throw Error('Unexpected action');
  });
  assert.equal(s.requests.length,0);
  assert.deepEqual(s.history[0],[null,'','/manage-reservation']);
  s.ids['holder-redeem'].fire('click'); await tick();
  assert.equal(s.requests[0].body.token,'signed-one-use-secret');
  assert.equal(s.requests[0].options.referrerPolicy,'no-referrer');
  assert.equal(s.requests[0].options.credentials,'omit');
  assert.equal(s.requests[1].body.session,'short-lived-session');
  const card=s.ids['holder-positions'].children[0];
  const button=card.children.find(child=>child.tag==='button');
  assert.equal(button.textContent,'List lots for resale');
  button.fire('click'); await tick();
  assert.equal(s.ids['holder-status'].textContent,'Network retry required');
  button.fire('click'); await tick();
  const mutations=s.requests.filter(request=>request.body.action==='list');
  assert.equal(mutations[0].body.request_id,mutations[1].body.request_id);
  assert.equal(mutations[1].body.lots,1);
  assert.equal(s.analytics.length,1);
  assert.equal(s.analytics[0][0],'nyte.resale_listed');
  assert.equal(JSON.stringify(s.analytics[0][1]),JSON.stringify({grade:'A'}));
});
test('requesting a link shows the generic message and erases the contact input',async()=>{
  const s=setup(()=>({message:'if we have reservations for that address, we sent a link'}));
  s.ids['holder-email'].value='private@example.com';
  s.ids['holder-email-form'].fire('submit');await tick();
  assert.equal(s.ids['holder-email'].value,'');
  assert.equal(s.ids['holder-status'].textContent,'if we have reservations for that address, we sent a link');
  assert.equal(s.analytics.length,0);
});
test('refund window and withdrawal UI use the server position, and escape display text',async()=>{
  const p=position();p.purchase_completed_at=Math.floor(Date.now()/1000);p.grade='<script>private</script>';
  p.listed_lots=1;p.listing_rows=[{id:'listing',status:'active',lots_listed:2,lots_sold:1}];
  const s=setup(body=>body.action==='redeem'?{session:'session'}:{resale_enabled:true,positions:[p],cursor:null});
  s.ids['holder-redeem'].fire('click');await tick();
  const card=s.ids['holder-positions'].children[0];
  assert.deepEqual(card.children.filter(child=>child.tag==='button').map(child=>child.textContent),['Request full refund','Withdraw 1 unsold lots']);
  assert.equal(card.children[0].textContent,'Grade <script>private</script> · 3 lots');
});
