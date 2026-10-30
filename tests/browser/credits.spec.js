const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');

// The fee schedule the design was signed off with (config defaults).
const schedule = {
  card: { basis_points: 290, fixed_cents: 30, minimum_cents: 80, max_cents: null, multiplier_basis_points: 15000 },
  ach: { basis_points: 80, fixed_cents: 0, minimum_cents: 0, max_cents: 500, multiplier_basis_points: 15000 },
  stablecoin: { basis_points: 150, fixed_cents: 0, minimum_cents: 0, max_cents: null, multiplier_basis_points: 15000 },
  adyen: { basis_points: 0, fixed_cents: 0, minimum_cents: 80, max_cents: null, multiplier_basis_points: 10000 },
};

// Render the real template through Jinja with one state's context; Stripe,
// the store and the session are not involved.
function credits(state = {}) {
  const context = {
    workspaces: [{ id: '0', name: 'Personal workspace' }], current_workspace_id: '0',
    console_next_path: '/console/credits', user_email: 'test@example.test', active: 'credits',
    page_title: 'Credits', provider_mode: false, credits_available: '$0.15', credits_empty: false,
    checkout_presets: [5, 25, 200], checkout_selected_preset: 25,
    checkout_fee_schedule_json: JSON.stringify({ rails: { auto: 'card', card: 'card', paypal: 'card', ach: 'ach', stablecoin: 'stablecoin', adyen: 'adyen' }, rates: schedule }),
    paypal_minimum_dollars: 20,
    checkout_summary: { credit: '$25.00', fee: '$1.59', total: '$26.59' },
    stripe_paypal_minimum_fee_display: '$1.20', stripe_ach_maximum_fee_display: '$7.50',
    paypal_enabled: true, adyen_enabled: false, wallet_only_billing: false,
    has_payment_method: false, payment_method_pending: false, has_stripe_customer: false,
    saved_payment_method: null, auto_refill_enabled: false, auto_refill_threshold_dollars: 10,
    auto_refill_amount_dollars: 25, identity_verification_checkout: false, paypal_pending: false,
    ...state,
  };
  return execFileSync(process.env.TEST_PYTHON || path.join(root, '.venv', 'bin', 'python'), ['-c', `
import json, sys
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
env = Environment(loader=ChoiceLoader([DictLoader({'console/_social_rewards.html': '', '_favicon.html': ''}), FileSystemLoader('src/trusted_router/templates')]), autoescape=True)
env.filters['datetime_iso'] = str
print(env.get_template('console/credits.html').render(**json.loads(sys.argv[1])))
`, JSON.stringify(context)], { cwd: root, encoding: 'utf8' });
}

async function load(page, state) {
  const posts = [];
  await page.route('http://console.test/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/static/')) return route.fulfill({ path: path.join(root, 'src/trusted_router', url.pathname) });
    if (url.pathname === '/console/credits/stripe-details') return route.fulfill({ contentType: 'text/html', body: '<span data-credits-saved-payment-method-fragment>Visa ····4242</span><div data-credits-payment-history-fragment><p class="credits-quiet">No payments yet.</p></div>' });
    if (route.request().method() === 'POST') { posts.push({ path: url.pathname, body: route.request().postData() }); return route.fulfill({ contentType: 'text/html', body: '<p>posted</p>' }); }
    return route.fulfill({ contentType: 'text/html', body: credits(state) });
  });
  await page.goto('http://console.test/console/credits');
  return posts;
}

const summary = (page) => Promise.all(['credit', 'fee', 'total'].map((cell) => page.locator(`[data-summary-${cell}]`).innerText()));
const preset = (page, label) => page.getByRole('radio', { name: label, exact: true });

