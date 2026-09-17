import { expect, test } from "./fixtures.mjs";

for (const theme of ["light", "dark"]) {
  for (const width of [320, 375, 390, 580, 768, 1440]) {
    test(`setup tabs have padded highlights and fit ${width}px in ${theme} mode`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/");
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      const tabs = page.getByRole("tablist", { name: "Agent" });
      await expect(tabs.getByRole("tab")).toHaveText(["Trusted Cowork", "OpenCode", "Crush", "OMP"]);
      for (const tab of await tabs.getByRole("tab").all()) {
        await expect(tab).toHaveCSS("padding-left", "16px");
        await expect(tab).toHaveCSS("padding-right", "16px");
        await tab.click();
        await expect(tab).toHaveAttribute("aria-selected", "true");
        await tab.hover();
        const geometry = await tab.evaluate(el => {
          const rect = el.getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(el);
          const label = range.getBoundingClientRect();
          return { height: rect.height, left: label.left - rect.left, right: rect.right - label.right };
        });
        expect(geometry.height).toBeGreaterThanOrEqual(46);
        expect(geometry.left).toBeGreaterThanOrEqual(15);
        expect(geometry.right).toBeGreaterThanOrEqual(15);
      }
      await page.getByRole("tab", { name: "OpenCode", exact: true }).click();
      await page.getByRole("tab", { name: "Crush", exact: true }).hover();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const bounds = await tabs.getByRole("tab").evaluateAll(elements => elements.map(el => {
        const { left, right, top, bottom } = el.getBoundingClientRect();
        return { left, right, top, bottom };
      }));
      for (let i = 0; i < bounds.length; i++) {
        expect(bounds[i].left).toBeGreaterThanOrEqual(0);
        expect(bounds[i].right).toBeLessThanOrEqual(width);
        for (const other of bounds.slice(i + 1)) {
          expect(bounds[i].right <= other.left || bounds[i].bottom <= other.top).toBe(true);
        }
      }
      await tabs.screenshot({ path: `test-results/tabs-${theme}-${width}.png` });
    });
  }
}
