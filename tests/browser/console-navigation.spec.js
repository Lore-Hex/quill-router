const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');

// Render the real shared shell; unrelated reward markup is tested separately.
function shell(count) {
  return execFileSync(process.env.TEST_PYTHON || 'python3', ['-c', `
import sys
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader
env=Environment(loader=ChoiceLoader([DictLoader({'console/_social_rewards.html':'','_favicon.html':''}),FileSystemLoader('src/trusted_router/templates')]))
print(env.get_template('console/_layout.html').render(workspaces=[{'id':str(i),'name':'Workspace '+str(i)} for i in range(int(sys.argv[1]))], current_workspace_id='0', console_next_path='/console/api-keys', user_email='test@example.test', active='api-keys', page_title='API Keys', provider_mode=False, credits_available='$0.15', credits_empty=False))
`, String(count)], { cwd:root, encoding:'utf8' });
}
for (const count of [1,2]) test(`navigation retains workspace and account controls (${count} workspaces)`, async ({page}) => {
  await page.route('http://console.test/**', route => {
    const url=new URL(route.request().url());
    if(url.pathname.startsWith('/static/')) return route.fulfill({path:path.join(root,'src/trusted_router',url.pathname)});
    return route.fulfill({contentType:'text/html',body:shell(count)});
  });
  await page.setViewportSize({width:390,height:844});
  await page.goto('http://console.test/console/api-keys');
  const menu=page.getByRole('button',{name:'Menu',exact:true});
  await expect(menu).toBeVisible();
  await expect(page.locator('.console-sidebar')).toBeHidden();
  await expect(page.locator('.console-topbar .brand')).toHaveAttribute('href','/');
  await menu.click();
  await expect(page.locator('.console-main')).toBeHidden();
  await expect(page.getByRole('button',{name:'Sign out',exact:true})).toBeVisible();
  await expect(page.locator('form[action="/auth/logout"]')).toHaveCount(1);
  await expect(page.locator('.workspace-selector')).toHaveCount(count>1?1:0);
  await expect(page.locator('.console-workspace-name')).toBeHidden();
  const configuration = page.locator('.console-configuration');
  await expect(configuration).not.toHaveAttribute('open', '');
  await expect(page.getByRole('link', {name:'Routing', exact:true})).toBeHidden();
  await expect(page.getByRole('link', {name:'Earnings', exact:true})).toBeVisible();
  await expect(page.getByRole('link', {name:'Broadcast', exact:true})).toBeVisible();
  await configuration.locator('summary').click();
  await expect(page.getByRole('link', {name:'Routing', exact:true})).toBeVisible();
  await configuration.locator('summary').click();
  if(count>1){
    await expect(page.getByRole('combobox',{name:'Workspace',exact:true})).toBeVisible();
    await expect(page.locator('.workspace-selector input[name="next"]')).toHaveValue('/console/api-keys');
    const request=page.waitForRequest(r=>r.method()==='POST' && r.url().endsWith('/console/workspaces/select'));
    await page.getByRole('combobox',{name:'Workspace',exact:true}).selectOption('1');
    expect((await request).postData()).toContain('workspace_id=1');
    await page.getByRole('button',{name:'Menu',exact:true}).click();
  }
  await page.keyboard.press('Escape');
  await expect(page.locator('.console-main')).toBeVisible();
  await page.setViewportSize({width:1280,height:900});
  await expect(page.locator('.console-sidebar')).toBeVisible();
  await expect(page.locator('.console-topbar .console-utilities')).toBeVisible();
  await expect(page.getByRole('link', {name:'Routing', exact:true})).toBeVisible();
  if(count===1) await expect(page.locator('.console-workspace-name')).toHaveText('Workspace 0');
  if(count===1) await expect(page.locator('.console-workspace-name')).toBeVisible();
  await expect(page.getByRole('button',{name:'Sign out',exact:true})).toBeHidden();
  await page.getByText('Account',{exact:true}).click();
  await expect(page.getByRole('button',{name:'Sign out',exact:true})).toBeVisible();
  await page.setViewportSize({width:320,height:800});
  await page.getByRole('button',{name:'Menu',exact:true}).click();
  await expect(page.locator('.console-workspace-name')).toBeHidden();
  await expect(page.getByRole('link', {name:'Routing', exact:true})).toBeHidden();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
});
