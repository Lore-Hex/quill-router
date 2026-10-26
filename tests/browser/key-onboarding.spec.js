const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const key = 'sk-tr-demo-not-a-real-key';
function welcome() {
  return execFileSync(process.env.TEST_PYTHON || path.join(root, '.venv/bin/python'), ['-c', `
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/trusted_router/templates'))
print(env.get_template('console/welcome.html').render(revealed_key='${key}', api_base_url='https://api.example.test/v1', credits_available='$0.15', can_run_first_call=True, onboarding_cell='run_request', user={'id': 'test-user'}))
`], { cwd: root, encoding: 'utf8' });
}
async function load(page, reply = {status: 200, json: {choices: [{message: {content: '<b>Safe text</b>'}}]}}) {
  const inference = [], analytics = [];
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { value: {writeText: async value => {window.copied = value;}} }));
  await page.route('https://api.example.test/**', async route => {
    inference.push(route.request());
    await route.fulfill(reply);
  });
  await page.route('https://onboarding.test/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/static/')) return route.fulfill({path: path.join(root, 'src/trusted_router', url.pathname)});
    if (url.pathname === '/console/social-rewards') return route.fulfill({json: {enabled: false, eligible: false, actions: []}});
    if (url.pathname === '/chat-proxy/v1/chat/completions') { inference.push(route.request()); return route.fulfill(reply); }
    if (url.pathname === '/analytics/events') { analytics.push(route.request().postDataJSON()); return route.fulfill({status: 204}); }
    return route.fulfill({contentType: 'text/html', body: welcome()});
  });
  await page.goto('https://onboarding.test/console/welcome');
  return {inference, analytics};
}
for (const width of [320, 390, 1280]) test(`key copy, setup and deliberate first call at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 900});
  const {inference, analytics} = await load(page);
  expect(inference).toHaveLength(0);
  await page.getByRole('button', {name: 'Copy API key', exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.copied)).toBe(key);
  await page.getByRole('button', {name: 'Copy message'}).click();
  await expect.poll(() => page.evaluate(() => window.copied)).toContain(key);
  await page.getByRole('tab', {name: 'Python', exact: true}).click();
  await page.locator('#setup-python').getByRole('button', {name: 'Copy code', exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.copied)).toContain(`api_key="${key}"`);
  await page.getByRole('tab', {name: 'Python', exact: true}).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#setup-js')).toBeVisible();
  expect(inference).toHaveLength(0);
  await page.getByRole('button', {name: 'Run my first API request'}).click();
  await expect(page.locator('[data-call-result]')).toContainText('<b>Safe text</b>');
  await expect(page.locator('[data-result-output] b')).toHaveCount(0);
  expect(inference).toHaveLength(1);
  expect(inference[0].headers().authorization).toBe(`Bearer ${key}`);
  expect(inference[0].postDataJSON().max_tokens).toBe(512);
  await expect.poll(() => analytics.length).toBe(2);
  expect(analytics[1].event).toBe('onboarding_call_succeeded');
  expect(JSON.stringify(analytics)).not.toContain(key);
  expect(JSON.stringify(analytics)).not.toContain('capital of France');
  expect(JSON.stringify(analytics)).not.toContain('Safe text');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});
test('payment failure offers recovery without reporting success', async ({page}) => {
  const {analytics} = await load(page, {status: 402, json: {error: {message: 'private provider detail'}}});
  await page.getByRole('button', {name: 'Run my first API request'}).click();
  await expect(page.locator('[data-call-error]')).toContainText('Credits are required');
  await expect(page.locator('[data-call-result]')).toBeHidden();
  await expect(page.getByRole('button', {name: 'Try the live request again'})).toBeEnabled();
  await expect.poll(() => analytics.length).toBe(2);
  expect(analytics[1].event).toBe('onboarding_call_failed');
  expect(JSON.stringify(analytics)).not.toContain('private provider detail');
});

test('mobile header contains large balances and keeps Menu usable', async ({page}) => {
  await page.setViewportSize({width: 320, height: 900});
  await load(page);
  for (const balance of ['$1,000.00', '$1,000,000.00']) {
    await page.locator('.console-balance span').evaluate((node, value) => { node.textContent = value; }, balance);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
    await page.getByRole('button', {name: 'Menu', exact: true}).click();
    await expect(page.locator('#console-navigation')).toBeVisible();
    await page.getByRole('button', {name: 'Close', exact: true}).click();
  }
});

test('clipboard failure shows a manual-copy recovery', async ({page}) => {
  await load(page);
  await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); }; });
  await page.getByRole('button', {name: 'Copy API key', exact: true}).click();
  await expect(page.locator('#welcome-api-key-copy-status')).toBeVisible();
  await expect(page.locator('#welcome-api-key-copy-status')).toContainText('copy it manually');
});
