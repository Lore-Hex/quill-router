import { expect, test } from "@playwright/test";

test("QR first, real balance transition, reload and model setup tabs", async ({ page, request }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#btc-amount")).toContainText("$10.00 USD");
  await expect(page.locator("#fx-terms")).toContainText("10% FX buffer included");
  await expect(page.locator("#fx-terms")).toContainText("90% becomes USD credits");
  expect((await page.locator("#qr").boundingBox()).y).toBeLessThan((await page.locator("#existing-key").boundingBox()).y);
  await expect(page.locator("#key-reveal")).toBeHidden();
  await expect(page.locator("#account")).toBeHidden();
  await page.reload();
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#account")).toBeHidden();
  await expect(page.locator("#error")).toBeEmpty();
  await request.post("/_test/pay", { data: {} });
  await expect(page.locator("#key-reveal")).toBeVisible({ timeout: 12000 });
  await expect(page.locator("#balance-usd")).toContainText("$10.00");
  await expect(page.locator("#balance-usd")).toHaveText("$10.000800 USD");
  await expect(page.locator("#balance-btc")).toHaveCount(0);
  const key = await page.locator("#your-key").inputValue();
  await page.reload();
  await expect(page.locator("#your-key")).toHaveValue(key);
  await page.selectOption("#model", "kimi/kimi-k2.7");
  for (const label of ["OpenCode", "Crush", "OMP"]) {
    await page.getByRole("tab", { name: label, exact: true }).click();
    await expect(page.locator("#config-code")).toContainText("kimi/kimi-k2.7");
    await expect(page.locator("#config-code")).toContainText("https://api.trustedrouter.com/v1");
  }
  await page.locator("#copy-key").click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(key);
  await page.locator("#amount").fill("5.00");
  await page.getByRole("button", { name: "Update invoice amount" }).click();
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#balance-usd")).toHaveText("$10.000800 USD");
  await page.locator("#your-key").evaluate((node) => { node.value = "sk-tr-v1-test-key"; });
  await page.locator("#env-code").evaluate((node) => { node.textContent = "export LIGHTNINGROUTER_API_KEY='YOUR_API_KEY'"; });
  await page.screenshot({ path: "test-results/funded-desktop.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("existing key replaces only a confirmed canceled invoice and topup adds balance", async ({ page, request }) => {
  const existing = await (await request.post("/_test/existing", { data: {} })).json();
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await page.locator("#existing-key").fill(existing.key);
  await page.getByRole("button", { name: "Use API key", exact: true }).click();
  await expect(page.locator("#balance-usd")).toContainText("$20.00");
  await expect(page.locator("#qr")).toBeVisible();
  await request.post("/_test/pay", { data: {} });
  await expect(page.locator("#balance-usd")).toContainText("$30.00", { timeout: 12000 });
  await expect(page.locator("#your-key")).toHaveValue(existing.key);
});

test("USD edits hide stale QR until invoice is replaced", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await page.locator("#amount").fill("25.00");
  await expect(page.locator("#qr")).toBeHidden();
  await page.getByRole("button", { name: "Update invoice amount" }).click();
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#btc-amount")).toContainText("$25.00 USD");
});

test("review-required invoices hide payment and never reveal an unfunded key", async ({ page }) => {
  await page.route("**/api/invoices/*/refresh", async (route) => {
    const response = await route.fetch();
    const invoice = await response.json();
    await route.fulfill({ response, json: { ...invoice, attention_required: true } });
  });
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#invoice-state")).toContainText("Checkout needs review", { timeout: 12000 });
  await expect(page.locator("#qr")).toBeHidden();
  await expect(page.locator("#invoice-actions")).toBeHidden();
  await expect(page.locator("#key-reveal")).toBeHidden();
  await page.reload();
  await expect(page.locator("#invoice-state")).toContainText("Checkout needs review");
  await expect(page.locator("#qr")).toBeHidden();
});

for (const width of [375, 768, 1440]) {
  test(`responsive layout ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await expect(page.locator("#qr")).toBeVisible();
    await expect(page.locator("#config-code")).toContainText("baseURL");
    const bounds = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth }));
    expect(bounds.content).toBeLessThanOrEqual(bounds.width);
    await page.screenshot({ path: `test-results/lightning-${width}.png`, fullPage: true });
  });
}
