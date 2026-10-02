const { test, expect } = require("@playwright/test");

test("new homepage searches models and opens the existing sign-in dialog", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("The AI router that shows its work.");
  const search = page.locator("#models [data-open-search]");
  await search.click();
  const dialog = page.getByRole("dialog", { name: "Find a model" });
  await expect(dialog).toBeVisible();
  await expect(page.locator("#search-results a").first()).toBeVisible();
  await page.getByRole("searchbox").fill("qwen");
  await expect(page.locator("#search-results a").first()).toContainText(/qwen/i);
  await page.getByRole("searchbox").press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(search).toBeFocused();
  await page.locator("#top .button-primary").click();
  const signin = page.locator("#signinModal");
  await expect(signin).toBeVisible();
  await expect(signin.getByRole("link", { name: "Continue with Google" })).toBeVisible();
  await expect(signin.getByRole("link", { name: "Continue with Google" })).toHaveAttribute("href", "/auth/google/login");
  await expect(signin.getByRole("link", { name: "Continue with GitHub" })).toBeVisible();
  await expect(signin.getByRole("link", { name: "Continue with GitHub" })).toHaveAttribute("href", "/auth/github/login");
  await expect(signin.getByRole("button", { name: "Continue with MetaMask" })).toBeVisible();
});

test("responsive navigation and customer disclosure remain usable", async ({ page, isMobile }) => {
  await page.goto("/");
  const customer = page.locator(".customer-details");
  if (isMobile) {
    await expect(customer).not.toHaveAttribute("open", "");
    await customer.locator("summary").click();
    await expect(customer).toHaveAttribute("open", "");
    await page.getByRole("button", { name: "Menu", exact: true }).click();
    await expect(page.locator("#homepage-nav")).toBeVisible();
    await page.getByRole("button", { name: "Menu", exact: true }).click();
    await expect(page.locator("#homepage-nav")).not.toBeVisible();
    await page.locator(".trnav .search").click();
    await expect(page.getByRole("dialog", { name: "Find a model" })).toBeVisible();
    await page.getByRole("searchbox").press("Escape");
  } else {
    await expect(customer).toHaveAttribute("open", "");
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("link", { name: "Back to top of page" }).click();
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
});

test("returning-user hint restores console labels without replacing authentication", async ({ page, context, baseURL }) => {
  await context.addCookies([{ name: "tr_signed_in", value: "1", url: baseURL }]);
  await page.goto("/");
  await expect(page.locator(".trnav .signin")).toHaveText("Console");
  await expect(page.locator("#top .button-primary")).toHaveText("Open console");
  await expect(page.locator("#top .button-primary")).toHaveAttribute("href", "/console/api-keys");
  await expect(page.locator("#signinModal")).not.toBeVisible();
});
