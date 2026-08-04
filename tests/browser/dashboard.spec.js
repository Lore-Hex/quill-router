const { expect, test } = require("@playwright/test");

test("homepage opens sign-in modal and handles missing MetaMask", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Every model. Provable privacy." })).toBeVisible();
  const headlineLines = page.locator(".charter-home-hero h1 .hero-line");
  await expect(headlineLines).toHaveCount(2);
  await expect(headlineLines.first()).toHaveCSS("display", "block");
  await expect(headlineLines.last()).toHaveCSS("display", "block");
  await expect(page.locator("body")).toHaveCSS("font-size", "15.5px");
  await expect(page.locator(".charter-pillar p a").first()).toHaveCSS("text-decoration-line", "underline");

  const faintTextContrast = await page.evaluate(() => {
    const luminance = (color) => {
      const [red, green, blue] = color.match(/[\d.]+/g).slice(0, 3).map(Number).map((channel) => {
        const value = channel / 255;
        return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
    };
    const ratio = (foreground, background) => {
      const lighter = Math.max(luminance(foreground), luminance(background));
      const darker = Math.min(luminance(foreground), luminance(background));
      return (lighter + 0.05) / (darker + 0.05);
    };
    const measure = () => ratio(
      getComputedStyle(document.querySelector(".charter-pillar-number")).color,
      getComputedStyle(document.body).backgroundColor,
    );
    const dark = measure();
    document.documentElement.dataset.theme = "light";
    return { dark, light: measure() };
  });
  expect(faintTextContrast.dark).toBeGreaterThanOrEqual(4.5);
  expect(faintTextContrast.light).toBeGreaterThanOrEqual(4.5);
  await expect(page.getByText("ATTESTED GATEWAY", { exact: true })).toBeVisible();
  await expect(page.locator(".charter-home-hero .hero-links")).toHaveCSS("justify-content", "flex-start");

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
  await page.route("**/provider", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<main><h1>Provider operations</h1></main>",
    });
  });

  await page.goto("/?reason=signin&next=%2Fprovider");
  await expect(page.locator('a[data-provider="google"]')).toHaveAttribute(
    "href",
    "/auth/google/login?next=%2Fprovider",
  );
  await page.getByRole("button", { name: /MetaMask/ }).click();

  await expect(page).toHaveURL(/\/provider$/);
  await expect(page.getByRole("heading", { name: "Provider operations" })).toBeVisible();
  expect(emailRequests).toBe(0);
});

test("console redirects unauthenticated users and auto-opens sign-in", async ({ page }) => {
  await page.goto("/console/api-keys");

  await expect(page).toHaveURL(/reason=signin/);
  await expect(page.locator("#signinModal")).toBeVisible();
});

test("delegated sign-in explains zero-credit onboarding", async ({ page }) => {
  const target = encodeURIComponent(
    "/auth?callback_url=https%3A%2F%2Fslopnazi.com%2Feditor&key_label=SlopNazi&limit=5&usage_limit_type=monthly",
  );
  await page.goto(`/?reason=signin&next=${target}`);

  await expect(page.locator("#signinModal")).toBeVisible();
  await expect(page.locator("#signinCreditNote")).toContainText(
    "Accounts created for this app start at $0",
  );
  await expect(page.locator('a[data-provider="google"]')).toHaveAttribute(
    "href",
    /auth\/google\/login\?next=/,
  );
});

test("delegated consent exposes funding and an editable app cap", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-trustedrouter-user": "oauth-browser@example.com" });
  await page.goto(
    "/auth?callback_url=https%3A%2F%2Fslopnazi.com%2Feditor&key_label=SlopNazi&limit=5&usage_limit_type=monthly",
  );

  await expect(page.getByRole("heading", { name: "Authorize SlopNazi" })).toBeVisible();
  await expect(page.getByText("$0.00 available")).toBeVisible();
  await expect(page.getByText("This account starts at $0")).toBeVisible();
  await expect(page.getByRole("radio", { name: "$20" })).toBeChecked();
  await expect(page.getByLabel("Maximum spend (USD)")).toHaveValue("5");
  await expect(page.getByLabel("Limit resets")).toHaveValue("monthly");
  await expect(page.getByRole("button", { name: "Authorize SlopNazi" })).toBeVisible();
  await expect(page.getByText("card is saved", { exact: false })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(2);
});

