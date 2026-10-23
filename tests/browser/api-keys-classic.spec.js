const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const secret = 'sk-tr-demo-not-a-real-key';
function fixture(reveal = false, empty = false) {
  return execFileSync(process.env.TEST_PYTHON || path.join(root, '.venv/bin/python'), ['-c', `
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/trusted_router/templates'))
key=dict(name='Production',label='sk-tr-v1-...demo',hash='demo',disabled=False,limit_input='',limit_display='none',usage_display='$1.25',windows=[dict(name=w,input='',limit_display=None) for w in ['daily','weekly','monthly']])
print(env.get_template('console/api_keys.html').render(user={'id':'test-user'},user_email='preview@example.test',workspaces=[dict(id='preview',name='Personal Workspace')],current_workspace_id='preview',page_title='API Keys',page_subtitle='Long-lived keys for your applications.',active='api-keys',keys=[] if ${empty ? 'True' : 'False'} else [key],created_key='${secret}' if ${reveal ? 'True' : 'False'} else None,suggested=dict(daily=20,weekly=100,monthly=200)))
`], { cwd: root, encoding: 'utf8' });
}
async function load(page, reveal = false, empty = false) {
  const html = fixture(reveal, empty);
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => {window.copied = value;}}}));
  await page.route('https://keys.test/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/static/')) return route.fulfill({path:path.join(root,'src/trusted_router',url.pathname)});
    if (url.pathname.includes('social-rewards')) return route.fulfill({json:{enabled:false,eligible:false}});
    return route.fulfill({contentType:'text/html',body:html});
  });
  await page.goto('https://keys.test/console/api-keys');
  await page.evaluate(() => document.fonts.ready);
}
for (const width of [320, 390, 1440]) {
  test(`key management and creation stay visible at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width,height:1000});
    await load(page);
    await expect(page.getByText('Used $1.25')).toBeVisible();
    await expect(page.getByRole('button',{name:'Disable',exact:true})).toBeVisible();
    await expect(page.locator('#new-api-key input[name="name"]')).toBeVisible();
    await expect(page.locator('#new-api-key input[name="limit_daily"]')).toBeVisible();
    await page.getByRole('button',{name:'New key',exact:true}).click();
    await expect(page.locator('#new-api-key input[name="name"]')).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => { document.activeElement.blur(); window.scrollTo(0,0); });
    await page.waitForTimeout(400);
    await page.screenshot({path:testInfo.outputPath(`api-keys-${width}.png`),fullPage:true});
  });
  test(`created key and agent message copy immediately at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width,height:1000});
    await load(page, true);
    await expect(page.locator('#created-agent-message')).toBeVisible();
    await page.getByRole('button',{name:'Copy complete agent message'}).click();
    await expect.poll(() => page.evaluate(() => window.copied)).toContain(secret);
    await expect.poll(() => page.evaluate(() => window.copied)).toContain('ask DeepSeek Flash');
    await page.getByRole('button',{name:'Copy API key',exact:true}).click();
    await expect.poll(() => page.evaluate(() => window.copied)).toBe(secret);
    await expect(page.locator('[data-key-onboarding]')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => { document.activeElement.blur(); window.scrollTo(0,0); });
    await page.waitForTimeout(400);
    await page.screenshot({path:testInfo.outputPath(`created-key-${width}.png`),fullPage:true});
  });
}
test('creation and budget controls work without JavaScript', async ({browser}) => {
  const context = await browser.newContext({javaScriptEnabled:false});
  const page = await context.newPage();
  await load(page);
  await expect(page.locator('#new-api-key input[name="name"]')).toBeVisible();
  await expect(page.locator('.key-budget-form button')).toBeVisible();
  await context.close();
});
test('empty workspace exposes the creation form', async ({page}) => {
  await load(page,false,true);
  await expect(page.getByRole('button',{name:'Create key',exact:true})).toBeVisible();
  await expect(page.getByText('No keys yet. Create one above.')).toBeVisible();
});