for (const width of [320, 390, 1280]) test(`presets, method and fee summary at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await load(page);
  await expect(preset(page, '$25')).toBeChecked();
  expect(await summary(page)).toEqual(['$25.00', '$1.59', '$26.59']);
  await expect(page.locator('.credits-other')).toBeHidden();

  await preset(page, '$200').check();
  expect(await summary(page)).toEqual(['$200.00', '$9.44', '$209.44']);
  await page.getByLabel('Payment method').selectOption('ach');
  expect(await summary(page)).toEqual(['$200.00', '$2.43', '$202.43']);
  await expect(page.locator('.credits-select')).toHaveAttribute('data-method', 'bank');
  await page.getByLabel('Payment method').selectOption('stablecoin');
  await preset(page, '$5').check();
  expect(await summary(page)).toEqual(['$5.00', '$0.12', '$5.12']);
  await expect(page.locator('.credits-select')).toHaveAttribute('data-method', 'coin');

  await preset(page, 'Other').check();
  const other = page.getByLabel('Amount in US dollars');
  await expect(other).toBeVisible();
  await expect(other).toBeFocused();
  expect(await summary(page)).toEqual(['—', '—', '—']);
  await expect(page.getByRole('button', { name: 'Continue to checkout' })).toBeDisabled();
  await other.fill('50');
  await page.getByLabel('Payment method').selectOption('auto');
  expect(await summary(page)).toEqual(['$50.00', '$2.72', '$52.72']);
  await expect(page.getByRole('button', { name: 'Continue to checkout' })).toBeEnabled();

  // Segments share one row at every width and nothing scrolls sideways.
  const boxes = await Promise.all(['$5', '$25', '$200', 'Other'].map((label) => preset(page, label).boundingBox()));
  expect(new Set(boxes.map((box) => Math.round(box.y))).size).toBe(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test('PayPal disables $5, bumps to $25 and names the minimum', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const posts = await load(page);
  await preset(page, '$5').check();
  await page.getByLabel('Payment method').selectOption('paypal');
  await expect(preset(page, '$5')).toBeDisabled();
  await expect(preset(page, '$25')).toBeChecked();
  await expect(page.getByRole('status').filter({ hasText: 'PayPal starts at $20, so $25 is selected.' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue to PayPal' })).toBeEnabled();
  expect(await summary(page)).toEqual(['$25.00', '$1.59', '$26.59']);
  await expect(page.locator('.credits-select')).toHaveAttribute('data-method', 'wallet');

  await preset(page, 'Other').check();
  await page.getByLabel('Amount in US dollars').fill('10');
  await expect(page.getByRole('status').filter({ hasText: /^PayPal starts at \$20\.$/ })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue to PayPal' })).toBeDisabled();
  await page.getByLabel('Amount in US dollars').fill('30');
  await expect(page.getByRole('button', { name: 'Continue to PayPal' })).toBeEnabled();
  await expect(page.locator('[data-paypal-minimum-message]')).toBeHidden();

  await page.getByLabel('Payment method').selectOption('auto');
  await expect(preset(page, '$5')).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Continue to checkout' })).toBeVisible();
  await page.getByLabel('Payment method').selectOption('paypal');

  await page.getByRole('button', { name: 'Continue to PayPal' }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].path).toBe('/console/credits/checkout');
  expect(posts[0].body).toContain('amount=other');
  expect(posts[0].body).toContain('other_amount=30');
  expect(posts[0].body).toContain('payment_method=paypal');
});

test('verification entry preselects the remaining amount and keeps the purpose', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  // $12 still owed: the middle preset becomes $12 and is selected.
  const posts = await load(page, {
    checkout_presets: [5, 12, 200], checkout_selected_preset: 12, identity_verification_checkout: true,
    verification_topup_line: true, verification_topup_remaining_display: '$12.00',
    checkout_summary: { credit: '$12.00', fee: '$1.20', total: '$13.20' },
  });
  await expect(page.getByRole('status').filter({ hasText: 'Verification needs $12.00 more in top-ups.' })).toBeVisible();
  await expect(preset(page, '$12')).toBeChecked();
  expect(await summary(page)).toEqual(['$12.00', '$1.20', '$13.20']);
  await page.getByLabel('Payment method').selectOption('paypal');
  await expect(preset(page, '$12')).toBeDisabled();
  await expect(preset(page, '$200')).toBeChecked();
  await expect(page.getByRole('status').filter({ hasText: 'PayPal starts at $20, so $200 is selected.' })).toBeVisible();
  await page.getByRole('button', { name: 'Continue to PayPal' }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].body).toContain('purpose=identity_verification');
  expect(posts[0].body).toContain('amount=200');
  expect(posts[0].body).not.toContain('other_amount');
});

for (const width of [390, 1280]) test(`returning customer: saved card, Change, switch and rows at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const posts = await load(page, {
    has_payment_method: true, has_stripe_customer: true,
    saved_payment_method: { id_tail: 'ab1234', brand: null, last4: null, details_available: false },
  });
  const chip = page.locator('[data-checkout-saved]');
  await expect(chip).toBeVisible();
  await expect(chip).toContainText('Visa ····4242');
  await expect(page.locator('.credits-row-methods')).toContainText('Visa ····4242');
  await expect(page.getByLabel('Payment method')).toBeHidden();
  await page.getByRole('button', { name: 'Change', exact: true }).click();
  await expect(chip).toBeHidden();
  const select = page.getByLabel('Payment method');
  await expect(select).toBeVisible();
  await expect(select).toBeFocused();
  await expect(select.locator('option').first()).toHaveText('Visa ····4242');
  await expect(select.locator('option').nth(1)).toHaveText('Another card');

  await expect(page.getByRole('button', { name: 'Manage in Stripe' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Remove', exact: true })).toBeVisible();
  const toggle = page.getByRole('switch', { name: 'Auto refill' });
  await expect(toggle).toBeEnabled();
  await expect(toggle).not.toBeChecked();
  await expect(page.locator('[data-auto-refill-settings]')).toBeHidden();
  await toggle.focus();
  await page.keyboard.press('Space');
  await expect(toggle).toBeChecked();
  await expect(page.locator('[data-auto-refill-settings]')).toBeVisible();
  await expect(page.getByLabel('When balance is below')).toHaveValue('10');
  await expect(page.getByLabel('Refill with')).toHaveValue('25');
  await expect(page.getByRole('button', { name: /Social rewards/ })).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();

  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].path).toBe('/console/credits/auto-refill');
  expect(posts[0].body).toContain('enabled=1');
  expect(posts[0].body).toContain('threshold=10');
  expect(posts[0].body).toContain('amount=25');
});

