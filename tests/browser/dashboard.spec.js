const { expect, test } = require("@playwright/test");

test("homepage opens sign-in modal and handles missing MetaMask", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", {
    name: "550+ AI Models at your fingertips. One Unified Interface. Privacy with proof.",
  })).toBeVisible();
  const headlineLines = page.locator(".charter-home-hero h1 .hero-line");
  await expect(headlineLines).toHaveCount(3);
  await expect(headlineLines.first()).toHaveCSS("display", "block");
  await expect(headlineLines.last()).toHaveCSS("display", "block");
  await expect(page.locator(".charter-home-hero .lead-claim")).toHaveText(
    "Better privacy, better prices, better uptime, no subscriptions.",
  );
  await expect(page.locator(".charter-stat-band")).toContainText("550+AI models");
  await expect(page.locator(".charter-stat-band")).toContainText("7Live regions");
  await expect(page.locator(".charter-stat-band")).toContainText("3Clouds");
  await expect(page.locator(".charter-stat-band")).toContainText("4Continents");
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

test("paid search quickstart copies runnable code and opens key creation", async ({
  context,
  page,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/openai-compatible-llm-api");

  const sample = page.locator("#openai-first-call");
  await expect(sample).toContainText("import os");
  await expect(sample).toContainText('model="trustedrouter/cheap"');

  await page.getByRole("button", { name: "Copy", exact: true }).click();
  await expect(page.getByRole("button", { name: "Copied", exact: true })).toBeVisible();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("import os");
  expect(copied).toContain('model="trustedrouter/cheap"');

  await page.getByRole("button", { name: "Create my API key" }).first().click();
  await expect(page.locator("#signinModal")).toBeVisible();
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

test("new API key quickstart stays inside its reveal panel", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value) => { window.__copiedAgentMessage = value; },
        readText: async () => window.__copiedAgentMessage || "",
      },
    });
  });
  await page.route("**/console/api-keys", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: `<!doctype html>
        <html lang="en">
          <head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="stylesheet" href="/static/dashboard.css">
            <link rel="stylesheet" href="/static/charter.css">
            <link rel="stylesheet" href="/static/console.css">
            <script defer src="/static/console.js"></script>
          </head>
          <body class="console">
            <div class="console-shell">
              <aside class="console-sidebar"></aside>
              <main class="console-main">
                <div class="console-page-body">
                  <section class="panel">
                    <div class="panel-body">
                      <div class="signup-reveal console-key-reveal">
                        <section class="agent-quickstart">
                          <h3>Paste this short message into a Claude Code, Codex, or your favorite agent chat.</h3>
                          <div class="agent-message-row">
                            <div class="agent-message" id="layout-agent-message" data-copy-lines>
                            <span class="agent-prompt-line">Use TrustedRouter.com with the key below to ask DeepSeek: "What is the capital of France?"</span>
                            <span class="agent-prompt-line">TrustedRouter API key: sk-tr-v1-layout-regression-key</span>
                            </div>
                            <button class="btn secret-copy-btn" type="button" data-copy-secret="layout-agent-message" aria-label="Copy complete agent message">Copy</button>
                          </div>
                        </section>
                        <div class="key-reveal-section">
                          <div class="signup-reveal-head">Your TrustedRouter API key</div>
                          <div class="secret-row"><code id="layout-api-key">sk-tr-v1-layout-regression-key</code><button class="btn secret-copy-btn" type="button" data-copy-secret="layout-api-key">Copy</button></div>
                        </div>
                      </div>
                    </div>
                  </section>
                </div>
              </main>
            </div>
          </body>
        </html>`,
    });
  });
  await page.goto("/console/api-keys");

  const reveal = page.locator(".console-key-reveal");
  const heading = reveal.locator(".agent-quickstart h3");
  const message = reveal.locator(".agent-message");
  const directKey = reveal.locator("#layout-api-key");
  await expect(heading).toBeVisible();
  await expect(message).toContainText('ask DeepSeek: "What is the capital of France?"');
  await expect(message).not.toContainText("stream the answer into this chat as it arrives");
  await expect(message).not.toContainText("Paste this short message");
  await expect(message).not.toContainText("Keep this agent's settings");
  await expect(directKey).toBeVisible();
  expect(await reveal.evaluate((element) => {
    const agentMessage = element.querySelector(".agent-message");
    const key = element.querySelector("#layout-api-key");
    return Boolean(agentMessage.compareDocumentPosition(key) & Node.DOCUMENT_POSITION_FOLLOWING);
  })).toBe(true);

  const assertContained = async () => {
    const layout = await reveal.evaluate((element) => {
      const headingElement = element.querySelector(".agent-quickstart h3");
      const messageElement = element.querySelector(".agent-message");
      const bounds = element.getBoundingClientRect();
      return {
        pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
        headingOverflow: headingElement.scrollWidth - headingElement.clientWidth,
        messageOverflow: messageElement.scrollWidth - messageElement.clientWidth,
        headingRight: headingElement.getBoundingClientRect().right - bounds.right,
        messageRight: messageElement.getBoundingClientRect().right - bounds.right,
      };
    });
    expect(layout.pageOverflow).toBeLessThanOrEqual(2);
    expect(layout.headingOverflow).toBeLessThanOrEqual(1);
    expect(layout.messageOverflow).toBeLessThanOrEqual(1);
    expect(layout.headingRight).toBeLessThanOrEqual(1);
    expect(layout.messageRight).toBeLessThanOrEqual(1);
  };

  await assertContained();
  await page.setViewportSize({ width: 820, height: 900 });
  await assertContained();
  // A rolling multi-region deploy can briefly pair new HTML with an older
  // console stylesheet. The message must still use normal HTML wrapping.
  await page.evaluate(() => document.querySelector('link[href="/static/console.css"]').remove());
  await assertContained();
  await page.setViewportSize({ width: 390, height: 844 });
  await assertContained();
  const copyButton = reveal.getByRole("button", { name: "Copy complete agent message" });
  await expect(copyButton).toBeVisible();
  await copyButton.click();
  const copiedAgentMessage = await page.evaluate(() => navigator.clipboard.readText());
  expect(copiedAgentMessage).not.toContain("Paste this short message");
  expect(copiedAgentMessage).not.toContain("stream the answer into this chat as it arrives");
  expect(copiedAgentMessage).not.toContain("Keep this agent's settings");
  expect(copiedAgentMessage).not.toContain("Use it in memory for this request");
  expect(copiedAgentMessage).not.toContain("stream=true");
  expect(copiedAgentMessage).not.toContain("ANTHROPIC_BASE_URL");
  expect(copiedAgentMessage).toContain("sk-tr-v1-layout-regression-key");
});

