const { expect, test } = require("@playwright/test");

const DOMAINS = (process.env.TR_ROLLOUT_SMOKE_DOMAINS || "").split(",").filter(Boolean);

test("signed-in production surfaces respond on every managed apex", async ({ browser }) => {
  expect(DOMAINS).toEqual([
    "trustedrouter.com",
    "allyrouter.com",
    "uptimerouter.com",
  ]);

  const context = await browser.newContext({
    storageState: process.env.TR_ROLLOUT_SMOKE_PLAYWRIGHT_STORAGE_STATE,
    extraHTTPHeaders: {
      Authorization: require("node:fs")
        .readFileSync(process.env.TR_ROLLOUT_SMOKE_AUTH_HEADER_FILE, "utf8")
        .trim()
        .replace(/^Authorization:\s*/, ""),
    },
  });
  const page = await context.newPage();
  try {
    for (const domain of DOMAINS) {
      const origin = `https://${domain}`;
      const home = await page.goto(`${origin}/`, { waitUntil: "domcontentloaded" });
      expect(home?.status()).toBe(200);
      await expect(page.locator("body")).toContainText(`api.${domain}`);

      const support = await page.goto(`${origin}/support`, {
        waitUntil: "domcontentloaded",
      });
      expect(support?.status()).toBe(200);
      await expect(page.locator("#support-inquiry")).toBeVisible();

      const session = await page.request.get(`${origin}/auth/session`);
      expect(session.status()).toBe(200);
      const sessionJson = await session.json();
      expect(sessionJson.data.authenticated).toBe(true);
      expect(sessionJson.data.management).toBe(true);

      const consolePage = await page.goto(`${origin}/console/api-keys`, {
        waitUntil: "domcontentloaded",
      });
      expect(consolePage?.status()).toBe(200);
      expect(new URL(page.url()).pathname).toBe("/console/api-keys");

      const models = await page.request.get(`${origin}/v1/models`);
      expect(models.status()).toBe(200);
      expect((await models.json()).data.length).toBeGreaterThan(100);
    }
  } finally {
    await context.close();
  }
});

test("Firefox reaches split auth gates without inference or billing credentials", async ({
  browser,
}) => {
  expect(DOMAINS).toEqual([
    "trustedrouter.com",
    "allyrouter.com",
    "uptimerouter.com",
  ]);

  // This context intentionally inherits neither the signed-in storage state
  // nor the management Authorization header used by the preceding smoke. Each
  // fetch also omits credentials explicitly so only the pre-body auth gates can
  // run; no browser key, internal token, prompt, API-key hash, or payment data
  // is present in these requests.
  const context = await browser.newContext({
    extraHTTPHeaders: {},
    storageState: { cookies: [], origins: [] },
  });
  const page = await context.newPage();
  try {
    for (const domain of DOMAINS) {
      const origin = `https://${domain}`;
      const home = await page.goto(`${origin}/`, { waitUntil: "domcontentloaded" });
      expect(home?.status()).toBe(200);
      expect((await context.cookies()).some((cookie) => cookie.name === "tr_session")).toBe(
        false,
      );

      const safePost = async (path, requestId) =>
        page.evaluate(
          async ({ body, requestId: id, route }) => {
            const response = await fetch(route, {
              method: "POST",
              credentials: "omit",
              redirect: "manual",
              headers: {
                "content-type": "application/json",
                "x-request-id": id,
              },
              body: JSON.stringify(body),
            });
            return {
              body: await response.json(),
              contentType: response.headers.get("content-type"),
              requestId: response.headers.get("x-trustedrouter-request-id"),
              status: response.status,
            };
          },
          {
            body: { smoke: "auth-gate-only" },
            requestId,
            route: path,
          },
        );

      const safeDomain = domain.replaceAll(".", "-");
      const chatRequestId = `tr-rollout-firefox-chat-${safeDomain}`;
      const chat = await safePost(
        "/chat-proxy/v1/chat/completions",
        chatRequestId,
      );
      expect(chat.status).toBe(401);
      expect(chat.contentType).toContain("application/json");
      expect(chat.requestId).toBe(chatRequestId);
      expect(chat.body.error).toEqual({
        code: 401,
        message: "Missing Authentication header",
        source: "router",
        type: "unauthorized",
      });

      const internalRequestId = `tr-rollout-firefox-internal-${safeDomain}`;
      const internal = await safePost(
        "/v1/internal/gateway/authorize",
        internalRequestId,
      );
      expect(internal.status).toBe(401);
      expect(internal.contentType).toContain("application/json");
      expect(internal.requestId).toBe(internalRequestId);
      expect(internal.body.error).toEqual({
        code: 401,
        message: "Invalid internal service token",
        source: "router",
        type: "unauthorized",
      });
    }
  } finally {
    await context.close();
  }
});