test("homepage and console redirect are usable on mobile width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Every model. Provable privacy." })).toBeVisible();
  let overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(2);

  await page.goto("/docs/video");
  await expect(page.getByRole("heading", { name: "Video Generation API" })).toBeVisible();
  overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(2);
});

test("prompt caching guide and public 404 remain useful on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/docs/prompt-caching");

  await expect(page.getByRole("heading", { name: "Prompt Caching For Lower LLM Costs" })).toBeVisible();
  await expect(page.getByText("Reuse long prompt prefixes.")).toBeVisible();
  let overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(2);

  const response = await page.goto("/missing-public-page");
  expect(response.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "Page Not Found" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open documentation" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Browse models" })).toBeVisible();
  await expect(page.getByRole("link", { name: "View status" })).toBeVisible();
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute("content", "noindex,follow");
  overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(2);
});

test("Charter design remains shared and responsive across public surfaces", async ({ page }) => {
  for (const path of ["/", "/models", "/security", "/status", "/blog"]) {
    await page.goto(path);
    await expect(page.locator(".brand-mark").first()).toBeVisible();
    const theme = await page.evaluate(() => {
      const body = getComputedStyle(document.body);
      const heading = document.querySelector("h1");
      return {
        bodyFont: body.fontFamily,
        background: body.backgroundColor,
        headingFont: heading ? getComputedStyle(heading).fontFamily : "",
        overflow: document.documentElement.scrollWidth - window.innerWidth,
      };
    });
    expect(theme.bodyFont).toContain("Archivo");
    expect(theme.headingFont).toContain("Spectral");
    expect(theme.background).toBe("rgb(10, 14, 11)");
    expect(theme.overflow).toBeLessThanOrEqual(2);
  }
});

test("homepage exposes privacy, no-subscription, and open-source claims", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByText("End-to-End Encrypted AI gateway").first()).toBeVisible();
  await expect(page.getByText("No subscription required")).toBeVisible();
  await expect(page.getByText("inspect, fork, or run yourself")).toBeVisible();
  await expect(page.getByText("ATTESTED GATEWAY", { exact: true })).toBeVisible();
  await expect(
    page.locator('a[href="https://github.com/Lore-Hex/trusted-router-py"]').first(),
  ).toBeVisible();
  await expect(
    page.locator('a[href="https://github.com/Lore-Hex/trusted-router-js"]').first(),
  ).toBeVisible();
});

