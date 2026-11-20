const { expect, test } = require("@playwright/test");

// Catalog pages (model, comparison, provider) carry one page-specific sign-up
// call to action in the hero. It must be the first thing a phone shows below
// the header, and it must open the shared dialog in sign-up mode.
const PAGES = [
  { family: "model section", path: "/models/moonshotai/kimi-k3/pricing", label: null, pitch: /^Try Kimi K3 on us\. One key/ },
  { family: "comparison", path: "/compare/models/moonshotai/kimi-k3/vs/z-ai/glm-5.3", hero: ".compare-hero", lead: ".compare-lead", label: null, pitch: /One API key runs both\.$/ },
  { family: "provider", path: "/providers/tinfoil", hero: ".pd-hero", lead: ".pd-lead", label: null, pitch: /^Try Tinfoil on us\. One key/ },
];

for (const { family, path, label, pitch, hero: heroSelector = ".catalog-hero", lead = ".lead", ctaText = "Start building" } of PAGES) {
  for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 844 }, { width: 1440, height: 900 }]) {
    test(`${family} page CTA is above the fold and opens sign-up at ${viewport.width}px`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.route("**/v1/models/picker", route => route.fulfill({ json: { data: [] } }));
      await page.goto(path);

      const hero = page.locator(heroSelector);
      // The comparison hero leads with its verdict instead of a label.
      if (label) await expect(hero.locator(".kicker")).toHaveText(label);
      await expect(hero.locator(lead)).toHaveText(pitch);
      const cta = hero.locator("[data-catalog-cta]");
      await expect(cta).toHaveCount(1);
      await expect(cta).toHaveText(ctaText);
      // Above the fold: the button is fully inside the first screen with no scrolling.
      const box = await cta.boundingBox();
      expect(box.y).toBeGreaterThan(0);
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
      await expect(hero.locator(".public-hero-metrics")).toHaveCount(0);
      // The hero itself fits the viewport. (Some section pages carry a wide
      // pricing table below it; that is older layout, tracked separately.)
      expect(await hero.evaluate(el => el.getBoundingClientRect().right - window.innerWidth)).toBeLessThanOrEqual(1);

      await cta.click();
      const dialog = page.locator("#signinModal");
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute("data-mode", "signup");
      await expect(dialog.locator("#signinTitle")).toHaveText("Get your API key");
      // Served HTML links no console URL: the dialog owns the next step.
      expect(await page.locator('main a[href^="/console"]').count()).toBe(0);
    });
  }
}

// The model page has its own hero: the model name, one sentence, price stats
// and the sign-up button with the no-card line under it.
for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 844 }, { width: 1440, height: 900 }]) {
  test(`model page CTA is above the fold and opens sign-up at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.route("**/v1/models/picker", route => route.fulfill({ json: { data: [] } }));
    await page.goto("/models/moonshotai/kimi-k3");

    const hero = page.locator(".md-hero");
    await expect(hero.locator("h1")).toHaveText("Kimi K3");
    const cta = hero.locator("[data-catalog-cta]");
    await expect(cta).toHaveCount(1);
    await expect(cta).toHaveText("Start building");
    const box = await cta.boundingBox();
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
    await expect(hero.locator(".md-no-card")).toContainText("No card required.");
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);

    await cta.click();
    const dialog = page.locator("#signinModal");
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("data-mode", "signup");
    expect(await page.locator('main a[href^="/console"]').count()).toBe(0);
  });
}

// Comparison visitors come for the table, so its hero does not fill the
// screen: the table starts on a phone's first screen and its price row is
// fully visible on a laptop.
for (const { viewport, firstRow } of [
  { viewport: { width: 390, height: 844 }, firstRow: ".compare-table thead" },
  { viewport: { width: 1440, height: 900 }, firstRow: ".compare-table tbody tr:first-child" },
]) {
  test(`comparison table starts on the first screen at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.route("**/v1/models/picker", route => route.fulfill({ json: { data: [] } }));
    await page.goto("/compare/models/moonshotai/kimi-k3/vs/z-ai/glm-5.3");

    const box = await page.locator(firstRow).boundingBox();
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
  });
}