test("first-call activation runs live request and copies agent chat prompt", async ({ page }) => {
  const apiKey = "sk-tr-v1-browser-activation-key";
  const analytics = [];
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value) => { window.__activationClipboard = value; },
        readText: async () => window.__activationClipboard || "",
      },
    });
  });
  await page.route("**/analytics/events", async (route) => {
    analytics.push(route.request().postDataJSON().event);
    await route.fulfill({ status: 204, body: "" });
  });
  await page.route("**/chat-proxy/v1/chat/completions", async (route) => {
    expect(route.request().headers().authorization).toBe(`Bearer ${apiKey}`);
    expect(route.request().headers()["idempotency-key"]).toMatch(/^welcome-/);
    expect(route.request().postDataJSON()).toEqual({
      model: "trustedrouter/cheap",
      messages: [{ role: "user", content: "Reply with exactly PONG." }],
      temperature: 0,
      max_tokens: 8,
      stream: false,
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "x-trustedrouter-provider": "cerebras",
        "x-trustedrouter-served-model": "openai/gpt-oss-120b",
      },
      body: JSON.stringify({
        model: "openai/gpt-oss-120b",
        choices: [{ message: { role: "assistant", content: "PONG" } }],
        usage: { cost_microdollars: 17 },
      }),
    });
  });
  await page.route("**/activation-test", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: `<!doctype html>
        <html lang="en"><head>
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <link rel="stylesheet" href="/static/dashboard.css">
          <link rel="stylesheet" href="/static/charter.css">
          <link rel="stylesheet" href="/static/console.css">
          <script defer src="/static/console.js"></script>
        </head><body class="console"><main class="console-main"><div class="console-page-body">
          <div class="activation-flow" data-first-call-flow data-endpoint="/chat-proxy/v1/chat/completions" data-key-source="welcome-api-key">
            <ol class="activation-progress"><li class="complete"><span>1</span><strong>Account</strong></li><li class="current"><span>2</span><strong>First call</strong></li><li><span>3</span><strong>Connect app</strong></li></ol>
            <section class="panel activation-key-panel"><div class="panel-head"><h2>Save it now</h2></div><div class="panel-body"><div class="activation-code-panel activation-onboarding-agent"><div class="activation-code-head"><strong>Paste this short message into a Claude Code, Codex, or your favorite agent chat.</strong><button class="btn" data-copy-template-target="welcome-agent-message" data-secret-source="welcome-api-key">Copy message</button></div><pre id="welcome-agent-message" data-copy-lines><span>Use TrustedRouter.com with the key below to ask DeepSeek: "What is the capital of France?"</span><span>TrustedRouter API key: ${apiKey}</span></pre></div><div class="secret-row"><code id="welcome-api-key">${apiKey}</code><button class="btn" data-copy-secret="welcome-api-key">Copy</button></div></div></section>
            <section class="panel activation-test-panel"><div class="panel-head activation-panel-head"><div><p class="activation-eyebrow">Live gateway check</p><h2>Run your first API request</h2><p class="panel-kicker">A real, inexpensive PONG request confirms the route.</p></div><span class="activation-step-number">02</span></div><div class="panel-body">
              <button class="btn primary activation-run-button" type="button" data-action="run-first-call"><span data-run-label>Run my first API request</span><span class="activation-button-arrow">&#8594;</span></button>
              <div class="activation-call-error" data-call-error hidden><strong data-call-error-title></strong><p data-call-error-message></p><a data-call-error-action hidden></a></div>
              <div class="activation-call-result" data-call-result hidden><div class="activation-result-head"><div><span class="activation-success-mark">&#10003;</span><div><strong>Production request passed</strong><p>Your key is ready.</p></div></div><code data-result-output></code></div><dl class="activation-result-grid"><div><dt>Model</dt><dd data-result-model></dd></div><div><dt>Provider</dt><dd data-result-provider></dd></div><div><dt>Latency</dt><dd data-result-latency></dd></div><div><dt>Exact cost</dt><dd data-result-cost></dd></div></dl><div data-success-actions></div></div>
            </div></section>
            <section class="panel activation-setup-panel"><div class="panel-body"><div class="activation-tabs" role="tablist"><button class="activation-tab" role="tab" aria-selected="true" data-setup-tab="setup-python">Python</button></div><div class="activation-code-panel" id="setup-python" data-setup-panel><pre>Python setup</pre></div></div></section>
          </div>
        </div></main></body></html>`,
    });
  });

  await page.goto("/activation-test");
  const rawCopies = await page.evaluate(
    (secret) => document.body.innerHTML.split(secret).length - 1,
    apiKey,
  );
  expect(rawCopies).toBe(2);

  await page.getByRole("button", { name: "Run my first API request" }).click();
  await expect(page.getByText("Production request passed")).toBeVisible();
  await expect(page.locator("[data-result-output]")).toHaveText("PONG");
  await expect(page.locator("[data-result-model]")).toHaveText("openai/gpt-oss-120b");
  await expect(page.locator("[data-result-provider]")).toHaveText("cerebras");
  await expect(page.locator("[data-result-latency]")).toContainText("ms");
  await expect(page.locator("[data-result-cost]")).toHaveText("$0.000017");
  await expect.poll(() => analytics).toContain("first_call_started");

  await page.getByRole("button", { name: "Copy message" }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(
    `Use TrustedRouter.com with the key below to ask DeepSeek: "What is the capital of France?"\n\nTrustedRouter API key: ${apiKey}`,
  );
  await page.getByRole("tab", { name: "Python" }).click();
  await expect(page.locator("#setup-python")).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(2);
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

  await expect(page.getByRole("heading", {
    name: "550+ AI Models at your fingertips. One Unified Interface. Privacy with proof.",
  })).toBeVisible();
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

test("homepage explainer video contacts YouTube only after an explicit press", async ({
  page,
}) => {
  const thirdParty = [];
  page.on("request", (request) => {
    if (/youtube|ytimg|googlevideo/.test(request.url())) {
      thirdParty.push(request.url());
    }
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "How TrustedRouter works" })).toBeVisible();

  const facade = page.locator('[data-action="load-video"]');
  await expect(facade).toBeVisible();

  // The point of the facade: a privacy-first homepage must not hand Google a
  // pageview to render. Nothing may be requested before the visitor asks.
  await expect(page.locator('[data-action="load-video"] iframe')).toHaveCount(0);
  expect(thirdParty).toEqual([]);

  await facade.click();

  const frame = page.locator('[data-action="load-video"] iframe');
  await expect(frame).toHaveCount(1);
  const src = await frame.getAttribute("src");
  // Privacy-enhanced host, and the timestamp the video actually explains from.
  expect(src).toContain("youtube-nocookie.com/embed/UzLY4kvjklI");
  expect(src).toContain("start=128");

  // Once loaded it is a player, not a button.
  await expect(facade).not.toHaveAttribute("role", "button");
  await facade.click();
  await expect(frame).toHaveCount(1);
});

test("homepage explainer video is keyboard operable", async ({ page }) => {
  await page.goto("/");
  const facade = page.locator('[data-action="load-video"]');
  await expect(facade).toHaveAttribute("role", "button");
  await facade.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator('[data-action="load-video"] iframe')).toHaveCount(1);
});