test('switching auto refill off saves without the enabled flag', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const posts = await load(page, {
    has_payment_method: true, has_stripe_customer: true, auto_refill_enabled: true,
    saved_payment_method: { id_tail: 'ab1234', brand: 'visa', last4: '4242', details_available: true },
  });
  const toggle = page.getByRole('switch', { name: 'Auto refill' });
  await expect(toggle).toBeChecked();
  await expect(page.locator('[data-auto-refill-settings]')).toBeVisible();

  // An out-of-range field keeps the switch on and posts nothing.
  await page.getByLabel('Refill with').fill('3');
  await toggle.click();
  await expect(toggle).toBeChecked();
  await page.waitForTimeout(200);
  expect(posts).toHaveLength(0);

  await page.getByLabel('Refill with').fill('25');
  await toggle.click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].path).toBe('/console/credits/auto-refill');
  expect(posts[0].body).not.toContain('enabled=');
  expect(posts[0].body).toContain('threshold=10');
});

test('keyboard order reaches every control and the fee note opens', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await load(page);
  const names = [];
  await page.getByRole('link', { name: 'Send credits' }).focus();
  for (let i = 0; i < 5; i += 1) {
    await page.keyboard.press('Tab');
    names.push(await page.evaluate(() => {
      const el = document.activeElement;
      return `${el.tagName.toLowerCase()}:${el.getAttribute('aria-label') || el.textContent.trim().slice(0, 24) || el.value}`;
    }));
  }
  // The radio group is one tab stop (the checked $25), then the select, the
  // button, Fees, then the first row's control. Without a saved card the
  // switch is disabled and the rewards row stays hidden, so neither stops.
  expect(names).toEqual([
    'input:25', 'select:Payment method', 'button:Continue to checkout', 'summary:Fees', 'button:Add a card',
  ]);
  await expect(page.getByRole('switch', { name: 'Auto refill' })).toBeDisabled();
  await page.getByText('Fees', { exact: true }).click();
  const note = page.locator('.credits-fees p');
  await expect(note).toBeVisible();
  await expect(note).toContainText('$1.20 minimum fee');
  const card = await page.locator('.credits-add').boundingBox();
  const box = await note.boundingBox();
  expect(box.x + box.width).toBeLessThanOrEqual(card.x + card.width + 1);
});

