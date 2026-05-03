const { expect, test } = require("@playwright/test");

test("homepage opens sign-in modal and handles missing MetaMask", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: /One API\. Every LLM\./ })).toBeVisible();
  await expect(page.locator(".proof-card .mono", { hasText: "trustedrouter/auto" })).toBeVisible();

  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("#signinModal")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();

  await page.getByRole("button", { name: /MetaMask/ }).click();
  await expect(page.locator("#signinError")).toContainText("MetaMask is not installed");
});

test("console redirects unauthenticated users and auto-opens sign-in", async ({ page }) => {
  await page.goto("/console/api-keys");

  await expect(page).toHaveURL(/reason=signin/);
  await expect(page.locator("#signinModal")).toBeVisible();
});

test("homepage and console redirect are usable on mobile width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  await expect(page.getByRole("heading", { name: /One API\. Every LLM\./ })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(2);
});

test("homepage exposes pricing, stablecoin, open-source, and trust claims", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByText("$0.01 less per 1M tokens")).toBeVisible();
  await expect(page.getByText("$25 USDC")).toBeVisible();
  await expect(page.getByText("Hosted OSS")).toBeVisible();
  await expect(page.locator(".panel-body .mono", { hasText: "api.quillrouter.com" })).toBeVisible();
  await expect(page.getByRole("link", { name: "trusted-router-py" })).toHaveAttribute(
    "href",
    "https://github.com/Lore-Hex/trusted-router-py",
  );
  await expect(page.getByRole("link", { name: "trusted-router-js" })).toHaveAttribute(
    "href",
    "https://github.com/Lore-Hex/trusted-router-js",
  );
});

test("local trust page links the public source repositories and release files", async ({ page }) => {
  await page.goto("/trust");

  await expect(page.getByRole("paragraph").filter({ hasText: "api.quillrouter.com is the prompt path" })).toBeVisible();
  for (const repo of [
    "Lore-Hex/quill-router",
    "Lore-Hex/quill-cloud-proxy",
    "Lore-Hex/quill-cloud-infra",
    "Lore-Hex/quill",
    "Lore-Hex/trusted-router-py",
    "Lore-Hex/trusted-router-js",
  ]) {
    await expect(page.getByRole("link", { name: repo }).first()).toBeVisible();
  }
  await expect(page.getByRole("link", { name: "gcp-release.json" }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "image-digest-gcp.txt" })).toBeVisible();
});
