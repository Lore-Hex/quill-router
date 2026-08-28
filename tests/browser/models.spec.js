const { expect, test } = require("@playwright/test");

test("models explorer prioritizes frontier models and searches immediately", async ({ page }) => {
  await page.goto("/models");

  const search = page.getByRole("searchbox", { name: "Search models" });
  await expect(search).toBeVisible();
  const visibleCards = page.locator(".model-result-card:visible");
  await expect(visibleCards.first()).toHaveAttribute("data-model-id", "z-ai/glm-5.3-flash");
  expect(await visibleCards.count()).toBeLessThanOrEqual(24);
  expect(await page.locator(".model-explorer table").count()).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThan(12_000);

  await search.fill("kimi k3");
  await expect(page.locator('.model-result-card[data-model-id="moonshotai/kimi-k3"]')).toBeVisible();
  await expect(page.locator('.model-result-card[data-model-id="trustedrouter/auto"]')).toBeHidden();
  await expect(page.locator("[data-model-result-count]")).toContainText("result");

  await search.fill("");
  await page.locator("[data-model-sort]").selectOption("cached");
  await expect(visibleCards.first()).not.toHaveAttribute("data-cached-price", "");
});

test("models explorer loads provider cache prices without horizontal overflow", async ({ page }) => {
  await page.goto("/models?q=glm-5.3-flash");
  const card = page.locator('.model-result-card[data-model-id="z-ai/glm-5.3-flash"]');
  await expect(card).toBeVisible();
  await card.locator("summary").click();
  await expect(card.locator(".model-route-row").first()).toBeVisible();
  await expect(card.getByText("Cached input", { exact: true }).first()).toBeVisible();

  for (const viewport of [
    { width: 1280, height: 800 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow).toBeLessThanOrEqual(2);
  }
});
