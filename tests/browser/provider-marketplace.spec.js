const { test, expect } = require("@playwright/test");

test("provider contact address appears only when a visitor uses the link", async ({ page }) => {
  await page.goto("/providers/marketplace");
  const contacts = page.locator("a[data-provider-contact]");
  const inbox = "provider.ops@nytex.com";
  const plain = "mailto:" + inbox;
  const application = plain + "?subject=Provider%20marketplace%20application%20for%20%5Bcompany%5D";
  await expect(contacts).toHaveCount(4);
  expect((await page.content()).toLowerCase()).not.toContain(inbox);
  for (const contact of await contacts.all()) {
    await expect(contact).toHaveAttribute("href", "/support#support-inquiry");
  }
  await page.evaluate(() => {
    // Let the link's own handler run, then cancel navigation to the mail client.
    for (const event of ["click", "auxclick", "contextmenu"]) {
      document.addEventListener(event, e => e.preventDefault());
    }
  });
  await contacts.nth(0).press("Enter");
  await contacts.nth(1).click();
  await contacts.nth(2).click({ button: "right" });
  await contacts.nth(3).click({ button: "middle" });
  for (const [i, href] of [application, plain, application, application].entries()) {
    await expect(contacts.nth(i)).toHaveAttribute("href", href);
  }
});

test.describe("without JavaScript", () => {
  test.use({ javaScriptEnabled: false });
  test("provider contact leads to support", async ({ page }) => {
    await page.goto("/providers/marketplace");
    await page.getByRole("link", { name: "Email provider operations" }).first().click();
    await expect(page).toHaveURL(/\/support#support-inquiry$/);
    await expect(page.locator("#support-inquiry")).toBeVisible();
  });
});

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
