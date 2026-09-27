const { test, expect } = require("@playwright/test");

const paths = [
  "/performance-routing", "/spend-controls", "/prompt-caching", "/model-precision",
  "/docs/performance-routing", "/docs/spend-controls", "/docs/model-precision",
];

for (const width of [375, 1440]) {
  for (const path of paths) {
    test(`${path} is readable at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      const response = await page.goto(path);
      expect(response.status()).toBe(200);
      await expect(page.locator("h1")).toHaveCount(1);
      await expect(page.locator("h1")).toBeVisible();
      await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
        "href", `https://trustedrouter.com${path}`,
      );
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
      const heading = await page.locator("h1").boundingBox();
      const lead = await page.locator(".routing-feature-hero .lead").boundingBox();
      const actions = await page.locator(".routing-feature-hero .hero-actions").boundingBox();
      expect(lead.y).toBeGreaterThanOrEqual(heading.y + heading.height);
      expect(actions.y).toBeGreaterThanOrEqual(lead.y + lead.height);
      for (const block of await page.locator("pre").all()) {
        const box = await block.boundingBox();
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(width);
      }
      await expect(page.getByRole("navigation", { name: "Related routing features" })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("routing-feature.png"), fullPage: true });
    });
  }
}
