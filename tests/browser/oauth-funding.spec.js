const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const path = require("node:path");

const pages = JSON.parse(execFileSync("uv", ["run", "--no-sync", "python", "tests/browser/oauth_funding_fixture.py"], {
  cwd: path.resolve(__dirname, "../.."), encoding: "utf8", maxBuffer: 4 * 1024 * 1024,
  env: { ...process.env, PYTHONPATH: "src", TR_ENVIRONMENT: "test", TR_STORAGE_BACKEND: "memory", TR_SENTRY_DSN: "" },
}));

async function show(page, flow, state) {
  await page.route(/\/auth(?:\?.*)?$/, route => route.fulfill({ status: 200, contentType: "text/html", body: pages[`${flow}-${state}`] }));
  await page.goto("/auth");
  await page.evaluate(() => document.fonts.ready);
}

for (const flow of ["legacy", "registered"]) {
  for (const width of [320, 390, 1280]) {
    for (const theme of ["dark", "light"]) {
      test(`${flow} credits ready at ${width}px ${theme}`, async ({ page }) => {
        await page.setViewportSize({ width, height: 844 });
        await page.addInitScript(theme => localStorage.setItem("tr-theme", theme), theme);
        await show(page, flow, "funded");
        const summary = page.locator(".funding-step summary");
        const payment = page.getByRole("button", { name: "Continue to secure card checkout" });
        await expect(summary).toContainText("Step 1 complete");
        await expect(summary).toContainText("$20.00 available");
        await expect(payment).toBeHidden();
        await expect(page.getByText("Final step", { exact: true })).toBeInViewport();
        await expect(page.getByRole("heading", { name: "Choose this app's maximum" })).toBeInViewport();
        const contrast = await page.locator('.approval-form button[type="submit"]').evaluate(button => {
          const luminance = color => {
            const [r, g, b] = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(channel => {
              const value = channel / 255;
              return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
          };
          const style = getComputedStyle(button);
          const foreground = luminance(style.color);
          const background = luminance(style.backgroundColor);
          return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
        });
        expect(contrast).toBeGreaterThanOrEqual(4.5);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        if (width !== 320) {
          await page.screenshot({ path: `/tmp/tr-consent-${flow}-${width}-${theme}.png`, fullPage: true });
        }
        // The completed step remains expandable without JavaScript or a mouse.
        await summary.focus();
        await page.keyboard.press("Enter");
        await expect(payment).toBeVisible();
        await expect(page.getByRole("radio", { name: "$20", exact: true })).toBeChecked();
        await page.keyboard.press("Space");
        await expect(payment).toBeHidden();
        const limit = flow === "legacy" ? page.getByLabel("Maximum spend (USD)") : page.getByRole("radio", { name: "$5/month", exact: true });
        if (flow === "legacy") await limit.fill("7.50");
        else await limit.check();
        await summary.click();
        await summary.click();
        if (flow === "legacy") await expect(limit).toHaveValue("7.50");
        else await expect(limit).toBeChecked();
      });
    }
  }

  test(`${flow} pending checkout offers a balance refresh, then collapses`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await show(page, flow, "pending");
    await expect(page.getByText("Waiting for payment confirmation.", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Continue to secure card checkout" })).toHaveCount(0);
    await page.screenshot({ path: `/tmp/tr-consent-${flow}-pending.png`, fullPage: true });
    await page.route(/\/auth\?consent=/, route => route.fulfill({ status: 200, contentType: "text/html", body: pages[`${flow}-funded`] }));
    await page.getByRole("link", { name: "Check credits", exact: true }).click();
    await expect(page.locator(".funding-step summary")).toContainText("Credits ready");
    await expect(page.locator(".funding-step")).not.toHaveAttribute("open", "");
    await expect(page.getByRole("heading", { name: "Choose this app's maximum" })).toBeInViewport();
  });
}

test("completed funding can be reopened with JavaScript disabled", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.route(/\/auth$/, route => route.fulfill({ status: 200, contentType: "text/html", body: pages["legacy-funded"] }));
  await page.goto("http://127.0.0.1:18081/auth");
  await expect(page.locator(".funding-form")).toBeHidden();
  await page.locator(".funding-step summary").click();
  await expect(page.locator(".funding-form")).toBeVisible();
  await context.close();
});
