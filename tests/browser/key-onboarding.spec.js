const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const key = 'sk-tr-review-fixture-not-a-real-key';

function welcome() {
  return execFileSync(process.env.TEST_PYTHON || 'python3', ['-c', `
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
env = Environment(loader=ChoiceLoader([DictLoader({'console/_layout.html': '''<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/tokens.css"><link rel="stylesheet" href="/static/dashboard.css">
<link rel="stylesheet" href="/static/console.css"><script defer src="/static/console.js"></script>
<body class="console"><main class="console-main">{% block body %}{% endblock %}</main></body>'''}),
FileSystemLoader('src/trusted_router/templates')]))
print(env.get_template('console/welcome.html').render(revealed_key='${key}', api_base_url='https://api.example.test/v1'))
`], { cwd: root, encoding: 'utf8' });
}

for (const width of [390, 1280]) test(`current key onboarding copies secrets and switches setup tabs at ${width}px`, async ({ page }) => {
  const calls = [];
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: {
      writeText: async (value) => { window.copied = value; },
    }});
  });
  await page.route('https://onboarding.test/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/static/')) return route.fulfill({ path: path.join(root, 'src/trusted_router', url.pathname) });
    if (route.request().method() === 'POST') calls.push(url.pathname);
    return route.fulfill({ contentType: 'text/html', body: welcome() });
  });
  await page.setViewportSize({ width, height: 900 });
  await page.goto('https://onboarding.test/console/welcome');
  await expect(page.locator('[data-action="run-first-call"]')).toHaveCount(0);
  await page.getByRole('button', { name: 'Copy API key', exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.copied)).toBe(key);
  await page.locator('#connect-your-app > summary').click();
  await page.getByRole('button', { name: 'Copy message', exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.copied)).toContain(`TrustedRouter API key: ${key}`);
  await page.getByRole('tab', { name: 'Python', exact: true }).click();
  await expect(page.locator('#setup-python')).toBeVisible();
  await page.locator('#setup-python button').click();
  await expect.poll(() => page.evaluate(() => window.copied)).toContain(`api_key="${key}"`);
  await page.getByRole('tab', { name: 'Python', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#setup-js')).toBeVisible();
  await expect(page.locator('#setup-python')).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(calls).toEqual([]);
});
