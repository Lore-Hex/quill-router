/* In-memory request state. Only the Calendly booking URL receives contact details. */
document.addEventListener('DOMContentLoaded', () => {
  'use strict';
  const flow = document.getElementById('buy-flow');
  if (!flow) return;
  const get = id => document.getElementById(`buy-${id}`);
  const selected = name => flow.querySelector(`input[name="${name}"]:checked`);
  const steps = [...flow.querySelectorAll('[data-step]')];
  const query = new URLSearchParams(location.search);
  const choose = (name, value, allowed) => {
    if (allowed.includes(value)) flow.querySelector(`input[name="${name}"][value="${value}"]`).checked = true;
  };
  choose('grade', query.get('grade'), ['AAA', 'A', 'B', 'C']);
  choose('unit', query.get('unit'), ['blocks', 'throughput', 'unsure']);
  let step = 1;
  let completed = false;
  let catalogueLoaded = false;
  const clean = value => value.trim().replace(/\s+/g, ' ');
  const metadata = () => ({unit: selected('unit').value, underlying_kind: selected('underlying').value,
    grade: selected('underlying').value === 'index' ? selected('grade').value : 'none'});
  const track = event => window.TokenExchangeAnalytics?.track(event, metadata());
  const money = new Intl.NumberFormat('en-US', {style:'currency', currency:'USD', maximumFractionDigits:0});

  async function catalogue() {
    if (catalogueLoaded) return;
    catalogueLoaded = true;
    const load = async (endpoint, target, provider) => {
      try {
        const payload = await window.NYTECatalog.read(endpoint);
        if (!Array.isArray(payload?.data) || !payload.data.length || payload.data.some(row =>
          !row || typeof row.id !== 'string' || (provider && !['active', 'blocked'].includes(row.routing_status)))) {
          throw new Error('Invalid catalogue');
        }
        const options = payload.data.filter(row => !provider || row.routing_status === 'active').map(row => {
          const option = document.createElement('option');
          option.value = row.id;
          option.label = typeof row.name === 'string' ? row.name : row.id;
          return option;
        });
        get(target).replaceChildren(...options);
      } catch { /* Plain text fields remain usable even if either catalogue fails. */ }
    };
    await Promise.all([load('models', 'models', false), load('providers', 'providers', true)]);
  }

  function initializeStart() {
    // Construct in local time on day 1 to avoid month-end overflow and UTC shifts.
    const today = new Date();
    const nextMonth = new Date(today.getFullYear(), today.getMonth() + 1, 1);
    get('start').value = `${nextMonth.getFullYear()}-${String(nextMonth.getMonth() + 1).padStart(2, '0')}`;
    get('start').required = true;
  }

  function update() {
    const unit = selected('unit').value;
    const index = selected('underlying').value === 'index';
    const showPrices = index && unit === 'blocks';
    get('grades').querySelectorAll('.buy-price').forEach(price => { price.hidden = !showPrices; });
    get('price-caption').hidden = !showPrices;
    get('grades').hidden = !index;
    get('model-fields').hidden = index;
    get('grades').disabled = !index;
    get('model-fields').disabled = index;
    get('model').required = !index;
    get('block-fields').hidden = unit !== 'blocks';
    get('rate-field').hidden = unit !== 'throughput';
    const quantity = get('blocks').valueAsNumber;
    flow.querySelector('[data-block-delta="-1"]').disabled = !Number.isFinite(quantity) || quantity <= 1;
    get('total').hidden = !showPrices || !get('blocks').validity.valid;
    if (!get('total').hidden) {
      get('total').textContent = `Indicative total: ${money.format(quantity * Number(selected('grade').dataset.price))} USD · ${quantity.toLocaleString('en-US')} × 1B-token blocks over the selected term. As of ${get('as-of').textContent}. Indicative, not an executable quote.`;
    } else {
      get('total').textContent = 'Pricing is discussed on the call. No indicative total is shown for throughput, a specific model, or an undecided request.';
    }
    if (!index) void catalogue();
  }

  function fieldsValid(number, report = false) {
    const unit = selected('unit').value;
    const fields = number === 2
      ? [get('start'), ...(selected('underlying').value === 'model' ? [get('model')] : []),
        ...(unit === 'blocks' ? [get('blocks')] : unit === 'throughput' ? [get('rate')] : [])]
      : [get('name'), get('email')];
    for (const field of fields) {
      field.setCustomValidity('');
      if (field.required && !clean(field.value)) field.setCustomValidity('Please complete this field.');
      if (field.id === 'buy-email' && clean(field.value) && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(field.value.trim())) {
        field.setCustomValidity('Please enter a valid work email address.');
      }
      if (!field.checkValidity()) {
        if (report) field.reportValidity();
        return false;
      }
    }
    return true;
  }

  function scheduling() {
    const unit = selected('unit').value;
    const underlying = selected('underlying').value === 'index'
      ? `NYTE Index ${selected('grade').value}`
      : `Model ${clean(get('model').value)}; provider ${clean(get('provider').value) || 'any'}`;
    const quantity = unit === 'blocks' ? `${get('blocks').value} x 1B tokens`
      : unit === 'throughput' ? `${get('rate').value} tokens per second` : 'to discuss';
    const term = `${get('term').value} ${get('term').value === '1' ? 'month' : 'months'}`;
    const summary = `Unit: ${unit}; quantity: ${quantity}; underlying: ${underlying}; term: ${term}; start: ${get('start').value}; company: ${clean(get('company').value) || 'not specified'}`;
    const url = new URL('https://calendly.com/joseph-perla/15min');
    url.search = new URLSearchParams({embed_domain:'nytokenexchange.com', embed_type:'Inline', hide_gdpr_banner:'1',
      name:clean(get('name').value), email:get('email').value.trim(), a1:summary}).toString();
    get('calendly-link').href = url.href;
    get('summary').textContent = summary;
    get('summary').hidden = false;
    const iframe = document.createElement('iframe');
    iframe.title = 'Book a 15-minute reserved tokens call with Joseph on Calendly';
    iframe.referrerPolicy = 'no-referrer';
    iframe.src = url.href;
    get('calendar').replaceChildren(iframe);
  }

  function show(number, push = false) {
    // A copied step URL cannot skip required request fields or an invalid contact prefill.
    if (number >= 3 && !fieldsValid(2)) number = 2;
    else if (number === 4 && !fieldsValid(3)) number = 3;
    step = number;
    update();
    steps.forEach(section => { section.hidden = Number(section.dataset.step) !== step; });
    flow.querySelectorAll('.buy-steps li').forEach((item, index) => {
      if (index + 1 === step) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
    // Reconstruct from allowlisted values; no free text or contact details in history.
    const url = new URL(location.pathname, location.origin);
    url.search = new URLSearchParams({step:String(step), unit:selected('unit').value, grade:selected('grade').value}).toString();
    history[push ? 'pushState' : 'replaceState'](null, '', url);
    if (step === 4) scheduling();
    else {
      get('calendar').replaceChildren();
      get('calendly-link').href = 'https://calendly.com/joseph-perla/15min?embed_domain=nytokenexchange.com&embed_type=Inline&hide_gdpr_banner=1';
    }
    get(`title-${step}`).focus({preventScroll:true});
    if (push) get(`title-${step}`).scrollIntoView({block:'start'});
    track(`nyte.buy_step_${step}_viewed`);
    if (step === 4 && !completed) {
      // Completion means handoff to scheduling, never a claim of a confirmed booking.
      completed = true;
      track('nyte.buy_completed');
    }
  }

  initializeStart();
  flow.addEventListener('change', update);
  flow.querySelectorAll('[data-js]').forEach(element => { element.hidden = false; });
  flow.addEventListener('input', event => {
    event.target.setCustomValidity?.('');
    update();
  });
  flow.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.hasAttribute('data-next') && (step === 1 || fieldsValid(step, true))) show(step + 1, true);
    if (button.hasAttribute('data-back')) show(step - 1, true);
    if (button.hasAttribute('data-block-delta')) {
      get('blocks').value = String(Math.max(1, Math.min(1000000, (get('blocks').valueAsNumber || 1) + Number(button.dataset.blockDelta))));
      get('blocks').setCustomValidity('');
      update();
    }
  });
  window.addEventListener('popstate', () => {
    const value = new URLSearchParams(location.search).get('step');
    show(['1', '2', '3', '4'].includes(value) ? Number(value) : 1);
  });
  const initial = query.get('step');
  show(['1', '2', '3', '4'].includes(initial) ? Number(initial) : 1);
});
