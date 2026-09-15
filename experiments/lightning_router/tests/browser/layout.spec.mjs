import { expect, test } from "./fixtures.mjs";

const session = page => page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));

test("Docs explains the shared API without starting a payment", async ({ page }) => {
  const mutations = [];
  page.on("request", req => { if (req.method() !== "GET") mutations.push(req.url()); });
  await page.goto("/docs");
  await expect(page.getByRole("heading", { name: "Docs", exact: true })).toBeVisible();
  await expect(page.locator("main")).toContainText("same TrustedRouter API key");
  await expect(page.locator("main")).toContainText("https://api.trustedrouter.com/v1");
  await expect(page.getByRole("link", { name: "TrustedRouter API docs", exact: true })).toHaveAttribute("href", "https://trustedrouter.com/docs");
  await expect(page.locator('header nav a[aria-current="page"]')).toHaveText("Docs");
  expect(mutations).toEqual([]);
});

test("a canceled invoice can be renewed without revealing or replacing its unfunded key", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await session(page);
  const response = await page.request.post(`/api/invoices/${before.invoice.id}/cancel`, {
    headers: { Authorization: `Bearer ${before.key}` }, data: {},
  });
  expect(response.ok()).toBe(true);
  await page.reload();
  await expect(page.getByRole("button", { name: "New invoice", exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/layout-canceled.png", fullPage: true });
  await page.getByRole("button", { name: "New invoice", exact: true }).click();
  await expect(page.locator("#qr")).toBeVisible();
  const after = await session(page);
  expect(after.key).toBe(before.key);
  expect(after.invoice.id).not.toBe(before.invoice.id);
  expect(after.reveal).toBe(false);
  await expect(page.locator("#existing-key")).toHaveValue("");
  await expect(page.locator("#copy-key")).toBeHidden();
  await expect(page.locator("#account")).toBeHidden();
});

test("one key field connects, copies, masks on reload, and clears on sign out", async ({ page, request }) => {
  const existing = await (await request.post("/_test/existing", { data: {} })).json();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await page.getByLabel("Existing API key").fill(existing.key);
  await page.getByRole("button", { name: "Use API key", exact: true }).click();
  await expect(page.getByLabel("Your API key", { exact: true })).toHaveValue(existing.key);
  await expect(page.locator("#existing-key")).toHaveAttribute("readonly", "");
  await expect(page.locator("#your-key")).toHaveCount(0);
  await expect(page.locator("#use-key")).toBeHidden();
  await page.getByRole("button", { name: "Show key", exact: true }).click();
  await expect(page.locator("#existing-key")).toHaveAttribute("type", "text");
  await page.getByRole("button", { name: "Copy API key", exact: true }).first().click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(existing.key);
  await page.reload();
  await expect(page.locator("#existing-key")).toHaveValue(existing.key);
  await expect(page.locator("#existing-key")).toHaveAttribute("type", "password");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByLabel("Existing API key")).toHaveValue("");
  await expect(page.locator("#existing-key")).toBeEditable();
  await expect(page.locator("#copy-key")).toBeHidden();
  await expect(page.locator("#account")).toBeHidden();
  expect((await session(page)).key).not.toBe(existing.key);
});

test("expired invoice renewal preserves the key when cancellation is uncertain", async ({ page }) => {
  await page.route("**/api/invoices/*/refresh", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), expired: true } });
  });
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await session(page);
  await page.reload();
  await expect(page.getByRole("button", { name: "New invoice", exact: true })).toBeVisible();
  await page.route("**/api/invoices/*/cancel", route => route.abort("failed"));
  await page.getByRole("button", { name: "New invoice", exact: true }).click();
  await expect(page.locator("#error")).not.toBeEmpty();
  const after = await session(page);
  expect(after.key).toBe(before.key);
  expect(after.requestId).toBe(before.requestId);
  expect(after.invoice.id).toBe(before.invoice.id);
  await expect(page.locator("#qr")).toBeHidden();
  await expect(page.locator("#copy-key")).toBeHidden();
});

