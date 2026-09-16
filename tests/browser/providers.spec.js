const { expect, test } = require("@playwright/test");

test("provider privacy separates retention from verified inference", async ({ page }) => {
  await page.goto("/providers");

  await expect(page.locator('[data-provider-id="trustedrouter"]')).toHaveCount(0);
  const phala = page.locator('[data-provider-id="phala"]');
  await expect(phala.locator(".provider-card-trust")).toContainText("ZDR");
  await expect(phala.locator(".provider-card-facts")).toContainText("Not verified");
  await expect(phala.getByText("Confidential compute", { exact: true })).toHaveCount(0);
  await expect(page.locator('[data-provider-id="tinfoil"] .provider-card-facts')).toContainText("Verified");
  const search = page.getByRole("searchbox", { name: "Search providers" });
  await search.fill("phala");
  await expect(phala).toBeVisible();
  await expect(page.locator("[data-provider-result-count]")).toHaveText("1 entry");
  await phala.getByRole("link", { name: /Phala/ }).click();
  await expect(page.getByRole("row", { name: "Verified confidential inference Not verified" })).toBeVisible();
});

test("provider catalog searches and expands policy notes", async ({ page }) => {
  await page.goto("/providers");

  const search = page.getByRole("searchbox", { name: "Search providers" });
  const tinfoil = page.locator('[data-provider-id="tinfoil"]');
  const anthropic = page.locator('[data-provider-id="anthropic"]');

  await expect(search).toBeVisible();
  await search.fill("tinfoil");
  await expect(tinfoil).toBeVisible();
  await expect(anthropic).toBeHidden();
  await expect(page.locator("[data-provider-result-count]")).toHaveText("1 entry");
  await expect(page).toHaveURL(/\?q=tinfoil$/);

  const policy = tinfoil.locator(".provider-policy-details");
  await expect(policy).not.toHaveAttribute("open", "");
  await expect(policy.locator(".provider-policy-preview")).toBeVisible();
  await policy.locator("summary").click();
  await expect(policy).toHaveAttribute("open", "");
  await expect(policy.locator(".provider-policy-full")).toBeVisible();
  await expect(policy.getByText("Hide policy", { exact: true })).toBeVisible();
});

test("provider catalog fits a mobile viewport without horizontal scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/providers");

  const dimensions = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));

  expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);
  await expect(page.locator(".provider-catalog-grid")).toHaveCSS("grid-template-columns", "320px");
});

test("privacy filters match verified flags, preserve search, and survive reload", async ({ page }) => {
  await page.goto("/providers?privacy=confidential");
  const privacy = page.getByRole("combobox", { name: "Filter providers by privacy" });
  await expect(privacy).toHaveValue("confidential");
  const tinfoil = page.locator('[data-provider-id="tinfoil"]');
  await expect(tinfoil).toBeVisible();
  await expect(tinfoil.locator('.provider-card-trust [data-privacy="confidential"]')).toBeVisible();
  await expect(tinfoil.locator('.provider-card-trust [data-privacy="zdr"]')).toBeVisible();
  await expect(page.locator('[data-confidential="false"]:visible')).toHaveCount(0);
  await page.getByRole("searchbox", { name: "Search providers" }).fill("phala");
  await expect(page.locator("[data-provider-empty]")).toBeVisible();
  await privacy.selectOption("zdr");
  await expect(page.locator('[data-provider-id="phala"]')).toBeVisible();
  await expect(page.locator('[data-zdr="false"]:visible')).toHaveCount(0);
  await page.reload();
  await expect(privacy).toHaveValue("zdr");
  await expect(page.getByRole("searchbox", { name: "Search providers" })).toHaveValue("phala");
  await expect(page.locator("[data-provider-result-count]")).toHaveText("1 entry");
});

test("privacy badges have readable contrast in both themes", async ({ page }) => {
  for (const path of ["/providers", "/models?q=glm-5.3-flash"]) {
    await page.goto(path);
    for (const theme of ["dark", "light"]) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      const ratios = await page.locator(".privacy-badge:visible").evaluateAll(badges => {
        const context = document.createElement("canvas").getContext("2d");
        const rgba = color => {
          context.clearRect(0, 0, 1, 1);
          context.fillStyle = color;
          context.fillRect(0, 0, 1, 1);
          return Array.from(context.getImageData(0, 0, 1, 1).data);
        };
        const luminance = rgb => rgb.slice(0, 3).map(value => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
        return badges.map(badge => {
          const ancestors = [];
          for (let node = badge; node; node = node.parentElement) ancestors.unshift(node);
          let background = [255, 255, 255];
          for (const node of ancestors) {
            const color = rgba(getComputedStyle(node).backgroundColor);
            const alpha = color[3] / 255;
            background = background.map((value, index) => alpha * color[index] + (1 - alpha) * value);
          }
          const foreground = luminance(rgba(getComputedStyle(badge).color));
          const behind = luminance(background);
          return (Math.max(foreground, behind) + 0.05) / (Math.min(foreground, behind) + 0.05);
        });
      });
      expect(ratios.length).toBeGreaterThan(0);
      expect(Math.min(...ratios)).toBeGreaterThanOrEqual(4.5);
    }
  }
});
