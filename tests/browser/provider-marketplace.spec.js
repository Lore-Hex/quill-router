const { test, expect } = require("@playwright/test");

for (const width of [375, 768, 1440]) {
  test(`provider application and energy questions fit at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/providers/marketplace");

    const application = page.locator('[aria-label="Provider onboarding email template"] pre');
    await expect(application).toContainText("Inference powered by 100% renewable energy? Yes / Partly / No / Unknown:");
    await expect(application).toContainText("API key: DO NOT INCLUDE");
    expect(await application.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);

    const energy = page.getByRole("region", { name: "Is your inference powered by 100% renewable energy?" });
    await energy.scrollIntoViewIfNeeded();
    await expect(energy).toContainText("failover capacity");
    await expect(energy).toContainText("hourly versus annual matching");
    await expect(energy).toContainText("not independently verified certification");
    await page.screenshot({ path: testInfo.outputPath(`energy-${width}.png`) });
  });
}
