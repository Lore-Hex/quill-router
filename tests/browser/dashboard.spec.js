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

test("wallet sign-in completes without email gate", async ({ page }) => {
  const address = "0x1111111111111111111111111111111111111111";
  let emailRequests = 0;

  await page.addInitScript((walletAddress) => {
    window.ethereum = {
      request: async ({ method }) => {
        if (method === "eth_requestAccounts") return [walletAddress];
        if (method === "personal_sign") return "0xsigned";
        throw new Error(`unexpected ethereum method ${method}`);
      },
    };
  }, address);

  await page.route("**/auth/wallet/email", async (route) => {
    emailRequests += 1;
    await route.fulfill({ status: 500, body: "email gate should not be reached" });
  });
  await page.route("**/v1/auth/wallet/challenge", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.address).toBe(address);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          message: "trustedrouter.com wants you to sign in",
          nonce: "wallet-nonce",
          expires_at: "2026-05-04T00:00:00Z",
        },
      }),
    });
  });
  await page.route("**/v1/auth/wallet/verify", async (route) => {
    const body = route.request().postDataJSON();
    expect(body).toEqual({
      address,
      signature: "0xsigned",
      nonce: "wallet-nonce",
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          redirect: "/console/api-keys",
          state: "active",
          email_required: false,
          workspace_id: "ws_wallet",
        },
      }),
    });
  });
  await page.route("**/console/api-keys", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<main><h1>API Keys</h1><p>$0.00</p></main>",
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: /MetaMask/ }).click();

  await expect(page).toHaveURL(/\/console\/api-keys$/);
  await expect(page.getByRole("heading", { name: "API Keys" })).toBeVisible();
  expect(emailRequests).toBe(0);
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
