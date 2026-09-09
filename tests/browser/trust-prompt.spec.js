const { test, expect } = require("@playwright/test");

test("copies exactly the visible prompt under the real page CSP", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: async (text) => { window.copiedTrustPrompt = text; } },
    });
  });
  await page.goto("/trust");
  await page.getByRole("button", { name: "Copy prompt" }).click();
  const prompt = (await page.locator("#trust-agent-prompt").textContent()).trim();
  expect(await page.evaluate(() => window.copiedTrustPrompt)).toBe(prompt);
  await expect(page.getByRole("status")).toHaveText("Copied. Ready for your agent chat.");
  await expect(page.getByRole("button", { name: "Copy prompt" })).toBeEnabled();
});

for (const mode of ["denied", "unavailable"]) {
  test(`selects the prompt when clipboard access is ${mode}`, async ({ page }) => {
    await page.addInitScript((mode) => {
      Object.defineProperty(navigator, "clipboard", {
        value: mode === "unavailable" ? undefined : {
          writeText: async () => { throw new DOMException("Denied", "NotAllowedError"); },
        },
      });
    }, mode);
    await page.goto("/trust");
    await page.getByRole("button", { name: "Copy prompt" }).click();
    const prompt = (await page.locator("#trust-agent-prompt").textContent()).trim();
    expect(await page.evaluate(() => window.getSelection().toString())).toBe(prompt);
    await expect(page.getByRole("status")).toContainText("Prompt selected");
    await expect(page.getByRole("button", { name: "Copy prompt" })).toBeEnabled();
  });
}

test("the prompt remains readable without JavaScript", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto("/trust");
  await expect(page.locator("#trust-agent-prompt")).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy prompt" })).toBeHidden();
  await context.close();
});

for (const width of [375, 1440]) {
  test(`prompt fits the first viewport at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/trust");
    const prompt = await page.locator(".agent-verify").boundingBox();
    expect(prompt.y + prompt.height).toBeLessThan(900);
    const heading = await page.locator("#agent-verify-title").boundingBox();
    const button = await page.getByRole("button", { name: "Copy prompt" }).boundingBox();
    expect(button.height).toBeGreaterThanOrEqual(44);
    expect(button.x >= heading.x + heading.width || button.y >= heading.y + heading.height).toBeTruthy();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await expect(page.locator(".copy-prompt img")).toHaveJSProperty("naturalWidth", 24);
    await page.screenshot({ path: testInfo.outputPath("trust-prompt.png") });
  });
}
