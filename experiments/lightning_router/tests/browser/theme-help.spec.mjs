import { expect, test } from "./fixtures.mjs";

test("theme persists across pages and reloads; mobile controls fit", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/");
  await page.getByRole("button", { name: "Switch to dark mode" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("#qr")).toBeVisible();
  expect(await page.locator("#qr").evaluate(el => getComputedStyle(el).backgroundColor)).toBe("rgb(255, 255, 255)");
  for (const path of ["/docs", "/usage", "/pricing", "/terms", "/privacy", "/"]) {
    await page.goto(path);
    await expect(page.getByRole("button", { name: "Switch to light mode" })).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: "test-results/dark-mobile.png", fullPage: true });
});

test("help requires a funded key, required email, private message, and handles delivery failure", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.getByRole("button", { name: "Help", exact: true })).toBeHidden();
  const existing = await (await request.post("/_test/existing", { data: {} })).json();
  await page.locator("#existing-key").fill(existing.key);
  await page.getByRole("button", { name: "Use API key", exact: true }).click();
  await page.getByRole("button", { name: "Help", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByLabel("Your email")).toHaveAttribute("required", "");
  await page.getByLabel("Your email").fill("customer@example.com");
  await page.getByLabel("Message", { exact: true }).fill(existing.key);
  await page.getByRole("button", { name: "Send feedback" }).click();
  await expect(page.locator("dialog [role=alert]")).toContainText("Remove API keys");
  await page.getByLabel("Message", { exact: true }).fill("Please help with my invoice.");
  await page.route("**/api/feedback", route => route.fulfill({ status: 503, json: { error: "temporarily_unavailable" } }));
  await page.getByRole("button", { name: "Send feedback" }).click();
  await expect(page.locator("dialog [role=alert]")).toContainText("could not be delivered");
  await expect(page.getByLabel("Message", { exact: true })).toHaveValue("Please help with my invoice.");
  await page.unroute("**/api/feedback");
  const sent = page.waitForRequest("**/api/feedback");
  await page.getByRole("button", { name: "Send feedback" }).click();
  expect((await sent).postDataJSON()).toEqual({ email: "customer@example.com", message: "Please help with my invoice." });
  await expect(page.locator("dialog [role=status]")).toContainText("Feedback sent");
  await page.setViewportSize({ width: 320, height: 700 });
  await page.screenshot({ path: "test-results/help-mobile.png", fullPage: true });
  await page.getByRole("button", { name: "Close help" }).click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("button", { name: "Help", exact: true })).toBeHidden();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("reload replaces a confirmed canceled invoice once without a new account", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  const canceled = await request.post(`/api/invoices/${before.invoice.id}/cancel`, { headers: { Authorization: "Bearer " + before.key }, data: {} });
  expect((await canceled.json()).state).toBe("CANCELED");
  let creates = 0;
  page.on("request", req => { if (req.url().endsWith("/api/invoices") && req.method() === "POST") creates++; });
  await page.reload();
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#invoice-state")).toHaveText("Waiting for payment");
  const after = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  expect(after.key).toBe(before.key);
  expect(after.invoice.id).not.toBe(before.invoice.id);
  expect(after.invoice.account_created).toBe(false);
  expect(creates).toBe(1);
  await page.reload();
  await expect(page.locator("#qr")).toBeVisible();
  expect(creates).toBe(1);
});

for (const state of ["ACCEPTED", "SETTLED", "review", "offline"]) {
  test(`reload preserves ${state} invoice and does not replace it`, async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("#qr")).toBeVisible();
    const before = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
    await page.route("**/api/invoices/*/refresh", route => state === "offline" ? route.abort() : route.fulfill({
      json: { ...before.invoice, state: state === "review" ? "CANCELED" : state, credited: false, attention_required: state === "review" },
    }));
    let creates = 0;
    page.on("request", req => { if (req.url().endsWith("/api/invoices") && req.method() === "POST") creates++; });
    await page.reload();
    await expect(page.locator("#qr-message")).not.toHaveText("Loading payment status");
    await expect(page.locator("#qr")).toBeHidden();
    expect(creates).toBe(0);
    const after = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
    expect(after.key).toBe(before.key);
    expect(after.requestId).toBe(before.requestId);
  });
}
