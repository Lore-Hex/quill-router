const { expect, test } = require("@playwright/test");

const catalog = [
  { id: "qwen/qwen3.8-27b", name: "Qwen: Qwen3.8 27B" },
  { id: "qwen/qwen-3-8-27b", name: "Qwen 3.8 27B" },
  { id: "lightning-ai/qwen3.8-27b", name: "Qwen3.8-27B" },
  { id: "cerebras/qwen-3.8-27b", name: "Qwen 3.8 27B" },
  { id: "qwen/qwen3.6-27b", name: "Qwen3.6 27B" },
];

for (const width of [1280, 390]) {
  test(`header search matches model names across punctuation at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await page.route("**/v1/models/picker", route => route.fulfill({ json: { data: catalog } }));
    await page.goto("/");
    await page.locator("[data-open-search]").first().click();
    for (const query of ["Qwen 3.8 27B", "QWEN 3-8 27B", "qwen3.8-27b", "27b qwen 3.8"]) {
      await page.locator("#model-query").fill(query);
      const results = page.locator("#search-results a");
      await expect(results).toHaveCount(4);
      await expect(results.filter({ hasText: "qwen/qwen3.8-27b" })).toHaveCount(1);
      await expect(results.filter({ hasText: "3.6" })).toHaveCount(0);
    }
    await page.screenshot({ path: testInfo.outputPath("model-search.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(2);
    await page.locator("#model-query").fill("no-such-model");
    await expect(page.locator("#search-results a")).toHaveCount(0);
  });
}

test("model explorer finds the same canonical model across punctuation", async ({ page }) => {
  await page.goto("/models");
  for (const query of ["Qwen 3.8 27B", "Qwen 3-8 27B", "qwen3.8-27b"]) {
    await page.getByRole("searchbox", { name: "Search models" }).fill(query);
    await expect(page.locator('[data-model-id="qwen/qwen3.8-27b"]')).toBeVisible();
    await expect(page.locator('[data-model-id="qwen/qwen3.6-27b"]')).toBeHidden();
  }
});
