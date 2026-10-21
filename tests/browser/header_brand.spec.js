const { test, expect } = require('@playwright/test');

for (const width of [1440, 1280, 880, 390, 320]) {
  test(`brand and model search stay aligned at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route('**/v1/models/picker', route => route.fulfill({ json: { data: [] } }));
    for (const path of ['/', '/models']) {
      await page.goto(path);
      await page.evaluate(() => document.fonts.ready);
      const header = await page.locator('.trnav .tbar').boundingBox();
      const brand = await page.locator('.trnav .brand').boundingBox();
      const mark = page.locator('.trnav .brand-mark');
      await expect(mark).toHaveCSS('width', '27px');
      await expect(mark).toHaveCSS('height', '27px');
      const search = page.locator('.trnav [data-open-search]');
      const box = await search.boundingBox();
      expect(brand.y).toBeGreaterThanOrEqual(header.y);
      expect(brand.y + brand.height).toBeLessThanOrEqual(header.y + header.height);
      expect(box.x).toBeGreaterThan(brand.x + brand.width);
      expect(Math.abs(brand.y + brand.height / 2 - box.y - box.height / 2)).toBeLessThan(1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: testInfo.outputPath(path === '/' ? 'homepage.png' : 'models.png') });
      await search.click();
      await expect(page.locator('#model-query')).toBeFocused();
      await page.keyboard.press('Escape');
    }
  });
}
