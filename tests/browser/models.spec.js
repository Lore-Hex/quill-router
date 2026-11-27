const { expect, test } = require("@playwright/test");

test("models explorer prioritizes frontier models and searches immediately", async ({ page }) => {
  await page.goto("/models");

  const search = page.getByRole("searchbox", { name: "Search models" });
  await expect(search).toBeVisible();
  const visibleCards = page.locator("[data-model-card]:visible");
  await expect(visibleCards.first()).toHaveAttribute("data-model-id", "z-ai/glm-5.3-flash");
  expect(await visibleCards.count()).toBeLessThanOrEqual(24);
  await expect(page.getByRole("table", { name: "Model catalog" })).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThan(12_000);

  await search.fill("kimi k3");
  await expect(page.locator('[data-model-card][data-model-id="moonshotai/kimi-k3"]')).toBeVisible();
  await expect(page.locator('[data-model-card][data-model-id="trustedrouter/auto"]')).toBeHidden();
  await expect(page.locator("[data-model-result-count]")).toContainText("model");

  await search.fill("");
  await page.locator('[data-sort-key="price"]:visible').click();
  await expect(visibleCards.first()).not.toHaveAttribute("data-input-price", "");
});

test("models explorer loads provider cache prices without horizontal overflow", async ({ page }) => {
  await page.goto("/models?q=glm-5.3-flash");
  const card = page.locator('[data-model-card][data-model-id="z-ai/glm-5.3-flash"]');
  await expect(card).toBeVisible();
  await card.locator("[data-route-toggle]").click();
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
      route("unknown-zdr", { provider_confidential_compute: true, provider_e2ee: true, provider_zero_data_retention: null }),
      route("retained", { provider_confidential_compute: true, provider_e2ee: true, provider_zero_data_retention: false }),
      route("novita", { stores_content: false, attested_gateway: true }),
      route("novita", { provider_confidential_compute: true, provider_e2ee: true, provider_zero_data_retention: true }, "BYOK"),
    ] },
  }));
  await page.goto("/models?q=glm-5.3-flash");
  const card = page.locator('[data-model-id="z-ai/glm-5.3-flash"]');
  await card.locator("[data-route-toggle]").click();
  const rows = card.locator(".model-route-row");
  await expect(rows).toHaveCount(6);
  await expect(rows.filter({ hasText: "tinfoil" }).locator("[data-privacy]")).toHaveCount(2);
  await expect(rows.filter({ hasText: "openai" }).locator('[data-privacy="zdr"]')).toBeVisible();
  for (const slug of ["phala", "novita", "unknown-zdr", "retained"]) {
    await expect(rows.filter({ hasText: slug }).locator("[data-privacy]")).toHaveCount(0);
    await expect(rows.filter({ hasText: slug })).toContainText("Not verified");
  }
});

test("model privacy is visible above provider details and filterable", async ({ page }) => {
  await page.goto("/models?filter=e2e");
  const cards = page.locator("[data-model-card]:visible");
  expect(await cards.count()).toBeGreaterThan(0);
  for (const card of await cards.all()) {
    await expect(card.locator('.models-routes [data-privacy="confidential"]')).toBeVisible();
  }
  await page.getByRole("link", { name: "ZDR", exact: true }).click();
  await expect(page).toHaveURL(/filter=zdr/);
  await expect(cards.first().locator('.models-routes [data-privacy="zdr"]')).toBeVisible();
});

