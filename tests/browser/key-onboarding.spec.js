const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const key = 'sk-tr-review-fixture-not-a-real-key';
function welcome() {
  return execFileSync(process.env.TEST_PYTHON || 'python3', ['-c', `
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
layout = '''<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/tokens.css"><link rel="stylesheet" href="/static/dashboard.css">
<link rel="stylesheet" href="/static/console.css">{% block head_extra %}{% endblock %}
{% block scripts %}{% endblock %}<body class="console"><main class="console-main">{% block body %}{% endblock %}</main></body>'''
env = Environment(loader=ChoiceLoader([DictLoader({'console/_layout.html': layout}),FileSystemLoader('src/trusted_router/templates')]))
print(env.get_template('console/welcome.html').render(revealed_key='${key}', api_base_url='https://api.example.test/v1'))
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
  await page.getByRole('tab', {name: 'Connect app'}).click();
  await page.getByRole('button', {name: 'Copy agent prompt'}).click();
  await expect.poll(() => page.evaluate(() => window.copied)).toContain(key);
  await page.getByLabel('Set up with').selectOption('Python');
  await page.getByRole('button', {name: 'Copy code', exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.copied)).toContain(`api_key="${key}"`);
  await page.getByRole('tab', {name: 'Connect app'}).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#try-pane')).toBeVisible();
  expect(inference).toHaveLength(0);
  await page.getByRole('button', {name: 'Send request', exact: true}).click();
  await expect(page.locator('#result')).toContainText('<b>Safe text</b>');
  await expect(page.locator('#result b')).toHaveCount(0);
  expect(inference).toHaveLength(1);
  expect(inference[0].headers().authorization).toBe(`Bearer ${key}`);
  expect(inference[0].postDataJSON().max_tokens).toBe(256);
  await expect.poll(() => analytics.length).toBe(2);
  expect(analytics[1].event).toBe('onboarding_call_succeeded');
  expect(JSON.stringify(analytics)).not.toContain(key);
  expect(JSON.stringify(analytics)).not.toContain('weather app');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});
test('payment failure offers recovery without reporting success', async ({page}) => {
  const {analytics} = await load(page, {status: 402, json: {error: {message: 'private provider detail'}}});
  await page.getByRole('tab', {name: 'Try a request'}).click();
  await page.getByRole('button', {name: 'Send request'}).click();
  await expect(page.locator('#result')).toContainText('Not enough credit');
  await expect(page.locator('#connected')).toBeHidden();
  await expect(page.getByRole('button', {name: 'Send request'})).toBeEnabled();
  await expect.poll(() => analytics.length).toBe(2);
  expect(analytics[1].event).toBe('onboarding_call_failed');
  expect(JSON.stringify(analytics)).not.toContain('private provider detail');
});
