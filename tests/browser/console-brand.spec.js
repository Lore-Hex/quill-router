const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');

// Use the complete production shell and stylesheet order, not a reduced shell.
function fixture(kind) {
  return execFileSync(process.env.TEST_PYTHON || path.join(root, '.venv', 'bin', 'python'), ['-c', `
import sys
from types import SimpleNamespace as NS
from trusted_router.config import Settings
from trusted_router.routes.console._shared import ConsoleContext, render
workspace=NS(id='fixture', name='Design review')
ctx=ConsoleContext(NS(id='fixture',email='fixture@example.test'),NS(),workspace,[workspace],True,'https://api.example.test/v1')
key=NS(name='Design fixture',label='fixture-not-a-real-key',usage_display='$0.00',limit_display='none',windows=[],disabled=False,hash='fixture')
print(render('console/'+sys.argv[1]+'.html', settings=Settings(environment='test',release='brand-regression'),ctx=ctx,active='api-keys',page_title='API Keys',keys=[key],suggested=NS(daily='20',weekly='100',monthly='200'),revealed_key='fixture-not-a-real-key'))
`, kind], { cwd: root, encoding: 'utf8' });
}

for (const kind of ['api_keys', 'welcome']) for (const width of [390, 1280]) {
  test(`brand fonts and colors on ${kind} at ${width}px`, async ({ page }, testInfo) => {
    const html = fixture(kind);
    await page.route('https://brand.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/static/')) return route.fulfill({path:path.join(root,'src/trusted_router',url.pathname)});
      if (url.pathname.includes('/social-rewards')) return route.fulfill({json:{eligible:true,enabled:true,dismissed:true,workspace_name:'Design review',actions:['github','x_trustedrouter','x_founder','linkedin'].map(id=>({id,label:id,url:'https://example.test/'+id,claimed:false}))}});
      return route.fulfill({contentType:'text/html',body:html});
    });
    await page.setViewportSize({width,height:900});
    await page.goto('https://brand.test/console/'+kind);
    await page.evaluate(() => document.fonts.ready);
    await expect(page.locator('h1')).toHaveCSS('font-family', 'Archivo, Arial, sans-serif');
    await expect(page.locator('h1')).toHaveCSS('color', 'rgb(237, 248, 245)');
    await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(10, 16, 13)');
    expect(await page.evaluate(() => document.fonts.check('16px Archivo'))).toBe(true);
    await page.screenshot({path:testInfo.outputPath(`${kind}-${width}.png`),fullPage:true});
    await expect(page.locator('[data-rewards-api-keys]')).toHaveCount(0);
  });
}
