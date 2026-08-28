const { expect, test } = require("@playwright/test");

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
  await expect(policy.getByText("Show less", { exact: true })).toBeVisible();
});
