const { expect, test } = require("@playwright/test");

const DOMAINS = [
  "trustedrouter.localhost",
  "allyrouter.localhost",
  "uptimerouter.localhost",
];

function origin(domain) {
  return `http://${domain}:18081`;
}

async function surface(response) {
  const headers = typeof response.allHeaders === "function"
    ? await response.allHeaders()
    : response.headers;
  return headers["x-tr-test-surface"];
}

async function localFetch(page, path, { method = "GET", headers = {}, data } = {}) {
  return page.evaluate(async ({ requestPath, requestMethod, requestHeaders, requestData }) => {
    const response = await fetch(requestPath, {
      method: requestMethod,
      headers: {
        ...(requestData === undefined ? {} : { "content-type": "application/json" }),
        ...requestHeaders,
      },
      body: requestData === undefined ? undefined : JSON.stringify(requestData),
      credentials: "same-origin",
    });
    const text = await response.text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch (_error) {
      // Static assets and SSE responses are intentionally not JSON.
    }
    return {
      status: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      text,
      json,
    };
  }, {
    requestPath: path,
    requestMethod: method,
    requestHeaders: headers,
    requestData: data,
  });
}

async function localOnlyContext(browser) {
  const context = await browser.newContext();
  await context.route("**/*", async (route) => {
    const hostname = new URL(route.request().url()).hostname;
    if (hostname.endsWith(".localhost") || hostname === "127.0.0.1") {
      await route.continue();
      return;
    }
    await route.abort("blockedbyclient");
  });
  return context;
}

test("all three domains traverse the six real service surfaces without side effects", async ({
  browser,
}) => {
  const context = await localOnlyContext(browser);
  const page = await context.newPage();

  try {
    for (const domain of DOMAINS) {
      const base = origin(domain);

      const home = await page.goto(`${base}/`);
      expect(home.status()).toBe(200);
      expect(await surface(home)).toBe("public");
      await expect(page.locator(".brand-mark").first()).toBeVisible();
      await expect(page.locator("body")).toContainText(`api.${domain}`);

      const asset = await localFetch(page, "/static/dashboard.css");
      expect(asset.status).toBe(200);
      expect(await surface(asset)).toBe("public");

      const consoleRedirectPromise = page.waitForResponse(
        (response) => new URL(response.url()).pathname === "/console/api-keys",
      );
      await page.goto(`${base}/console/api-keys`);
      const consoleRedirect = await consoleRedirectPromise;
      expect(consoleRedirect.status()).toBe(302);
      const consoleHeaders = await consoleRedirect.allHeaders();
      expect(consoleHeaders.location).toBe("/?reason=signin");
      expect(await surface(consoleRedirect)).toBe("console");

      await page.goto(`${base}/support`);
      const actionRequests = [];
      const recordAction = (request) => {
        if (request.url().endsWith("/support/inquiry")) actionRequests.push(request.url());
      };
      page.on("request", recordAction);
      await page.getByRole("button", { name: "Send support request" }).click();
      await expect(page.locator("#support-inquiry [data-role=status]")).toContainText(
        "Please complete your name, email, subject, and message.",
      );
      expect(actionRequests).toEqual([]);
      page.off("request", recordAction);

      const invalidAction = await localFetch(page, "/support/inquiry", {
        method: "POST",
        data: {
          name: "",
          email: "not-an-email",
          category: "api",
          subject: "",
          message: "",
          website: "",
        },
      });
      expect(invalidAction.status).toBe(422);
      expect(await surface(invalidAction)).toBe("actions");

      const eventId = `evt_invalid_signature_${domain.replaceAll(".", "_")}`;
      const statePath = `/__test__/state?stripe_event_id=${eventId}`;
      const before = (await localFetch(page, statePath)).json;
      const invalidWebhook = await localFetch(
        page,
        "/internal/stripe/webhook",
        {
          method: "POST",
          headers: { "stripe-signature": "t=0,v1=invalid" },
          data: {
            id: eventId,
            type: "payment_intent.succeeded",
            data: { object: { id: "pi_never_recorded" } },
          },
        },
      );
      expect(invalidWebhook.status).toBe(400);
      expect(await surface(invalidWebhook)).toBe("webhooks");
      const after = (await localFetch(page, statePath)).json;
      expect(after).toEqual(before);
      expect(after.stripe_event_recorded).toBe(false);

      const rejectedInternal = await localFetch(
        page,
        "/internal/gateway/authorize",
        {
          method: "POST",
          data: {
            api_key_hash: "browser-harness-missing-key",
            model: "trustedrouter/test",
          },
        },
      );
      expect(rejectedInternal.status).toBe(401);
      expect(await surface(rejectedInternal)).toBe("internal");
    }
  } finally {
    await context.close();
  }
});

test("signed-in chat crosses public, console, and chat services on every domain", async ({
  browser,
}) => {
  const context = await localOnlyContext(browser);
  const page = await context.newPage();

  try {
    for (const domain of DOMAINS) {
      const base = origin(domain);

      await page.goto(`${base}/`);
      const session = await localFetch(page, "/__test__/session", {
        method: "POST",
        data: { email: `chat-${domain.replaceAll(".", "-")}@example.test` },
      });
      expect(session.status).toBe(200);
      expect(await surface(session)).toBe("harness");

      const modelsResponsePromise = page.waitForResponse(
        (response) => new URL(response.url()).pathname === "/v1/models",
      );
      const chatPage = await page.goto(`${base}/chat`);
      const modelsResponse = await modelsResponsePromise;
      expect(chatPage.status()).toBe(200);
      expect(await surface(chatPage)).toBe("public");
      expect(await surface(modelsResponse)).toBe("public");

      const authResponsePromise = page.waitForResponse(
        (response) => new URL(response.url()).pathname === "/auth/session",
      );
      const issueKeyResponsePromise = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/internal/chat/issue-browser-key",
      );
      const chatProxyResponsePromise = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/chat-proxy/v1/chat/completions",
      );
      await page.locator("[data-chat-input]").fill("Prove the local split.");
      await page.locator("[data-chat-send]").click();
      await expect(page.locator(".chat-msg-assistant").last()).toContainText(
        "Six-surface local reply.",
      );
      const [authResponse, issueKeyResponse, chatProxyResponse] =
        await Promise.all([
          authResponsePromise,
          issueKeyResponsePromise,
          chatProxyResponsePromise,
        ]);
      expect(await surface(authResponse)).toBe("console");
      expect(await surface(issueKeyResponse)).toBe("console");
      expect(await surface(chatProxyResponse)).toBe("chat");
      const chatProxyHeaders = await chatProxyResponse.allHeaders();
      expect(chatProxyHeaders["x-tr-test-upstream"]).toBe(
        `https://api.${domain}`,
      );
    }
  } finally {
    await context.close();
  }
});
