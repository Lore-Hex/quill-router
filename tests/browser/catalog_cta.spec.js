const { expect, test } = require("@playwright/test");

// Catalog pages (model, comparison, provider) carry one page-specific sign-up
// call to action in the hero. It must be the first thing a phone shows below
// the header, and it must open the shared dialog in sign-up mode.
const PAGES = [
  { family: "model", path: "/models/moonshotai/kimi-k3", label: "Free to start", pitch: /^Try Kimi K3 on us\. Starter credit/ },
  { family: "model section", path: "/models/moonshotai/kimi-k3/pricing", label: "Free to start", pitch: /^Try Kimi K3 on us\. Starter credit/ },
  { family: "comparison", path: "/compare/models/moonshotai/kimi-k3/vs/z-ai/glm-5.3", label: "Free to start", pitch: /^Try both on us\. Starter credit/ },
  { family: "provider", path: "/providers/tinfoil", label: "Free to start", pitch: /^Try Tinfoil on us\. Starter credit/ },
];

for (const { family, path, label, pitch } of PAGES) {
  for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 844 }, { width: 1440, height: 900 }]) {
    test(`${family} page CTA is above the fold and opens sign-up at ${viewport.width}px`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.route("**/v1/models/picker", route => route.fulfill({ json: { data: [] } }));
      await page.goto(path);

      const hero = page.locator(".catalog-hero");
      await expect(hero.locator(".kicker")).toHaveText(label);
      await expect(hero.locator(".lead")).toHaveText(pitch);
      const cta = hero.locator("[data-catalog-cta]");
      await expect(cta).toHaveCount(1);
      await expect(cta).toHaveText("Create key");
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

test("a model with no Credits route makes no credit claim", async ({ page }) => {
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
  const hero = page.locator(".catalog-hero");
  await expect(hero.locator(".kicker")).toHaveText("Bring your own key");
  await expect(hero.locator(".lead")).not.toContainText("Starter credit");
  await expect(hero.locator(".lead")).not.toContainText("on us");
  await hero.locator("[data-catalog-cta]").click();
  await expect(page.locator("#signinModal")).toHaveAttribute("data-mode", "signup");
});
