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
  const initialFacility = query.get('facility');
  const facilityPreselected = ['solar', 'underwater'].includes(initialFacility);
  if (facilityPreselected) {
    choose('facility', initialFacility, ['solar', 'underwater']);
    choose('underlying', 'facility', ['index', 'model', 'facility']);
  }
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
    const facility = selected('underlying').value === 'facility';
    if (facility) choose('unit', 'blocks', ['blocks']);
    const unit = selected('unit').value;
    const index = selected('underlying').value === 'index';
    const model = selected('underlying').value === 'model';
    for (const value of ['throughput', 'unsure']) {
      flow.querySelector(`input[name="unit"][value="${value}"]`).disabled = facility;
    }
    get('facility-unit-note').hidden = !facility;
    get('facility-fields').hidden = !facility;
    get('facility-fields').disabled = !facility;
    get('quantity-label').textContent = facility ? 'Number of 1B-token lots' : '1 billion tokens per block';
    get('blocks').max = facility ? selected('facility').dataset.lots : '1000000';
    if (get('blocks').valueAsNumber > Number(get('blocks').max)) get('blocks').value = get('blocks').max;
    get('lot-cap').hidden = !facility;
    get('lot-cap').textContent = facility
      ? `Maximum ${Number(get('blocks').max).toLocaleString('en-US')} lots in the first offering.` : '';
    for (const field of ['term', 'start']) {
      get(`${field}-field`).hidden = facility;
      get(field).disabled = facility;
    }
    get('start').required = !facility;
    get('rate').disabled = unit !== 'throughput';
    const showPrices = index && unit === 'blocks';
    get('grades').querySelectorAll('.buy-price').forEach(price => { price.hidden = !showPrices; });
    get('price-caption').hidden = !showPrices;
    get('grades').hidden = !index;
    get('model-fields').hidden = !model;
    get('grades').disabled = !index;
    get('model-fields').disabled = !model;
    get('model').required = model;
    get('block-fields').hidden = unit !== 'blocks';
    get('rate-field').hidden = unit !== 'throughput';
    const quantity = get('blocks').valueAsNumber;
    flow.querySelector('[data-block-delta="-1"]').disabled = !Number.isFinite(quantity) || quantity <= 1;
    flow.querySelector('[data-block-delta="1"]').disabled = !Number.isFinite(quantity) || quantity >= Number(get('blocks').max);
    for (const delta of [-1, 1]) {
      flow.querySelector(`[data-block-delta="${delta}"]`).setAttribute('aria-label',
        `${delta < 0 ? 'Remove' : 'Add'} one ${facility ? 'lot' : 'block'}`);
    }
    get('total').hidden = !showPrices || !get('blocks').validity.valid;
    if (!get('total').hidden) {
      get('total').textContent = `Indicative total: ${money.format(quantity * Number(selected('grade').dataset.price))} USD · ${quantity.toLocaleString('en-US')} × 1B-token blocks over the selected term. As of ${get('as-of').textContent}. Indicative, not an executable quote.`;
    } else {
      get('total').textContent = 'Pricing is discussed on the call. No indicative total is shown for throughput, a specific model, or an undecided request.';
    }
    if (model) void catalogue();
  }

  function fieldsValid(number, report = false) {
    const unit = selected('unit').value;
    const fields = number === 2
      ? [...(selected('underlying').value === 'facility' ? [] : [get('start')]),
        ...(selected('underlying').value === 'model' ? [get('model')] : []),
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
    const facility = selected('underlying').value === 'facility';
    const underlying = selected('underlying').value === 'index'
      ? `NYTE Index ${selected('grade').value}`
      : facility ? `Planned ${selected('facility').dataset.name}; targeted launch: ${selected('facility').dataset.launch}; Grade B · Professional (DeepSeek-class models)`
      : `Model ${clean(get('model').value)}; provider ${clean(get('provider').value) || 'any'}`;
    const quantity = facility ? `${get('blocks').value} x 1B-token lots`
      : unit === 'blocks' ? `${get('blocks').value} x 1B tokens`
      : unit === 'throughput' ? `${get('rate').value} tokens per second` : 'to discuss';
    const term = `${get('term').value} ${get('term').value === '1' ? 'month' : 'months'}`;
    const timing = facility ? 'delivery: over the facility’s first twelve months of operation, not per month'
      : `term: ${term}; start: ${get('start').value}`;
    const summary = `Unit: ${facility ? '1B-token lots' : unit}; quantity: ${quantity}; underlying: ${underlying}; ${timing}; company: ${clean(get('company').value) || 'not specified'}`;
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
    update();
    // A copied step URL cannot skip required request fields or an invalid contact prefill.
    if (number >= 3 && !fieldsValid(2)) number = 2;
    else if (number === 4 && !fieldsValid(3)) number = 3;
    step = number;
    steps.forEach(section => { section.hidden = Number(section.dataset.step) !== step; });
    flow.querySelectorAll('.buy-steps li').forEach((item, index) => {
      if (index + 1 === step) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
    // Reconstruct from allowlisted values; no free text or contact details in history.
    const url = new URL(location.pathname, location.origin);
    url.search = new URLSearchParams({step:String(step), unit:selected('unit').value, grade:selected('grade').value}).toString();
    if (selected('underlying').value === 'facility') url.searchParams.set('facility', selected('facility').value);
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
      get('blocks').value = String(Math.max(1, Math.min(Number(get('blocks').max), (get('blocks').valueAsNumber || 1) + Number(button.dataset.blockDelta))));
      get('blocks').setCustomValidity('');
      update();
    }
  });
  window.addEventListener('popstate', () => {
    const value = new URLSearchParams(location.search).get('step');
    show(['1', '2', '3', '4'].includes(value) ? Number(value) : 1);
  });
  const initial = query.get('step');
  show(facilityPreselected ? 2 : ['1', '2', '3', '4'].includes(initial) ? Number(initial) : 1);
});