for (const pending of [
  { state: "ACCEPTED", expired: true },
  { state: "SETTLED", credited: false },
  { state: "CANCELED", attention_required: true },
]) {
  test(`no renewal or key reveal while ${JSON.stringify(pending)}`, async ({ page }) => {
    await page.route("**/api/invoices/*/refresh", async route => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), ...pending } });
    });
    await page.goto("/");
    await expect(page.locator("#qr")).toBeVisible();
    await page.reload();
    await expect(page.locator("#qr")).toBeHidden();
    await expect(page.locator("#invoice-state")).not.toHaveText("Waiting for payment");
    await expect(page.locator("#new-invoice")).toBeHidden();
    await expect(page.locator("#existing-key")).toHaveValue("");
    await expect(page.locator("#copy-key")).toBeHidden();
  });
}

test("failed sign out keeps the single connected key and invoice", async ({ page, request }) => {
  const existing = await (await request.post("/_test/existing", { data: {} })).json();
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await page.getByLabel("Existing API key").fill(existing.key);
  await page.getByRole("button", { name: "Use API key", exact: true }).click();
  await expect(page.locator("#use-key")).toBeEnabled();
  const before = await session(page);
  await page.route("**/api/invoices/*/cancel", route => route.abort("failed"));
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.locator("#error")).not.toBeEmpty();
  await expect(page.getByLabel("Your API key", { exact: true })).toHaveValue(existing.key);
  await expect(page.locator("#existing-key")).not.toBeEditable();
  expect((await session(page)).invoice.id).toBe(before.invoice.id);
});

test("restored keys remain accessible when balance lookup fails", async ({ page, request }) => {
  const existing = await (await request.post("/_test/existing", { data: {} })).json();
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await page.getByLabel("Existing API key").fill(existing.key);
  await page.getByRole("button", { name: "Use API key", exact: true }).click();
  await expect(page.locator("#use-key")).toBeEnabled();
  await page.route("**/api/account", route => route.fulfill({ status: 503, json: { error: "temporarily_unavailable" } }));
  await page.reload();
  await expect(page.getByLabel("Your API key", { exact: true })).toHaveValue(existing.key);
  await expect(page.locator("#balance-usd")).toHaveText("Unavailable");
  await expect(page.locator("#copy-key")).toBeVisible();
  await expect(page.locator("#sign-out")).toBeVisible();
  await page.unroute("**/api/account");
  await page.locator("#sign-out").click();
  await expect(page.getByLabel("Existing API key")).toHaveValue("");
  await expect(page.locator("#account")).toBeHidden();
});

for (const width of [320, 375, 768, 1440]) {
  test(`right aligned navigation and funded balance fit ${width}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 900 });
    const existing = await (await request.post("/_test/existing", { data: {} })).json();
    await page.route("**/api/account", async route => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), balance_usd: "1234567.123456" } });
    });
    await page.goto("/");
    await expect(page.locator("#qr")).toBeVisible();
    const nav = page.getByRole("navigation", { name: "Main", exact: true });
    await expect(nav.getByRole("link")).toHaveText(["Usage", "Pricing", "Docs"]);
    await expect(page.locator(".brand")).toContainText("⚡");
    const header = await page.locator("header").boundingBox();
    const bounds = await nav.boundingBox();
    expect(header.x + header.width - bounds.x - bounds.width).toBeLessThanOrEqual(33);
    await page.getByLabel("Existing API key").fill(existing.key);
    await page.getByRole("button", { name: "Use API key", exact: true }).click();
    await expect(page.locator("#balance-usd")).toBeVisible();
    await expect(page.locator("#balance-usd")).toHaveText("$1234567.123456 USD");
    const value = await page.locator("#balance-usd").boundingBox();
    const buttons = await page.locator("#refresh-balance").boundingBox();
    expect(value.x + value.width <= buttons.x || value.y + value.height <= buttons.y).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/layout-funded-${width}.png`, fullPage: true });
  });
}
