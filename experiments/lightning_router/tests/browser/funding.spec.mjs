import { expect, test } from "./fixtures.mjs";

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
  const key = await page.locator("#existing-key").inputValue();
  await page.reload();
  await expect(page.locator("#existing-key")).toHaveValue(key);
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
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
  await page.locator("#existing-key").evaluate((node) => { node.value = "sk-tr-v1-test-key"; });
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
  await expect(page.locator("#existing-key")).toHaveValue(existing.key);
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

test("lost cancel response is reconciled before replacing the invoice", async ({ page }) => {
  let cancellations = 0;
  await page.route("**/api/invoices/*/cancel", async (route) => {
    cancellations += 1;
    const response = await route.fetch();
    expect((await response.json()).state).toBe("CANCELED");
    await route.abort("failed");
  });
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  await page.locator("#amount").fill("7.00");
  await page.getByRole("button", { name: "Update invoice amount" }).click();
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#btc-amount")).toContainText("$7.00 USD");
  await expect(page.locator("#error")).toBeEmpty();
  const after = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  expect(after.key).toBe(before.key);
  expect(after.invoice.id).not.toBe(before.invoice.id);
  expect(cancellations).toBe(1);
  await expect(page.locator("#account")).toBeHidden();
});

test("unconfirmed cancellation preserves the original invoice and key", async ({ page }) => {
  await page.route("**/api/invoices/*/cancel", (route) => route.abort("failed"));
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  await page.locator("#amount").fill("7.00");
  await page.getByRole("button", { name: "Update invoice amount" }).click();
  await expect(page.locator("#error")).not.toBeEmpty();
  const after = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  expect(after.key).toBe(before.key);
  expect(after.invoice.id).toBe(before.invoice.id);
  expect(after.requestId).toBe(before.requestId);
  await expect(page.locator("#qr")).toBeHidden();
});

test("payment winning a lost cancel response keeps the funded key", async ({ page, request }) => {
  await page.route("**/api/invoices/*/cancel", async (route) => {
    await request.post("/_test/pay", { data: {} });
    const response = await route.fetch();
    expect((await response.json()).state).toBe("SETTLED");
    await route.abort("failed");
  });
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  await page.locator("#amount").fill("7.00");
  await page.getByRole("button", { name: "Update invoice amount" }).click();
  await expect(page.locator("#existing-key")).toHaveValue(before.key);
  await expect(page.locator("#error")).toContainText("Copy your new API key");
  const after = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  expect(after.invoice.id).toBe(before.invoice.id);
  expect(after.invoice.credited).toBe(true);
  await expect(page.locator("#balance-usd")).toHaveText("$10.000800 USD");
  await expect(page.locator("#qr")).toBeHidden();
});

test("model-specific reasoning updates snippets without a global dropdown or payment changes", async ({ page }) => {
  await page.route("**/api/models", (route) => route.fulfill({ json: { data: [
    { id: "deepseek/deepseek-flash", name: "DeepSeek Flash", reasoning: {
      status: "reviewed", field: "reasoning_effort", values: ["low", "high", "max"],
      setup_efforts: ["low", "high", "max"], setup_default: "high", source: "https://api-docs.deepseek.com/guides/thinking_mode/",
    } },
    { id: "other/plain", name: "Plain model", reasoning_effort: true },
  ] } }));
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await page.evaluate(() => sessionStorage.getItem("lightningrouter-usd-session-v1"));
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
  await page.getByLabel("Privacy", {exact: true}).selectOption("any");
  await expect(page.locator("#reasoning-effort")).toHaveCount(0);
  await expect(page.locator("#reasoning-values")).toHaveText("reasoning_effort: low, high, max");
  await expect(page.locator("#config-code")).toContainText('"reasoningEffort": "high"');
  await page.getByRole("tab", { name: "Crush", exact: true }).click();
  await expect(page.locator("#config-code")).toContainText('"reasoning_effort": "high"');
  await page.getByRole("tab", { name: "OMP", exact: true }).click();
  await expect(page.locator("#command-code")).toContainText("--thinking high");
  expect(await page.evaluate(() => sessionStorage.getItem("lightningrouter-usd-session-v1"))).toBe(before);
  await page.selectOption("#model", "other/plain");
  await expect(page.locator("#reasoning-values")).toHaveText("Not yet verified");
  await expect(page.locator("#reasoning-source")).toBeHidden();
  await expect(page.locator("#command-code")).not.toContainText("--thinking");
});

test("Anthropic native effort is not confused with TR's thinking-token budget", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
  await page.getByLabel("Privacy", {exact: true}).selectOption("zdr");
  await expect(page.locator("#model")).toBeEnabled();
  await page.selectOption("#model", "anthropic/claude-opus-4.8");
  await expect(page.locator("#reasoning-values")).toContainText("output_config.effort: low, medium, high, xhigh, max");
  await expect(page.locator("#reasoning-note")).toContainText("native output_config.effort");
  await expect(page.locator("#reasoning-default")).toContainText("reasoning_effort = high");
  await expect(page.locator("#config-code")).toContainText('"reasoningEffort": "high"');
  await expect(page.locator("#reasoning-source")).toHaveAttribute("href", "https://platform.claude.com/docs/en/build-with-claude/effort");
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
    await expect(page.locator("#cowork-setup")).toBeVisible();
    await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
    await expect(page.locator("#config-code")).toContainText("baseURL");
    const bounds = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth }));
    expect(bounds.content).toBeLessThanOrEqual(bounds.width);
    await page.screenshot({ path: `test-results/lightning-${width}.png`, fullPage: true });
  });
}
