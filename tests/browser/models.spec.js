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
  await expect(card.locator(".model-route-head")).not.toContainText("Routes");
  await expect(card.locator(".model-route-results")).not.toContainText("BYOK");

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

test("expanded provider privacy shows both dimensions and never combines different routes", async ({ page }) => {
  const route = (provider, posture, usage_type = "Credits") => ({
    provider, provider_name: provider, usage_type, trustedrouter: posture,
    pricing: { prompt: "0.000001", completion: "0.000002" },
  });
  await page.route("**/v1/models/z-ai/glm-5.3-flash/endpoints", (request) => request.fulfill({
    json: { data: [
      route("tinfoil", { provider_confidential_compute: true, provider_e2ee: true, provider_zero_data_retention: true }),
      route("phala", { provider_confidential_compute: true, provider_e2ee: false }),
      route("phala", { provider_confidential_compute: false, provider_e2ee: true }),
      route("openai", { provider_zero_data_retention: true }),
      route("novita", { stores_content: false, attested_gateway: true }),
      route("novita", { provider_confidential_compute: true, provider_e2ee: true, provider_zero_data_retention: true }, "BYOK"),
    ] },
  }));
  await page.goto("/models?q=glm-5.3-flash");
  const card = page.locator('[data-model-id="z-ai/glm-5.3-flash"]');
  await card.locator("summary").click();
  const rows = card.locator(".model-route-row");
  await expect(rows).toHaveCount(4);
  await expect(rows.filter({ hasText: "tinfoil" }).locator("[data-privacy]")).toHaveCount(2);
  await expect(rows.filter({ hasText: "openai" }).locator('[data-privacy="zdr"]')).toBeVisible();
  for (const slug of ["phala", "novita"]) {
    await expect(rows.filter({ hasText: slug }).locator("[data-privacy]")).toHaveCount(0);
    await expect(rows.filter({ hasText: slug })).toContainText("Not verified");
  }
});

test("model privacy is visible above provider details and filterable", async ({ page }) => {
  await page.goto("/models?filter=e2e");
  const cards = page.locator("[data-model-card]:visible");
  expect(await cards.count()).toBeGreaterThan(0);
  for (const card of await cards.all()) {
    await expect(card.locator('.model-card-heading [data-privacy="confidential"]')).toBeVisible();
  }
  await page.getByRole("link", { name: "ZDR", exact: true }).click();
  await expect(page).toHaveURL(/filter=zdr/);
  await expect(cards.first().locator('.model-card-heading [data-privacy="zdr"]')).toBeVisible();
});
