/* Run the shipped UI with the same rendered-control fixture as the facility tests. */
const assert = require('node:assert/strict');
const {setup} = require('./test-buy-facilities.cjs');
const flush = async s => {
  for (const [id, fn] of [...s.timers]) { s.timers.delete(id); fn(); }
  await new Promise(setImmediate);
};
(async () => {
  const requests=[];
  const s=setup('?step=2&unit=blocks&grade=C', {fetch: async (url, options) => {
    requests.push({url,options});
    if (url.includes('/quote?')) {
      const grade=s.radio('grade', new URL(url).searchParams.get('grade'));
      const lots=Number(new URL(url).searchParams.get('lots'));
      const listCents=Number(grade.dataset.price)*100;
      const perLot=Math.floor((listCents*9950+5000)/10000);
      return {ok:true,json:async()=>({grade:grade.value,lots,
        index_as_of:s.get('as-of').dateTime,
        list_price_cents:listCents, per_lot_cents:perLot,
        total_cents:perLot*lots, discount_bps:50, max_lots:100})};
    }
    return {ok:true,json:async()=>({url:'https://checkout.stripe.com/c/pay/reserved'})};
  }});
  assert.equal(s.get('reserve').hidden,false);
  assert.equal(s.get('reserve-button').disabled,true);
  await flush(s);
  const expectedCents=Math.floor((Number(s.radio('grade','C').dataset.price)*100*9950+5000)/10000);
  assert(s.get('reserve-price').textContent.includes(new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(expectedCents/100)));
  assert.equal(s.get('reserve-button').disabled,true,'terms required even with quote');
  await s.get('reserve-button').click();
  assert(!requests.some(r=>r.url.endsWith('/checkout')));
  s.get('reserve-terms').checked=true;s.handlers.change();
  assert.equal(s.get('reserve-button').disabled,false);
  await s.get('reserve-button').click();
  const post=requests.find(r=>r.url.endsWith('/checkout'));
  assert.deepEqual(JSON.parse(post.options.body),{grade:'C',lots:1,terms_accepted:true});
  assert.equal(post.options.credentials,'omit');
  assert.equal(s.location.redirect,'https://checkout.stripe.com/c/pay/reserved');
  assert.equal(s.events.filter(e=>e.event==='nyte.reserve_checkout_started').length,1);
  for (const [name,value] of [['unit','throughput'],['unit','unsure'],['underlying','model'],['underlying','facility']]) {
    s.change(name,value);
    assert.equal(s.get('reserve').hidden,true);
    assert.equal(s.get('reserve-button').disabled,true);
  }
  const returned=setup('?reserved=success&email=private&grade=AAA&lots=999&session_id=private');
  assert.equal(returned.get('reserved-success').hidden,false);
  assert.deepEqual(returned.events.find(e=>e.event==='nyte.reserve_checkout_succeeded'),{event:'nyte.reserve_checkout_succeeded'});
  const failure=setup('?step=2', {fetch:async()=>({ok:false})});
  await flush(failure);
  assert.equal(failure.get('reserve-button').disabled,true);
  assert(failure.get('reserve-price').textContent.includes('Book a call'));
  const stale=setup('?step=2', {fetch:async()=>({ok:true,json:async()=>({index_as_of:'2000-01-01'})})});
  await flush(stale);
  assert.equal(stale.get('reserve-button').disabled,true);
  assert(stale.get('reserve-price').textContent.includes('Reload'));
})().catch(error=>{console.error(error);process.exitCode=1;});
