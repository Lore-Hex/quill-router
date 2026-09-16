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
