const { expect, test } = require("@playwright/test");

for (const width of [1440, 880, 390, 320]) {
  test(`header controls remain reachable after scrolling at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.route("**/v1/models/picker", route => route.fulfill({ json: { data: [] } }));
    for (const path of ["/", "/models", "/providers", "/pricing"]) {
      await page.goto(path);
      await page.evaluate(() => window.scrollTo(0, 900));
      await expect.poll(() => page.locator(".trnav").evaluate(el => el.getBoundingClientRect().top)).toBe(0);
      // A real click also checks that other positioned controls do not intercept the CTA.
      await page.locator(".trnav .signin").click();
      await expect(page.locator("#signinModal")).toBeVisible();
      await page.keyboard.press("Escape");
      await page.locator(".trnav [data-open-search]").click();
      await page.locator("#model-query").fill("test model");
      await expect(page.locator("#model-query")).toBeFocused();
      await page.keyboard.press("Escape");
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    }
  });

  test(`docs anchors and contents clear the header at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/docs");
    const target = page.locator("main h2[id]").last();
    await target.evaluate(el => el.scrollIntoView({ block: "start", behavior: "instant" }));
    const headerBottom = await page.locator(".trnav").evaluate(el => el.getBoundingClientRect().bottom);
    expect(await target.evaluate(el => el.getBoundingClientRect().top)).toBeGreaterThanOrEqual(headerBottom);
    // Check the pinned state before the rail reaches the end of its container.
    await page.evaluate(() => window.scrollTo(0, 900));
    const toc = page.locator(".content-toc");
    if (await toc.isVisible()) {
      expect(await toc.evaluate(el => el.getBoundingClientRect().top)).toBeGreaterThanOrEqual(headerBottom);
      await toc.locator('a[href^="#"]').first().click();
      await expect.poll(() => page.evaluate(() => {
        const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
        return target && target.getBoundingClientRect().top >= document.querySelector(".trnav").getBoundingClientRect().bottom;
      })).toBe(true);
    }
  });
}

for (const path of ["/", "/models"]) {
  test(`landscape menu stays within the viewport on ${path}`, async ({ page }) => {
    await page.setViewportSize({ width: 844, height: 320 });
    await page.goto(path);
    await page.evaluate(() => window.scrollTo(0, 900));
    await page.locator(".trnav .menu").click();
    const menu = page.locator(".trnav nav");
    expect(await menu.evaluate(el => el.getBoundingClientRect().bottom)).toBeLessThanOrEqual(320);
    const status = menu.getByRole("link", { name: "Status", exact: true });
    await status.scrollIntoViewIfNeeded();
    const box = await status.boundingBox();
    expect(box.y).toBeGreaterThanOrEqual(await page.locator(".trnav").evaluate(el => el.getBoundingClientRect().bottom));
    expect(box.y + box.height).toBeLessThanOrEqual(320);
    await status.click();
    await expect(page).toHaveURL(/\/status$/);
  });
}
