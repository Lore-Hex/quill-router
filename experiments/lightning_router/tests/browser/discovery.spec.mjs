import { expect, test } from "./fixtures.mjs";

test("server-rendered guides and 404 work without JavaScript or payment mutations", async ({ browser, baseURL }) => {
  const context = await browser.newContext({baseURL, javaScriptEnabled: false});
  const page = await context.newPage();
  const writes = [];
  page.on("request", req => { if (req.method() !== "GET") writes.push(req.url()); });
  try {
    await page.goto("/");
    // Playwright text/role selectors omit noscript nodes even with scripting off.
    await expect(page.locator("noscript p")).toBeVisible();
    await expect(page.locator("noscript p")).toContainText("Funding and key lookup need JavaScript.");
    await expect(page.locator("#api-readiness")).not.toContainText("pending");
    await page.locator('noscript a[href="/docs"]').click();
    await expect(page.getByRole("heading", {name: "Docs", exact: true})).toBeVisible();
    await expect(page.getByRole("link", {name: "read this setup guide as Markdown", exact: true})).toHaveAttribute("href", "/docs.md");
    expect((await page.goto("/not-a-page")).status()).toBe(404);
    await expect(page.getByRole("heading", {name: "Page not found", exact: true})).toBeVisible();
    for (const width of [320, 375, 1440]) {
      await page.setViewportSize({width, height: 900});
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({path: `test-results/discovery-404-${width}.png`, fullPage: true});
    }
    expect(writes).toEqual([]);
  } finally { await context.close(); }
});

test("social preview is a decodable public image with matching dimensions", async ({page}) => {
  await page.goto("/docs");
  await expect(page.locator('meta[property="og:title"]')).toHaveAttribute("content", "Docs | LightningRouter");
  await expect(page.locator('meta[property="og:image"]')).toHaveAttribute("content", "https://lightningrouter.ai/assets/lightningrouter-og.jpg");
  const dimensions = await page.evaluate(async () => {
    const image = new Image();
    image.src = "/assets/lightningrouter-og.jpg";
    await image.decode();
    return [image.naturalWidth, image.naturalHeight];
  });
  expect(dimensions).toEqual([1732, 908]);
});
