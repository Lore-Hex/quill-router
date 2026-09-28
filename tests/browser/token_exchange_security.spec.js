const { test, expect } = require("@playwright/test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");

const archivePath = path.resolve(__dirname, "../../src/trusted_router/data/enterprise/TrustedRouter-Security-Pack.zip");

for (const [name, width, height, theme] of [
  ["desktop", 1440, 1000, "dark"],
  ["mobile", 390, 844, "dark"],
  ["narrow", 320, 740, "dark"],
  ["light", 1440, 1000, "light"],
]) {
  test(`security resources render and download on ${name}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height });
    await page.addInitScript(value => localStorage.setItem("tr-theme", value), theme);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    let submissions = 0;
    const archive = await readFile(archivePath);
    await page.route("**/token-exchange/brief", async route => {
      submissions += 1;
      expect(route.request().method()).toBe("POST");
      expect(route.request().postDataJSON()).toEqual({ email: "ada@example.com", website: "", resource: "security" });
      await route.fulfill({ status: 200, contentType: "application/zip", body: archive });
    });
    await page.goto("/token-exchange/security");
    await page.evaluate(() => document.fonts.ready);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Token Exchangesecurity resources.");
    await page.locator(".ts-documents").scrollIntoViewIfNeeded();
    await expect.poll(() => page.locator(".ts-cover img").evaluateAll(images => images.every(img => img.complete && img.naturalWidth > 0))).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const overflowing = await page.locator("main h1, main h2, main p, main button").evaluateAll(elements => elements.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.textContent));
    expect(overflowing).toEqual([]);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
    await page.screenshot({ path: testInfo.outputPath(`security-${name}.png`), fullPage: true });
    await page.screenshot({ path: testInfo.outputPath(`security-${name}-viewport.png`) });
    await page.locator("#brief-email").fill("invalid");
    await page.getByRole("button", { name: "Download both PDFs" }).click();
    expect(submissions).toBe(0);
    await page.locator("#brief-email").fill("ada@example.com");
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download both PDFs" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("TrustedRouter-Security-Pack.zip");
    expect(await readFile(await download.path())).toEqual(archive);
    await expect(page.locator("#brief-status")).toContainText("ZIP contains both PDFs");
    await expect(page.getByRole("button", { name: "Download both PDFs" })).toBeEnabled();
    expect(submissions).toBe(1);
    expect(errors).toEqual([]);
  });
}

for (const status of [422, 429, 503, 200]) {
  test(`security download handles ${status} JSON without releasing a file`, async ({ page }) => {
    let downloads = 0;
    page.on("download", () => downloads += 1);
    await page.route("**/token-exchange/brief", route => route.fulfill({ status, contentType: "application/json", body: '{"ok":false}' }));
    await page.goto("/token-exchange/security");
    await page.locator("#brief-email").fill("ada@example.com");
    await page.getByRole("button", { name: "Download both PDFs" }).click();
    await expect(page.locator("#brief-status")).toHaveAttribute("data-state", "error");
    await expect(page.getByRole("button", { name: "Download both PDFs" })).toBeEnabled();
    expect(downloads).toBe(0);
    await page.route("**/token-exchange/brief", route => route.fulfill({ status: 200, contentType: "application/zip", path: archivePath }));
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download both PDFs" }).click();
    await downloadPromise;
    await expect(page.locator("#brief-status")).toHaveAttribute("data-state", "success");
  });
}

test("brochure download retains its original filename and request type", async ({ page }) => {
  await page.route("**/token-exchange/brief", route => {
    expect(route.request().postDataJSON().resource).toBe("brochure");
    return route.fulfill({ status: 200, contentType: "application/pdf", path: archivePath.replace("Security-Pack.zip", "Token-Exchange-Brochure.pdf") });
  });
  await page.goto("/token-exchange");
  await page.locator("#brief-email").fill("ada@example.com");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download the brochure" }).click();
  expect((await downloadPromise).suggestedFilename()).toBe("TrustedRouter-Token-Exchange-Brochure.pdf");
});
