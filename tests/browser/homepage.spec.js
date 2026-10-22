const { test, expect } = require("@playwright/test");
const { devices } = require("@playwright/test");

// The default project is Desktop Chrome. The same scenarios run again as an
// iPhone 13 (Chromium with the phone's viewport, touch and user agent).
const phone = { ...devices["iPhone 13"] };
delete phone.defaultBrowserType;

const scenarios = () => {

  test("exchange banner is visible, fits and records the outbound click", async ({ page }, testInfo) => {
    await page.goto("/?utm_source=homepage-browser-test");
    const banner = page.locator('[data-token-exchange-banner]');
    await expect(banner).toBeVisible();
    expect(await banner.boundingBox()).toMatchObject({x:0});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path:testInfo.outputPath('exchange-banner.png')});
    await page.route('https://nytokenexchange.com/**', route => route.fulfill({body:'Exchange destination'}));
    const event = page.waitForRequest(request => request.url().endsWith('/analytics/events')
      && request.postDataJSON()?.event === 'home.token_exchange_clicked');
    await banner.click();
    expect((await event).postDataJSON()).toEqual({event:'home.token_exchange_clicked'});
    await expect(page).toHaveURL(/nytokenexchange\.com\/\?utm_source=trustedrouter.com/);
  });

  test("new homepage searches models and opens the existing sign-in dialog", async ({ page }, testInfo) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Encrypt all your AI chats");
    const browse = page.getByRole("link", { name: "Browse all models", exact: true });
    await expect(browse).toHaveAttribute("href", "/models");
    await browse.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("browse-all-models.png") });
    await browse.click();
    await expect(page).toHaveURL(/\/models$/);
    await page.goBack();
    const search = page.locator(".trnav [data-open-search]");
    await search.click();
    const dialog = page.getByRole("dialog", { name: "Find a model" });
    await expect(dialog).toBeVisible();
    await expect(page.locator("#search-results a").first()).toBeVisible();
    await page.getByRole("searchbox").fill("qwen");
    await expect(page.locator("#search-results a").first()).toContainText(/qwen/i);
    await page.getByRole("searchbox").press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(search).toBeFocused();
    await page.locator("#top .hero .button-primary").click();
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
      await expect(page.locator(".trnav .signin")).toBeVisible();
      await expect(page.locator(".trnav .search span[aria-hidden='true']")).toHaveText("Search models", { useInnerText: true });
      await expect(page.locator(".trnav .search")).toHaveAccessibleName(/^Search \d+ models/);
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
    await expect(page.locator("#top .hero .button-primary")).toHaveText("Get my API key");
    await expect(page.locator("#top .cta-note")).toBeHidden();
    await expect(page.locator("#top .hero .button-primary")).toHaveAttribute("href", "/console/api-keys");
    await expect(page.locator("#signinModal")).not.toBeVisible();
  });
};

scenarios();

test.describe("iPhone 13", () => {
  test.use(phone);
  scenarios();
});

test.describe("smallest phones", () => {
  test.use({ viewport: { width: 320, height: 700 } });
  test("header keeps Sign in and a short search label at 320px", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".trnav .signin")).toBeVisible();
    await expect(page.locator(".trnav .search span[aria-hidden='true']")).toHaveText(/^models$/i, { useInnerText: true }); // capitalised by CSS
    await expect(page.locator(".trnav .search")).toHaveAccessibleName(/^Search \d+ models/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
});
