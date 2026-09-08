const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const path = require("node:path");

const html = execFileSync("uv", ["run", "--no-sync", "python", "tests/browser/leaderboard_fixture.py"], {
  cwd: path.resolve(__dirname, "../.."), encoding: "utf8", maxBuffer: 4 * 1024 * 1024,
  env: { ...process.env, PYTHONPATH: "src", TR_ENVIRONMENT: "test", TR_STORAGE_BACKEND: "memory", TR_SENTRY_DSN: "" },
});

test.beforeEach(async ({ page }) => {
  await page.route(/\/leaderboard(?:\?.*)?$/, route => route.fulfill({ status: 200, contentType: "text/html", body: html }));
});

test("provider/model tabs, search, evidence, paging and URL persistence", async ({ page }) => {
  await page.goto("/leaderboard");
  await expect(page.getByRole("tab", { name: "Providers", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Models", exact: true }).click();
  await expect(page.locator("[data-lb-count]")).toHaveText("1-25 of 62 models");
  await expect(page.locator("#lb-models [data-lb-row]:visible")).toHaveCount(25);
  await page.getByRole("button", { name: "Next page" }).click();
  await expect(page.locator("[data-lb-count]")).toHaveText("26-50 of 62 models");
  await page.locator("[data-lb-provider]").selectOption("mistral");
  await page.locator("[data-lb-search]").fill("model-0");
  await expect(page.locator("#lb-models [data-lb-row]:visible")).toHaveCount(3);
  await page.reload();
  await expect(page.locator("[data-lb-search]")).toHaveValue("model-0");
  await expect(page.locator("#lb-models [data-lb-row]:visible")).toHaveCount(3);
  await page.locator("[data-lb-search]").fill("");
  await page.locator("[data-lb-evidence]").selectOption("config");
  await expect(page.locator("#lb-models [data-lb-row]:visible")).toHaveCount(1);
  await expect(page.locator("#lb-models [data-lb-row]:visible")).toContainText("needs-configuration");
  await page.locator("#lb-models [data-lb-row]:visible summary").click();
  await expect(page.locator("#lb-models [data-lb-row]:visible dl")).toContainText("probe_config_error: 1");
  await expect(page.getByRole("link", { name: "7-day evidence", exact: true })).toHaveAttribute("href", /window=7d/);
  await expect(page.getByRole("link", { name: "7-day evidence", exact: true })).toHaveAttribute("href", /evidence=config/);
});

test("numeric sorting keeps absent measurements last and filters unranked routes", async ({ page }) => {
  await page.goto("/leaderboard?view=models&sort=ttft");
  await expect(page.locator("#lb-models [data-lb-row]:visible").first()).toContainText("model-00");
  await page.locator("[data-lb-evidence]").selectOption("qualified");
  await expect(page.locator("[data-lb-count]")).toHaveText("1-25 of 60 models");
  await page.locator("[data-lb-evidence]").selectOption("warming");
  await expect(page.locator("#lb-models [data-lb-row]:visible").first()).toContainText("model-60");
  await expect(page.locator("#lb-models [data-lb-row]:visible").last()).toContainText("needs-configuration");
  await page.locator("[data-lb-search]").fill("no-such-model");
  await expect(page.locator("#lb-models [data-lb-empty]")).toBeVisible();
  await expect(page.getByRole("button", { name: "Next page" })).toBeDisabled();
  await page.getByRole("tab", { name: "Models", exact: true }).focus();
  await page.keyboard.press("ArrowLeft");
  await expect(page.getByRole("tab", { name: "Providers", exact: true })).toBeFocused();
});

for (const width of [390, 1280]) {
  test(`fits ${width}px with details and no document overflow`, async ({ page }) => {
    await page.setViewportSize({ width, height: 950 });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("/leaderboard?view=models");
    await page.locator("#lb-models [data-lb-row]:visible summary").first().click();
    await expect(page.locator("#lb-models details[open] dl")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator("#lb-models img").first().evaluate(img => img.complete && img.naturalWidth > 0)).toBe(true);
    expect(errors).toEqual([]);
    await page.screenshot({ path: `/tmp/tr-leaderboard-improved-${width}.png`, fullPage: false });
  });
}

test("all evidence remains readable without JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.route(/\/leaderboard$/, route => route.fulfill({ status: 200, contentType: "text/html", body: html }));
  await page.goto("http://127.0.0.1:18081/leaderboard");
  await expect(page.locator("#lb-models [data-lb-row]")).toHaveCount(62);
  await expect(page.locator("#lb-providers")).toBeVisible();
  await expect(page.locator("#lb-models")).toBeVisible();
  await context.close();
});