// Keep the original fixture without maximum_cents: older rendered pages must
// still initialize correctly. Exercise the current route's explicit map too.
for (const suppliedMaxima of [false, true]) test(`cent amounts and rail ceilings with server maxima ${suppliedMaxima}`, async ({ page }) => {
  const rails = { auto: 'card', card: 'card', paypal: 'card', ach: 'ach', stablecoin: 'stablecoin', adyen: 'adyen' };
  const maxima = { auto: 957076, card: 957076, paypal: 1000000, ach: 999250, stablecoin: 977666, adyen: 1000000 };
  await load(page, suppliedMaxima ? {
    checkout_fee_schedule_json: JSON.stringify({ rails, rates: schedule, maximum_cents: maxima }),
    checkout_max_credit_dollars: '9570.76',
    adyen_enabled: true,
  } : { adyen_enabled: true });
  expect(await summary(page)).toEqual(['$25.00', '$1.59', '$26.59']);
  await preset(page, 'Other').check();
  const amount = page.getByLabel('Amount in US dollars');
  const method = page.getByLabel('Payment method');
  const submit = page.locator('[data-checkout-submit]');
  await amount.fill('25.50');
  expect(await summary(page)).toEqual(['$25.50', '$1.62', '$27.12']);
  await expect(submit).toBeEnabled();
  for (const [rail, maximum] of [['auto', '9570.76'], ['ach', '9992.50'], ['stablecoin', '9776.66'], ['paypal', '10000.00'], ['adyen', '10000.00']]) {
    await method.selectOption(rail);
    await expect(amount).toHaveAttribute('max', maximum);
    await amount.fill(maximum);
    await expect(submit).toBeEnabled();
    await expect(page.locator('[data-checkout-maximum-message]')).toContainText(`$${maximum}`);
    if (!['paypal', 'adyen'].includes(rail)) expect((await summary(page))[2]).toBe('$10000.00');
    await amount.fill((Number(maximum) + 0.01).toFixed(2));
    await expect(submit).toBeDisabled();
    expect(await summary(page)).toEqual(['—', '—', '—']);
  }
  await method.selectOption('paypal');
  await expect(amount).toHaveAttribute('min', '20');
  await amount.fill('19.99');
  await expect(submit).toBeDisabled();
  await amount.fill('20');
  await expect(submit).toBeEnabled();
  await amount.fill('9999');
  await expect(submit).toBeEnabled();
  await method.selectOption('auto');
  await expect(submit).toBeDisabled();
  await expect(amount).toHaveAttribute('min', '1');
});

test('missing maxima use the configured fee rates', async ({ page }) => {
  const rates = Object.fromEntries(Object.entries(schedule).map(([rail, values]) => [rail, { ...values, multiplier_basis_points: 30000 }]));
  await load(page, { checkout_fee_schedule_json: JSON.stringify({ rails: { auto: 'card', ach: 'ach', stablecoin: 'stablecoin' }, rates }) });
  await preset(page, 'Other').check();
  const amount = page.getByLabel('Amount in US dollars');
  for (const [rail, maximum] of [['auto', '9176.83'], ['ach', '9985.00'], ['stablecoin', '9563.08']]) {
    await page.getByLabel('Payment method').selectOption(rail);
    await expect(amount).toHaveAttribute('max', maximum);
    await amount.fill(maximum);
    await expect(page.locator('[data-checkout-submit]')).toBeEnabled();
    expect(Number((await summary(page))[2].slice(1))).toBeLessThanOrEqual(10000);
    await amount.fill((Number(maximum) + 0.01).toFixed(2));
    await expect(page.locator('[data-checkout-submit]')).toBeDisabled();
  }
});

test('explicit server maxima including zero remain authoritative', async ({ page }) => {
  await load(page, { checkout_fee_schedule_json: JSON.stringify({ rails: { auto: 'card', ach: 'ach' }, rates: schedule, maximum_cents: { auto: 2000, ach: 0 } }) });
  await expect(preset(page, '$25')).toBeDisabled();
  await expect(page.locator('[data-checkout-submit]')).toBeDisabled();
  await preset(page, '$5').check();
  await expect(page.locator('[data-checkout-submit]')).toBeEnabled();
  await page.getByLabel('Payment method').selectOption('ach');
  await expect(preset(page, '$5')).toBeDisabled();
  await expect(page.locator('[data-checkout-submit]')).toBeDisabled();
  await expect(page.getByLabel('Amount in US dollars')).toHaveAttribute('max', '0.00');
});

test('wallet-only checkout derives the stablecoin ceiling', async ({ page }) => {
  await load(page, { wallet_only_billing: true });
  expect(await summary(page)).toEqual(['$25.00', '$0.59', '$25.59']);
  await expect(page.getByLabel('Payment method')).toHaveCount(0);
  await preset(page, 'Other').check();
  const amount = page.getByLabel('Amount in US dollars');
  await expect(amount).toHaveAttribute('max', '9776.66');
  await amount.fill('9776.66');
  await expect(page.locator('[data-checkout-submit]')).toBeEnabled();
  expect((await summary(page))[2]).toBe('$10000.00');
});
