const { expect, test } = require("@playwright/test");

async function layoutReport(page) {
  return page.evaluate(() => {
    const hero = document.querySelector(".cc-hero").getBoundingClientRect();
    const proof = document.querySelector(".cc-proof-band").getBoundingClientRect();
    const primaryButtons = Array.from(document.querySelectorAll(".cc-primary-cta"));
    const prompt = document.querySelector(".cc-prompt-tool").getBoundingClientRect();
    return {
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      heroBottom: hero.bottom,
      proofTop: proof.top,
      viewportHeight: window.innerHeight,
      promptRight: prompt.right,
      buttonsInsideViewport: primaryButtons.every((button) => {
        const bounds = button.getBoundingClientRect();
        return bounds.left >= 0 && bounds.right <= window.innerWidth + 1;
      }),
    };
  });
}

test("Claude Code landing turns the ten-second promise into the real signup flow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/claude-code");

  await expect(
    page.getByRole("heading", { name: "Cut your Claude Code token bill in 10 seconds." }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Every model. In seconds." }),
  ).toBeVisible();
  await expect(page.getByText("your complete key is inserted", { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("YOUR_TRUSTEDROUTER_API_KEY");
  await expect(page.locator(".cc-primary-cta")).toHaveCount(3);

  const heroAsset = await page.request.get("/static/claude-code-hero.png");
  expect(heroAsset.ok()).toBeTruthy();
  expect(Number(heroAsset.headers()["content-length"] || 0)).toBeGreaterThan(10_000);

  const desktop = await layoutReport(page);
  expect(desktop.overflow).toBeLessThanOrEqual(2);
  expect(desktop.proofTop).toBeLessThan(desktop.viewportHeight);
  expect(desktop.buttonsInsideViewport).toBeTruthy();

  await page.locator(".cc-primary-cta").first().click();
  await expect(page.locator("#signinModal")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("Claude Code landing remains readable on phone and narrow tablet", async ({ page }) => {
  for (const viewport of [
    { width: 820, height: 1180 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto("/claude-code");

    const report = await layoutReport(page);
    expect(report.overflow).toBeLessThanOrEqual(2);
    expect(report.proofTop).toBeLessThan(report.viewportHeight);
    expect(report.promptRight).toBeLessThanOrEqual(viewport.width + 1);
    expect(report.buttonsInsideViewport).toBeTruthy();
    await expect(page.locator(".cc-prompt-tool")).toBeVisible();
    await expect(page.getByText("No placeholders. No scavenger hunt.")).toBeVisible();
  }
});