for (const width of [1440, 390, 320]) {
  test(`catalog uses one sticky search with shortcuts and URL state at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/models?q=kimi&sort=name");
    const search = page.getByRole("searchbox", { name: "Search models" });
    await expect(page.getByRole("searchbox")).toHaveCount(1);
    await expect(search).toHaveValue("kimi");
    await expect(page.locator("[data-model-sort]")).toHaveValue("name");
    await expect(page.locator("#model-search")).toHaveCount(0);
    for (const shortcut of ["Control+k", "Meta+k"]) {
      await search.evaluate(el => el.blur());
      await page.keyboard.press(shortcut);
      await expect(search).toBeFocused();
    }
    await search.fill("no-such-model-xyz");
    await expect(page.locator("[data-model-result-count]")).toHaveText("0 models");
    await expect(page.locator("[data-model-empty]")).toBeVisible();
    await expect(page.locator("[data-model-result-count]")).toBeInViewport();
    await page.locator("[data-model-clear]").first().click();
    await expect(search).toHaveValue("");
    await expect(search).toBeFocused();
    await expect(page).toHaveURL(/\/models$/);
    await search.fill("deepseek");
    await expect(search).toHaveValue("deepseek");
    await expect(page).toHaveURL(/q=deepseek/);
    await page.reload();
    await expect(search).toHaveValue("deepseek");
    await page.evaluate(() => window.scrollTo(0, 800));
    expect((await search.boundingBox()).y).toBeGreaterThanOrEqual(0);
    await search.fill("kimi k3");
    await expect(page.locator('[data-model-id="moonshotai/kimi-k3"]')).toBeVisible();
    await expect(page.locator("[data-model-result-count]")).toBeInViewport();
    await expect(search).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("catalog-sticky-search.png") });
    await page.locator('[data-model-id="moonshotai/kimi-k3"] .models-name h2 a').first().click();
    await expect(page.locator(".trnav [data-open-search]")).toBeVisible();
    await page.locator(".trnav [data-open-search]").click();
    await expect(page.locator("#model-query")).toBeFocused();
  });
}

for (const width of [1440, 390]) {
  test(`type filters and media prices at ${width}px`, async ({ page }) => {
    await page.setViewportSize({width, height: 900});
    await page.goto('/models?makes=image');
    const cards = page.locator('[data-model-card]:visible');
    for (const kind of ['image', 'video', 'speech', 'embeddings', 'text']) {
      if (width < 761 && ['speech', 'embeddings'].includes(kind)) {
        await page.locator('[data-model-kind-more]').selectOption(kind);
      } else {
        await page.locator(`[data-model-kind-tab="${kind}"]`).click();
      }
      await expect(page).toHaveURL(new RegExp(`makes=${kind}`));
      expect(await cards.count()).toBeGreaterThan(0);
      for (const card of await cards.all()) await expect(card).toHaveAttribute('data-kind', kind);
      await page.reload();
      await expect(page.locator('[data-model-explorer]')).toHaveAttribute('data-active-kind', kind);
    }
    for (const [kind, unit] of [['image', '/image'], ['video', '/sec']]) {
      await page.goto(`/models?makes=${kind}&sort=price`);
      const priced = cards.filter({has: page.locator(`[data-col="price"]`)}).first();
      await expect(priced.locator('[data-col="price"]')).toContainText(unit);
      await expect(priced.locator('h2 a')).toHaveAttribute('href', /\/models\//);
      await expect(page.locator('[data-model-card] .models-publisher').first()).toHaveAttribute('href', /\/providers\//);
      await priced.locator('[data-route-toggle]').click();
      await expect(priced.locator('.model-route-row').first()).toBeVisible();
      await expect(priced.locator('.model-route-results')).toContainText(unit);
      await expect(priced.locator('.model-route-row a').first()).toHaveAttribute('href', /\/providers\//);
      const prices = await cards.evaluateAll(rows => rows.map(r => Number(r.dataset.mediaPrice || Infinity)));
      expect(prices).toEqual([...prices].sort((a,b) => a-b));
      expect(await page.evaluate(() => document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(1);
    }
  });
}

for (const width of [1440, 390]) {
  test(`route filters retain type, search and sort at ${width}px`, async ({ page }) => {
    await page.setViewportSize({width, height: 844});
    await page.goto('/models?makes=text&q=glm&sort=name');
    if (width < 761) await page.locator('[data-route-select]').selectOption('/models?filter=zdr');
    else await page.getByRole('link', {name:'ZDR', exact:true}).click();
    await expect(page).toHaveURL(/filter=zdr/);
    const params = new URL(page.url()).searchParams;
    expect(Object.fromEntries(params)).toMatchObject({makes:'text',q:'glm',sort:'name',filter:'zdr'});
    await expect(page.getByRole('searchbox', {name:'Search models'})).toHaveValue('glm');
    await expect(page.locator('[data-model-card]:visible').first()).toHaveAttribute('data-kind','text');
  });
}
