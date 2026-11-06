const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const path = require("node:path");

const pages = JSON.parse(execFileSync("uv", ["run", "--no-sync", "python", "tests/browser/account_closure_fixture.py"], {
  cwd: path.resolve(__dirname, "../.."), encoding: "utf8", maxBuffer: 4 * 1024 * 1024,
  env: { ...process.env, PYTHONPATH: "src", TR_ENVIRONMENT: "test", TR_STORAGE_BACKEND: "memory", TR_SENTRY_DSN: "" },
}));

for (const width of [320, 390, 1280]) {
  test(`account closure confirmation and receipt at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.route("**/console/account/delete", route => route.fulfill({
      contentType: "text/html", body: route.request().method() === "POST" ? pages.final : pages.confirmation,
    }));
    await page.route("**/console/account/delete/confirm", route => route.fulfill({
      contentType: "text/html", body: pages.closed,
    }));
    await page.goto("/console/account/delete");
    await page.evaluate(() => document.fonts.ready);
    await expect(page.getByRole("heading", { name: "Delete account", exact: true })).toBeVisible();
    await expect(page.getByText("This closes access; it does not erase your personal data.", { exact: false })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `/tmp/tr-account-close-${width}.png`, fullPage: true });
    const input = page.getByLabel("Type DELETE to confirm");
    const submit = page.getByRole("button", { name: "Review account closure" });
    await submit.click();
    await expect(input).toBeFocused();
    await input.fill("DELETE");
    await page.getByRole("checkbox").check();
    await submit.click();
    await expect(page.getByRole("heading", { name: "Final confirmation", exact: true })).toBeVisible();
    await expect(page.getByText("Your account is still active.")).toBeVisible();
    await expect(page.getByRole("link", { name: "Keep my account" })).toHaveAttribute("href", "/console/account/preferences");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `/tmp/tr-account-final-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: "Yes, close my account" }).click();
    await expect(page.getByRole("heading", { name: "Account closed", exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `/tmp/tr-account-closed-${width}.png`, fullPage: true });
  });
}
