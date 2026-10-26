const { test, expect } = require("@playwright/test");

for (const [width, height] of [[320, 568], [360, 800], [390, 844], [430, 932], [768, 1024], [1024, 768], [1280, 800], [1440, 900]]) {
  test(`OpenRouter alternative fits ${width}px with readable tables`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto("/openrouter-alternative");
    await expect(page.locator("main h1")).toHaveCount(1);
    await expect(page.locator('[aria-labelledby="quick-answer-heading"] .panel-body > p:first-child strong')).toHaveCSS("color", "rgb(183, 228, 202)");
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
        resourceBorder: style('.link-list li').borderTopWidth,
        codeInset: parseFloat(style('.copy-terminal pre').paddingLeft),
        headingSize: parseFloat(heading.fontSize),
        bodySize: parseFloat(body.fontSize),
        headingWeight: parseFloat(heading.fontWeight),
      };
    });
    expect(layout.ctaInset).toBeGreaterThanOrEqual(24);
    expect(layout.resourceBorder).toBe("0px");
    expect(layout.codeInset).toBeGreaterThanOrEqual(20);
    expect(layout.headingSize).toBeGreaterThan(layout.bodySize);
    expect(layout.headingWeight).toBeGreaterThanOrEqual(600);
    if (width <= 720) {
      await expect(page.locator(".v2-stack thead").first()).toBeVisible();
      expect(await page.locator(".v2-saving-table").evaluate(el => el.getBoundingClientRect().width)).toBeLessThanOrEqual(width - 40);
      await expect(page.locator(".v2-stack tbody tr").first()).toHaveCSS("display", "table-row");
    }
  });
}

test('Tables and code use native scrolling with boundary-aware edge cues', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/openrouter-alternative');
  await expect(page.locator('.table-scroll-tools')).toHaveCount(0);
  for (const viewport of [page.locator('.model-table-wrap').first(), page.locator('.copy-terminal pre')]) {
    await expect(viewport).toHaveClass(/can-scroll-right/);
    await expect(viewport).not.toHaveClass(/can-scroll-left/);
    await viewport.focus();
    await page.keyboard.press('ArrowRight');
    await expect.poll(() => viewport.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
    await expect(viewport).toHaveClass(/can-scroll-left/);
    await viewport.evaluate(el => { el.scrollLeft = el.scrollWidth; });
    await expect(viewport).not.toHaveClass(/can-scroll-right/);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator('.model-table-wrap').first()).not.toHaveClass(/can-scroll/);
});
