import { expect, test } from "./fixtures.mjs";

for (const colorScheme of ["light", "dark"]) {
  test(`mascot and favicon load on every page in ${colorScheme} mode`, async ({ page, request }) => {
    await page.emulateMedia({ colorScheme });
    const asset = await request.get("/assets/lightningrouter-mark.webp");
    expect(asset.status()).toBe(200);
    expect(asset.headers()["content-type"]).toBe("image/webp");
    expect((await asset.body()).length).toBeLessThan(100000);
    for (const path of ["/", "/docs", "/usage", "/pricing", "/terms", "/privacy"]) {
      await page.goto(path);
      const logo = page.locator(".brand-mark");
      await expect(logo).toBeVisible();
      await expect(page.getByRole("link", { name: "LightningRouter", exact: true })).toHaveAttribute("href", "/");
      await expect(page.locator('link[rel="icon"]')).toHaveAttribute("href", "/assets/lightningrouter-mark.webp");
      const alpha = await logo.evaluate(async img => {
        await img.decode();
        const canvas = document.createElement("canvas");
        canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0);
        return [ctx.getImageData(0, 0, 1, 1).data[3], ctx.getImageData(canvas.width / 2, canvas.height / 2, 1, 1).data[3]];
      });
      expect(alpha[0]).toBe(0);
      expect(alpha[1]).toBeGreaterThanOrEqual(250);
      for (const width of [320, 375, 768, 1440]) {
        await page.setViewportSize({ width, height: 900 });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        const box = await logo.boundingBox();
        expect(box.width).toBe(40); expect(box.height).toBe(40);
      }
    }
    await page.setViewportSize({ width: 375, height: 900 });
    await page.screenshot({ path: `test-results/logo-${colorScheme}-mobile.png`, fullPage: true });
  });
}
