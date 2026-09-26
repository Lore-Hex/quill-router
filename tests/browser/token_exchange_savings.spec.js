const { test, expect } = require("@playwright/test");

test("enterprise estimate responds to routing, prices and invalid spend", async ({ page }) => {
  await page.goto("/token-exchange/savings");
  await expect(page.locator("#tx-saved")).toHaveText("$1,057,260");
  await page.getByRole("switch").uncheck();
  await expect(page.locator("#tx-total")).toHaveText("$3,350,000");
  await expect(page.locator("#tx-fee")).toHaveText("$0");
  await expect(page.locator("[data-tx-example]").first()).toHaveText("Google Cloud");
  await page.getByRole("switch").check();
  await page.locator("#tx-spend").fill("100");
  await page.locator("#tx-share").fill("100");
  await page.locator("#tx-discount").fill("0");
  await expect(page.locator("#tx-total")).toHaveText("$105.50");
  await expect(page.locator("#tx-savings-label")).toHaveText("Estimated monthly increase");
  await page.locator("#tx-spend").fill("-1");
  await expect(page.locator("#tx-spend")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#tx-copy")).toBeDisabled();
  await page.getByRole("button", { name: "Reset example" }).click();
  await expect(page.locator("#tx-saved")).toHaveText("$1,057,260");
  await expect(page.locator("#tx-copy")).toBeEnabled();
});

test("estimate links restore assumptions and can be copied", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/token-exchange/savings#spend=123.45&share=100&discount=80&enabled=0");
  await expect(page.locator("#tx-total")).toHaveText("$123.45");
  await expect(page.getByRole("switch")).not.toBeChecked();
  await page.locator("#tx-copy").click();
  await expect(page.locator("#tx-copy-status")).toContainText("Copied.");
  const url = new URL(await page.evaluate(() => navigator.clipboard.readText()));
  expect(url.pathname).toBe("/token-exchange/savings");
  expect(url.hash).toBe("#spend=123.45&share=100&discount=80&enabled=0");
});

test("mobile calculator has no horizontal overflow and supports keyboard ranges", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 740 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/token-exchange/savings");
  await expect(page.locator("#tx-calculator")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.locator("#tx-share").focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#tx-share-label")).toHaveText("41%");
  await page.locator("#tx-spend").fill("0");
  await expect(page.locator("#tx-total")).toHaveText("$0");
  await expect(page.locator("#tx-percent")).toHaveText("0.0% less on tokens");
});
