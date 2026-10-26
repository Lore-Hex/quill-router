const { test, expect } = require("@playwright/test");

for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) {
  test(`OpenRouter alternative fits ${width}px with readable tables`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto("/openrouter-alternative");
    await expect(page.locator("main h1")).toHaveCount(1);
    await expect(page.locator(".faq-panel details")).toHaveCount(11);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await expect(page.locator(".v2-stack")).toHaveCount(4);
    await expect(page.locator("main > section").nth(1)).toHaveAttribute("aria-labelledby", "quick-answer-heading");
    await expect(page.locator('.quote-feature[aria-labelledby="privacy-tiers-heading"] h3')).toHaveCount(3);
    const layout = await page.evaluate(() => {
      const style = (selector) => getComputedStyle(document.querySelector(selector));
      const heading = style('.quote-feature h3');
      const body = style('.quote-feature p');
      return {
        ctaInset: parseFloat(style('.cta-band').paddingLeft),
        resourceInset: parseFloat(style('.link-list li').paddingLeft),
        codeInset: parseFloat(style('.copy-terminal pre').paddingLeft),
        headingSize: parseFloat(heading.fontSize),
        bodySize: parseFloat(body.fontSize),
        headingWeight: parseFloat(heading.fontWeight),
      };
    });
    expect(layout.ctaInset).toBeGreaterThanOrEqual(24);
    expect(layout.resourceInset).toBeGreaterThanOrEqual(20);
    expect(layout.codeInset).toBeGreaterThanOrEqual(20);
    expect(layout.headingSize).toBeGreaterThan(layout.bodySize);
    expect(layout.headingWeight).toBeGreaterThanOrEqual(600);
    if (width <= 720) {
      await expect(page.locator(".v2-stack tbody tr").first()).toHaveCSS("display", "block");
    }
  });
}
