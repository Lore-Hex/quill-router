/* Run the shipped UI with the same rendered-control fixture as the facility tests. */
const assert = require('node:assert/strict');
// CI supplies its rendered page; direct `node --test` builds the same fixture.
if (!process.argv[2]) {
  const fs = require('node:fs');
  const path = require('node:path');
  const root = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'nyte-checkout-'));
  process.on('exit', () => fs.rmSync(root, {recursive:true, force:true}));
  require('node:child_process').execFileSync('python3', [path.join(__dirname, 'build.py'), '--output', root]);
  process.argv[2] = path.join(root, 'buy');
}
const {setup} = require('./test-buy-facilities.cjs');
const quoteFor = (s, url) => {
  const grade=s.radio('grade', new URL(url).searchParams.get('grade'));
  const lots=Number(new URL(url).searchParams.get('lots'));
  const listCents=Number(grade.dataset.price)*100;
  const perLot=Math.floor((listCents*9000+5000)/10000);
  const subtotal=perLot*lots;
  const exchangeFee=Math.floor((subtotal*10+5000)/10000);
  const processingFee=Math.ceil((subtotal+exchangeFee+30)*10000/9710)-subtotal-exchangeFee;
  return {grade:grade.value,lots,index_as_of:s.get('as-of').dateTime,
    list_price_cents:listCents,per_lot_cents:perLot,discount_bps:1000,max_lots:100,
    subtotal_cents:subtotal,exchange_fee_bps:10,exchange_fee_cents:exchangeFee,
    processing_fee_bps:290,processing_fee_fixed_cents:30,processing_fee_cents:processingFee,
    total_cents:subtotal+exchangeFee+processingFee};
};
const flush = async s => {
  for (const [id, fn] of [...s.timers]) { s.timers.delete(id); fn(); }
  await new Promise(setImmediate);
};
(async () => {
  const requests=[];
  const s=setup('?step=2&unit=blocks&grade=C', {fetch: async (url, options) => {
    requests.push({url,options});
    if (url.includes('/quote?')) {
      return {ok:true,json:async()=>quoteFor(s,url)};
    }
    return {ok:true,json:async()=>({url:'https://checkout.stripe.com/c/pay/reserved'})};
  }});
  s.radio('grade','C').dataset.price='27';
  assert.equal(s.get('reserve').hidden,false);
  assert.equal(s.get('reserve-button').disabled,true);
  await flush(s);
  const expectedCents=Math.floor((Number(s.radio('grade','C').dataset.price)*100*9000+5000)/10000);
  assert(s.get('reserve-price').textContent.includes(new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(expectedCents/100)));
  assert.equal(s.get('reserve-price').textContent, '$24.30 per lot · 10% early-reservation discount · 1 lot: $24.30 + $0.02 NYTE exchange fee (0.1%) + $1.04 card processing fee = $25.36 USD total. Index as of ' + s.get('as-of').dateTime + '. Up to 100 lots by card; larger orders by call.');
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
  for (const field of ['per_lot_cents','subtotal_cents','exchange_fee_bps','exchange_fee_cents',
                       'processing_fee_bps','processing_fee_fixed_cents','processing_fee_cents','total_cents']) {
    for (const missing of [false,true]) {
      const bad=setup('?step=2&unit=blocks&grade=A', {fetch:async url=>({ok:true,json:async()=>{
        const quote=quoteFor(bad,url);
        if (missing) delete quote[field]; else quote[field] += 1;
        return quote;
      }})});
      bad.get('reserve-terms').checked=true;
      await flush(bad);
      assert.equal(bad.get('reserve-button').disabled,true, `${field} ${missing ? 'missing' : 'mismatch'}`);
      assert(bad.get('reserve-price').textContent.includes('Quote unavailable'),field);
    }
  }
  for (const [indexPrice, expectedTotal] of [['0.01',32], ['2000000',185561308]]) {
    const outOfRange=setup('?step=2&unit=blocks&grade=C', {fetch:async url=>({ok:true,json:async()=>{
      // All fields agree with the displayed index and published fee rates.
      const quote=quoteFor(outOfRange,url);
      assert.equal(quote.total_cents,expectedTotal);
      return quote;
    }})});
    outOfRange.radio('grade','C').dataset.price=indexPrice;
    outOfRange.get('reserve-terms').checked=true;
    await flush(outOfRange);
    assert.equal(outOfRange.get('reserve-button').disabled,true, `charge bound: ${expectedTotal}`);
    assert(outOfRange.get('reserve-price').textContent.includes('Quote unavailable'));
  }
  const multi=setup('?step=2&unit=blocks&grade=A', {fetch:async url=>({ok:true,json:async()=>quoteFor(multi,url)})});
  multi.radio('grade','A').dataset.price='863';
  multi.input('3');
  multi.get('reserve-terms').checked=true;
  await flush(multi);
  assert.equal(multi.get('reserve-button').disabled,false);
  assert(multi.get('reserve-price').textContent.includes('3 lots: $2,330.10 + $2.33 NYTE exchange fee (0.1%) + $69.97 card processing fee = $2,402.40 USD total.'));
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
