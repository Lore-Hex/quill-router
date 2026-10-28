(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const status = byId('holder-status'), positions = byId('holder-positions');
  let token = location.hash.slice(1), session = '', cursor = null, busy = false;
  history.replaceState(null, '', location.pathname);
  byId('holder-redeem').hidden = !token;
  const money = cents => new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'}).format(cents / 100);
  async function api(action, values = {}) {
    const response = await fetch('https://trustedrouter.com/token-exchange/reservations', {
      method:'POST', mode:'cors', credentials:'omit', referrerPolicy:'no-referrer',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, session, ...values}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Please try again.');
    return data;
  }
  async function run(button, work) {
    if (busy) return;
    busy = true; button.disabled = true; status.textContent = '';
    try { await work(); } catch (error) { status.textContent = error.message || 'Please try again.'; }
    finally { busy = false; button.disabled = false; }
  }
  function element(tag, text) {
    const el = document.createElement(tag); el.textContent = text; return el;
  }
  function actionButton(parent, text, action, p, values = {}) {
    const button = element('button', text);
    button.type = 'button'; button.className = 'btn btn-secondary';
    // Keep this ID on network failure. A retry cannot list the same lots twice.
    let requestId = crypto.randomUUID();
    button.addEventListener('click', () => run(button, async () => {
      const data = await api(action, {position:p.session_id, request_id:requestId, ...values()});
      requestId = crypto.randomUUID();
      if ((action === 'list' && data.status === 'active') || (action === 'withdraw' && data.status === 'withdrawn')) {
        window.TokenExchangeAnalytics?.track(action === 'list' ? 'nyte.resale_listed' : 'nyte.resale_withdrawn', {grade:p.grade});
      }
      await load(false);
      status.textContent = action === 'request_refund' ? 'Your full refund request is recorded. Our team will refund the original card through Stripe.' : `Reservation updated: ${data.status}.`;
    }));
    parent.append(button);
  }
  async function load(append) {
    const data = await api('positions', {cursor:append ? cursor : null});
    if (!append) positions.replaceChildren();
    cursor = data.cursor; byId('holder-more').hidden = !cursor;
    if (!data.positions.length && !append) positions.append(element('p', 'No reservations found. Use “Find older reservations” below if your purchase predates holder management.'));
    for (const p of data.positions) {
      const card = element('section', ''); card.className = 'buy-reserve';
      card.append(element('h2', `Grade ${p.grade} · ${p.lots} lots`));
      card.append(element('p', `${money(p.per_lot_cents)} per lot · ${p.sold_lots} sold · ${p.listed_lots} listed · ${p.status.replaceAll('_', ' ')}`));
      const deadline = p.purchase_completed_at === null ? null : (p.purchase_completed_at + 30 * 86400) * 1000;
      if (deadline === null) card.append(element('p', 'Purchase completion time needs review. Contact enterprise@trustedrouter.com with your Stripe receipt.'));
      else {
        card.append(element('p', `Purchased ${new Date(p.purchase_completed_at * 1000).toLocaleString()}. Full refund deadline: ${new Date(deadline).toLocaleString()}.`));
        if ((p.refund_request_allowed ?? p.status === 'held') && Date.now() <= deadline) actionButton(card, 'Request full refund', 'request_refund', p, () => ({}));
        const available = p.lots - p.sold_lots - p.listed_lots;
        if (p.status === 'held' && Date.now() > deadline && available > 0 && data.resale_enabled) {
          const label = element('label', 'Lots to list '), input = document.createElement('input');
          input.type = 'number'; input.min = '1'; input.max = String(available); input.value = '1'; input.step = '1';
          label.append(input); card.append(label);
          actionButton(card, 'List lots for resale', 'list', p, () => ({lots:Number(input.value)}));
        }
      }
      for (const listing of p.listing_rows) {
        if (listing.status === 'active') actionButton(card, `Withdraw ${listing.lots_listed - listing.lots_sold} unsold lots`, 'withdraw', p, () => ({listing:listing.id}));
      }
      positions.append(card);
    }
  }
  byId('holder-email-form').addEventListener('submit', event => {
    event.preventDefault();
    run(event.target.querySelector('button'), async () => {
      const data = await api('email', {email:byId('holder-email').value});
      status.textContent = data.message; byId('holder-email').value = '';
    });
  });
  byId('holder-redeem').addEventListener('click', event => run(event.target, async () => {
    const data = await api('redeem', {token}); session = data.session; token = '';
    byId('holder-redeem').hidden = true; byId('holder-email-form').hidden = true;
    byId('holder-backfill').hidden = false; await load(false);
  }));
  byId('holder-more').addEventListener('click', event => run(event.target, () => load(true)));
  byId('holder-backfill').addEventListener('submit', event => {
    event.preventDefault();
    run(event.target.querySelector('button'), async () => {
      await api('backfill', {email:byId('holder-old-email').value});
      byId('holder-old-email').value = ''; await load(false);
    });
  });
})();