test("local trust page links the public source repositories and release files", async ({ page }) => {
  await page.goto("/trust");

  await expect(page.getByRole("paragraph").filter({ hasText: "api.trustedrouter.com is the prompt path" })).toBeVisible();
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

test("synth local demo streams raw thinking and completes", async ({ page }) => {
  await page.goto("/synth?demo=1");

  await expect(page.locator("[data-fusion-synthesis-prompt]")).toBeHidden();
  await page.getByText("Advanced settings").click();
  await expect(page.locator("[data-fusion-synthesis-prompt]")).toBeVisible();
  await page.locator("[data-fusion-prompt]").fill("Compare two router designs.");
  await page.locator("[data-fusion-synthesis-prompt]").fill("Return a crisp recommendation.");
  await expect(page.locator("[data-fusion-code]")).toContainText('"synthesis_prompt": "Return a crisp recommendation."');
  await page.getByRole("button", { name: "Run Synth" }).click();

  await expect(page.locator("[data-result-title]")).toContainText("Completed");
  await expect(page.locator("[data-fusion-answer]")).toContainText("Demo Synth answer.");
  await expect(page.locator("[data-fusion-details]")).toBeVisible();
  await expect(page.locator("[data-fusion-details]")).toContainText("Panel raw thinking and output");
  await expect(page.locator("[data-fusion-details]")).toContainText("Judge raw thinking and output");
  await expect(page.locator("[data-fusion-details]")).toContainText("Final synthesizer raw thinking and output");
  await expect(page.locator("[data-fusion-details]")).toContainText("Demo raw thinking from");
});

test("synth preserves streamed thinking when final visible answer is empty", async ({ page }) => {
  await page.goto("/synth?demo=1&demo_empty=1");

  await page.locator("[data-fusion-prompt]").fill("Run a regression that returns no final visible content.");
  await page.getByRole("button", { name: "Run Synth" }).click();

  await expect(page.locator("[data-result-title]")).toContainText("Needs review");
  await expect(page.locator("[data-fusion-error]")).toContainText("Synth returned an empty final answer.");
  await expect(page.locator("[data-fusion-answer]")).toContainText("Raw panel, judge, and synthesizer traces are preserved below.");
  await expect(page.locator("[data-fusion-details]")).toBeVisible();
  await expect(page.locator("[data-fusion-details]")).toContainText("Panel raw thinking and output");
  await expect(page.locator("[data-fusion-details]")).toContainText("Judge raw thinking and output");
  await expect(page.locator("[data-fusion-details]")).toContainText("Final synthesizer raw thinking and output");
  await expect(page.locator("[data-fusion-details]")).toContainText("Final synthesizer demo thinking.");
});

test("model picker applies privacy to exact provider routes", async ({ page }) => {
  await page.goto("/choose");
  const picker = page.frameLocator("#tr-choose-frame");

  await expect(picker.locator("#loadState")).toContainText("independently scored models");
  await picker.getByRole("button", { name: /Simple/ }).click();
  await picker.getByRole("button", { name: /Any/ }).click();
  await picker.locator("#privacy").selectOption("3");

  await expect(picker.locator(".model-card").first()).toBeVisible();
  await expect(picker.locator(".route-recommendation code").first()).toHaveText(
    "trustedrouter/e2e",
  );
  await expect(picker.locator(".model-card", { hasText: "DeepSeek V4 Pro" })).toHaveCount(0);
  const routeLabels = await picker.locator(".provider-route").allTextContents();
  expect(routeLabels.length).toBeGreaterThan(0);
  expect(routeLabels.every((label) => label.endsWith("· TEE"))).toBe(true);

  await picker.locator("#privacy").selectOption("2");
  await expect(picker.locator(".route-recommendation code").first()).toHaveText(
    "trustedrouter/zdr",
  );
  const zdrRouteLabels = await picker.locator(".provider-route").allTextContents();
  expect(zdrRouteLabels.every((label) => !label.endsWith("· Open"))).toBe(true);
});

test("model picker triangle is keyboard adjustable", async ({ page }) => {
  await page.goto("/static/choose-app.html");
  await expect(page.locator("#loadState")).toContainText("independently scored models");

  const before = Number(await page.locator("#qualityWeight").textContent());
  await page.locator("#triangle").focus();
  await page.keyboard.press("ArrowUp");
  const after = Number(await page.locator("#qualityWeight").textContent());
  expect(after).toBeGreaterThan(before);

  await page.keyboard.press("ArrowDown");
  await expect(page.locator("#qualityWeight")).toHaveText("33");
  await expect(page.locator("#costWeight")).toHaveText("33");
  await expect(page.locator("#speedWeight")).toHaveText("33");
});

test("model picker fails closed when route facts are unavailable", async ({ page }) => {
  await page.route("**/choose/catalog.json", async (route) => {
    await route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
  });
  await page.goto("/static/choose-app.html");

  await expect(page.locator("#loadState")).toContainText("HTTP 503");
  await expect(page.locator("#retry-catalog")).toBeVisible();
  await expect(page.locator(".alias-card")).toHaveCount(0);
  await expect(page.locator(".model-card")).toHaveCount(0);
  await expect(page.locator("#modelResults")).toContainText(
    "Recommendations are unavailable",
  );
});

test("model picker has no horizontal overflow at mobile width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/static/choose-app.html");
  await expect(page.locator("#loadState")).toContainText("independently scored models");

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(2);
});