test("model page quickstart switches tabs and folds the prompt on phones", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.route("**/v1/models/picker", route => route.fulfill({ json: { data: [] } }));
  await page.goto("/models/moonshotai/kimi-k3");
  const prompt = page.locator("#md-agent-prompt");
  await expect(prompt).toContainText("Set the model to moonshotai/kimi-k3.");
  const expand = page.locator("[data-md-expand]");
  await expect(expand).toBeVisible();
  await expand.click();
  await expect(expand).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("tab", { name: "Python" }).click();
  await expect(page.locator("#md-panel-python")).toBeVisible();
  await expect(prompt).toBeHidden();
  await expect(page.getByRole("button", { name: "Copy", exact: true })).toBeVisible();
});

test("a model with no Credits route makes no credit claim", async ({ page, context, baseURL }) => {
  // It also stays out of the sign-up button test: the free cell keeps "Start building".
  await context.addCookies([{ name: "tr_cta", value: "start_for_free", url: baseURL }]);
  // Pick the first bring-your-own-key model from the live catalog instead of
  // pinning an id, so the test survives a provider funding that route later.
  // TrustedRouter's own pools and orchestration models route through other
  // models, so they are free to start with no route of their own: skip them.
  const catalog = await page.request.get("/v1/models/picker");
  const models = (await catalog.json()).data.filter(m =>
    !m.id.startsWith("trustedrouter/") && m.trustedrouter?.route_kind === "model");
  let byok = null;
  for (const model of models) {
    const endpoints = await (await page.request.get(`/v1/models/${model.id}/endpoints`)).json();
    if (!endpoints.data.some(e => e.usage_type === "Credits")) { byok = model.id; break; }
  }
  test.skip(byok === null, "every catalog model has a Credits route");
  await page.goto(`/models/${byok}`);
  const hero = page.locator(".md-hero");
  await expect(hero.locator(".md-lead")).toContainText("Bring your own provider key");
  await expect(hero.locator(".md-no-card")).toHaveCount(0);
  await expect(page.locator(".md-no-card")).toHaveCount(0);
  await expect(page.locator("[data-signup-cta]")).toHaveCount(0);
  await expect(hero.locator("[data-catalog-cta]")).toHaveText("Start building");
  await hero.locator("[data-catalog-cta]").click();
  await expect(page.locator("#signinModal")).toHaveAttribute("data-mode", "signup");
});

// Sign-up button A/B test (signup_cta_v1): the free cell swaps only the
// button label; the line under it stays the same in both cells.
for (const [cell, label] of [["start_building", "Start building"], ["start_for_free", "Start for free"]]) {
  test(`sign-up button reads "${label}" for the ${cell} cell`, async ({ page, context, baseURL }) => {
    await context.addCookies([{ name: "tr_cta", value: cell, url: baseURL }]);
    await page.route("**/v1/models/picker", route => route.fulfill({ json: { data: [] } }));
    await page.goto("/providers/tinfoil");
    await expect(page.locator(".pd-hero [data-catalog-cta]")).toHaveText(label);
    await expect(page.locator(".pd-hero .hero-free-note")).toHaveText("1 million free tokens. No card required.");
    await page.goto("/");
    await expect(page.locator("#top .hero .button-primary")).toHaveText(label);
  });
}

// The label is set by an inline head script, so it must not wait for
// dashboard.js (otherwise free-cell visitors see "Start building" first).
test("free-cell label is set without dashboard.js", async ({ page, context, baseURL }) => {
  await context.addCookies([{ name: "tr_cta", value: "start_for_free", url: baseURL }]);
  await page.route("**/static/dashboard.js*", route => route.abort());
  await page.goto("/");
  await expect(page.locator("#top .hero [data-signup-cta]")).toHaveText("Start for free");
  await page.goto("/providers/tinfoil");
  await expect(page.locator(".pd-hero [data-signup-cta]")).toHaveText("Start for free");
});

// A first visit has no cookie: the response sets it, and the label on that
// same page already matches the cell.
test("first visit gets a cell and the matching label", async ({ page, context }) => {
  await context.clearCookies();
  await page.goto("/providers/tinfoil");
  const cell = (await context.cookies()).find(c => c.name === "tr_cta");
  expect(["start_building", "start_for_free"]).toContain(cell && cell.value);
  await expect(page.locator(".pd-hero [data-signup-cta]")).toHaveText(cell.value === "start_for_free" ? "Start for free" : "Start building");
});
