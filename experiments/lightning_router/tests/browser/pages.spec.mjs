import { expect, test } from "./fixtures.mjs";

test("marketing and documented DeepSeek budgets", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "No account needed." })).toBeVisible();
  await expect(page.locator(".intro")).toContainText("Pay with BTC over Lightning ⚡ and you're good to go!");
  await page.getByRole("tab", {name: "Crush", exact: true}).click();
  await expect(page.locator("#model-limits")).toContainText("393,216 max output");
  await expect(page.locator("#config-code")).toContainText('"default_max_tokens": 65536');
});

test("default selects V4.1 Flash instead of an alphabetically earlier retired Flash", async ({page}) => {
  await page.route("**/api/models", route => route.fulfill({json: {data: [
    {id: "deepseek/deepseek-v4-flash-0731-fast", name: "DeepSeek V4 Flash Fast"},
    {id: "deepseek/deepseek-flash", name: "DeepSeek Flash (rolling)"},
    {id: "deepseek/deepseek-v4.1-flash", name: "DeepSeek V4.1 Flash", context: 1048576, output: 393216, default_output: 65536},
  ]}}));
  await page.goto("/");
  await expect(page.locator("#model")).toHaveValue("deepseek/deepseek-v4.1-flash");
  await page.getByRole("tab", {name: "Crush", exact: true}).click();
  await expect(page.locator("#config-code")).toContainText('"default_max_tokens": 65536');
});

test("existing key balance survives an invoice creation outage", async ({ page, request }) => {
  const existing = await (await request.post("/_test/existing", {data: {}})).json();
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await page.route("**/api/invoices", route => route.fulfill({status: 503, json: {error: "payments_not_ready"}}));
  await page.getByLabel("Existing API key").fill(existing.key);
  await page.getByRole("button", {name: "Use API key", exact: true}).click();
  await expect(page.locator("#balance-usd")).toContainText("$20.00");
  await expect(page.locator("#error")).toContainText("not ready");
  await page.getByRole("link", {name: "View usage", exact: true}).click();
  await expect(page.locator("#usage-balance")).toContainText("$20.00");
  await expect(page.locator("#usage-total")).toHaveText("$1.234567");
  await expect(page.locator("#page-error")).toBeEmpty();
});

test("usage reads create no invoices and never put the key in a URL", async ({ page, request }) => {
  const existing = await (await request.post("/_test/existing", {data: {}})).json();
  const requests = [];
  page.on("request", req => requests.push({url: req.url(), method: req.method()}));
  await page.goto("/usage");
  await expect(page.locator("#usage-data")).toBeHidden();
  await page.getByLabel("API key", {exact: true}).fill(existing.key);
  await page.getByRole("button", {name: "View usage", exact: true}).click();
  await expect(page.locator("#usage-total")).toHaveText("$1.234567");
  await expect(page.locator("#usage-limit")).toHaveText("No key limit");
  await page.getByRole("button", {name: "Refresh usage"}).click();
  await expect(page.locator("#usage-status")).toContainText("Updated");
  expect(requests.every(req => req.method === "GET" && !req.url.includes(existing.key))).toBe(true);
  expect(requests.some(req => req.url.includes("/api/invoices"))).toBe(false);
});

test("usage failures never fabricate zero spend or hide a verified balance", async ({ page, request }) => {
  const existing = await (await request.post("/_test/existing", {data: {}})).json();
  await page.route("**/api/usage", route => route.fulfill({status: 503, json: {error: "failed"}}));
  await page.goto("/usage");
  await page.getByLabel("API key", {exact: true}).fill(existing.key);
  await page.getByRole("button", {name: "View usage", exact: true}).click();
  await expect(page.locator("#page-error")).toContainText("Temporarily unavailable");
  await expect(page.locator("#usage-balance")).toContainText("$20.00");
  await expect(page.locator("#usage-total")).toHaveText("Unavailable");
});

test("pricing is exact, searchable, and public pages never create invoices", async ({ page }) => {
  const mutations = [];
  page.on("request", req => { if (req.method() !== "GET") mutations.push(req.url()); });
  await page.goto("/pricing");
  await expect(page.locator("#price-rows")).toContainText("$0.0422");
  await expect(page.locator("#price-rows")).toContainText("$0.0844");
  await page.getByLabel("Find a model").fill("kimi");
  await expect(page.locator("#price-rows tr")).toHaveCount(1);
  await expect(page.locator("#price-rows")).toContainText("Unavailable");
  await page.getByLabel("Find a model").fill("not-a-model");
  await expect(page.locator("#price-status")).toHaveText("No matching models");
  await page.getByRole("link", {name: "Terms of Service", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Terms of Service", exact: true})).toBeVisible();
  await page.locator("footer").getByRole("link", {name: "Privacy Policy", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Privacy Policy", exact: true})).toBeVisible();
  expect(mutations).toEqual([]);
});

for (const width of [320, 375, 768, 1440]) {
  test(`public pages fit ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 950});
    for (const path of ["/", "/usage", "/pricing", "/docs", "/terms", "/privacy"]) {
      await page.goto(path);
      if (path === "/") await expect(page.locator("#qr")).toBeVisible();
      if (path === "/pricing") await expect(page.locator("#price-rows tr")).toHaveCount(3);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({path: `test-results/pages-${path.slice(1) || "home"}-${width}.png`, fullPage: true});
    }
  });
}
