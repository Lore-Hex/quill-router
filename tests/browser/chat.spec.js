const { expect, test } = require("@playwright/test");
const http = require("node:http");

const MODEL = "deepseek/deepseek-v4-flash-0731";
const LONG_NAME = "DeepSeek V4 Flash 0731 High Speed Extended Context Preview";
const sse = (...events) => events.map((event) => "data: " + JSON.stringify(event) + "\n\n").join("") + "data: [DONE]\n\n";

async function setup(page, context, signedIn = true) {
  if (signedIn) await context.addCookies([{ name: "tr_signed_in", value: "1", url: "http://127.0.0.1:18081" }]);
  await page.route("**/auth/session", (route) => route.fulfill({ json: { data: { user: { id: "chat-test" }, workspace: { id: "chat-test-workspace" } } } }));
  await page.route("**/internal/chat/issue-browser-key", (route) => route.fulfill({ json: { data: { raw_key: "sk-tr-test-only" } } }));
  await page.route(/\/v1\/models(?:\/picker)?$/, (route) => route.fulfill({ json: { data: [{
    id: MODEL, name: LONG_NAME, context_length: 1_000_000,
    pricing: { prompt: "0.000001", completion: "0.000002" },
    trustedrouter: { open_weights: true, us_provider_available: true,
      eu_focused_provider_available: true, capabilities: ["vision", "tools"] },
  }] } }));
  // All inference is mocked. Unexpected requests must never leave the test.
  await page.route("**/v1/chat/completions", (route) => route.fulfill({
    contentType: "text/event-stream", body: sse({ choices: [{ delta: { content: "Hello world" } }] }),
  }));
}

async function send(page) {
  await page.getByRole("textbox", { name: "Chat input" }).fill("Hello");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
}

test("chat and picker show full names with composer in viewport on desktop and mobile", async ({ page, context }, testInfo) => {
  await setup(page, context);
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`/chat?model=${MODEL}`);
    const name = page.locator(".chat-model-pill-name");
    await expect(name).toHaveText(LONG_NAME);
    const layout = await name.evaluate((el) => ({
      clipped: el.scrollWidth > el.clientWidth + 1,
      ellipsis: getComputedStyle(el).textOverflow,
      pageOverflow: document.documentElement.scrollWidth > innerWidth + 1,
      usageBottom: document.querySelector("[data-chat-usage]").getBoundingClientRect().bottom,
      composerBottom: document.querySelector(".chat-input-row").getBoundingClientRect().bottom,
    }));
    expect(layout).toMatchObject({ clipped: false, pageOverflow: false });
    expect(layout.ellipsis).not.toBe("ellipsis");
    expect(layout.usageBottom).toBeLessThanOrEqual(800);
    expect(layout.composerBottom).toBeLessThan(layout.usageBottom);
    await page.screenshot({ path: testInfo.outputPath(`chat-${width}.png`) });
    await page.locator(".chat-model-pill").click();
    await page.getByRole("button", { name: "Change model", exact: true }).click();
    const rowName = page.locator(".chat-model-row-name").first();
    await expect(rowName).toHaveText(LONG_NAME);
    expect(await rowName.evaluate((el) => el.scrollWidth <= el.clientWidth + 1 && getComputedStyle(el).textOverflow !== "ellipsis")).toBe(true);
    const rowLayout = await rowName.evaluate((el) => {
      const row = el.closest(".chat-model-row");
      return {
        mainBottom: row.querySelector(".chat-model-row-main").getBoundingClientRect().bottom,
        metaTop: row.querySelector(".chat-model-row-meta").getBoundingClientRect().top,
        clipped: row.scrollWidth > row.clientWidth + 1,
      };
    });
    expect(rowLayout.metaTop).toBeGreaterThanOrEqual(rowLayout.mainBottom);
    expect(rowLayout.clipped).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`picker-${width}.png`) });
  }
});

test("billed usage replaces cumulative chunks, includes cache/reasoning, and persists", async ({ page, context }) => {
  await setup(page, context);
  let request;
  const usage = { prompt_tokens: 1000, completion_tokens: 120, cost_microdollars: 12345,
    prompt_tokens_details: { cached_tokens: 800 }, completion_tokens_details: { reasoning_tokens: 40 },
    provider_usage: { total_cost_microdollars: 99999 } };
  await page.route("**/v1/chat/completions", (route) => {
    request = route.request().postDataJSON();
    return route.fulfill({ contentType: "text/event-stream", body: sse(
      { choices: [{ delta: { content: "Hello world" } }] }, { usage }, { usage },
      { usage: { cost_microdollars: 12345 } },
    ) });
  });
  await page.goto(`/chat?model=${MODEL}`);
  await send(page);
  await expect(page.locator("[data-chat-usage-cost]")).toHaveText("$0.012345");
  await expect(page.locator("[data-chat-usage-tokens]")).toHaveText("1,120 tokens");
  expect(request.stream_options).toEqual({ include_usage: true });
  await page.locator(".chat-usage summary").click();
  await expect(page.locator("[data-chat-usage-cached]")).toHaveText("800");
  await expect(page.locator("[data-chat-usage-reasoning]")).toHaveText("40");
  await expect(page.locator(".chat-msg-meta")).toContainText("$0.012345");
  await page.reload();
  await expect(page.locator("[data-chat-usage-cost]")).toHaveText("$0.012345");
  await send(page);
  await expect(page.locator("[data-chat-usage-cost]")).toHaveText("$0.02469");
  await expect(page.locator("[data-chat-usage-tokens]")).toHaveText("2,240 tokens");
  await page.getByRole("button", { name: "Clear current chat" }).click();
  await page.locator(".chat-prompt-confirm").click();
  await expect(page.locator("[data-chat-usage-cost]")).toHaveText("$0.00");
  await expect(page.locator("[data-chat-usage-tokens]")).toHaveText("0 tokens");
});

for (const [label, payload, expected] of [
  ["zero", { usage: { cost_microdollars: 0 } }, "$0.00"],
  ["USD", { usage: { cost: 0.012345 } }, "$0.012345"],
  ["legacy", { trustedrouter: { cost_microdollars: 42 } }, "$0.000042"],
  ["missing", {}, "Cost unreported"],
  ["invalid", { usage: { cost_microdollars: -1, cost: "oops" } }, "Cost unreported"],
]) {
  test(`usage handles ${label} cost without inventing billing`, async ({ page, context }) => {
    await setup(page, context);
    await page.route("**/v1/chat/completions", (route) => route.fulfill({ contentType: "text/event-stream", body: sse(
      { choices: [{ delta: { content: "Hello world" } }], usage: { prompt_tokens: 8, completion_tokens: 4 } }, payload,
    ) }));
    await page.goto(`/chat?model=${MODEL}`);
    await send(page);
    await expect(page.locator("[data-chat-usage-tokens]")).toHaveText("12 tokens");
    await expect(page.locator("[data-chat-usage-cost]")).toHaveText(expected);
  });
}

test("signed-out send stays local and preserves the draft", async ({ page, context }) => {
  await setup(page, context, false);
  let calls = 0;
  page.on("request", (r) => { if (r.url().includes("/chat/completions")) calls++; });
  await page.goto(`/chat?model=${MODEL}`);
  await send(page);
  await expect(page.getByText("Sign in to send this message.")).toBeVisible();
  expect(calls).toBe(0);
  await expect(page.locator("[data-chat-usage-cost]")).toHaveText("$0.00");
});

test("failed request restores Send so the next request can run", async ({ page, context }) => {
  await setup(page, context);
  await page.route("**/v1/chat/completions", (route) => route.fulfill({ status: 503, json: { error: { message: "upstream unavailable" } } }));
  await page.goto(`/chat?model=${MODEL}`);
  await send(page);
  await expect(page.locator(".chat-msg-error")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send message", exact: true })).toBeVisible();
});

test("thinking and usage arrive before the stream completes", async ({ page, context }) => {
  await setup(page, context);
  let finish;
  const server = http.createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Access-Control-Allow-Origin": "*" });
    res.write('data: {"choices":[{"delta":{"reasoning":"Working through the question."}}]}\n\n');
    res.write('data: {"usage":{"prompt_tokens":100,"completion_tokens":20,"cost_microdollars":25}}\n\n');
    finish = () => res.end(sse({ choices: [{ delta: { content: "Hello world" } }] }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  await page.route("**/v1/chat/completions", (route) => route.continue({ url: `http://127.0.0.1:${server.address().port}/stream` }));
  try {
    await page.goto(`/chat?model=${MODEL}`);
    await send(page);
    await expect(page.locator(".chat-msg-reasoning-body")).toBeVisible();
    await expect(page.locator("[data-chat-usage-cost]")).toHaveText("$0.000025");
    await expect(page.locator(".chat-send.is-stop")).toBeVisible();
    finish();
    await expect(page.locator(".chat-msg-md")).toHaveText("Hello world");
    await expect(page.locator(".chat-msg-reasoning-body")).toHaveText("Working through the question.");
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});

test("user-chat shares the same usage UI and keeps its custom model locked", async ({ page, context }) => {
  await setup(page, context);
  await page.goto("/user-chat?model=trustedrouter/user-testslug");
  await expect(page.locator("[data-chat-usage]")).toBeVisible();
  await expect(page.locator(".chat-model-pill")).toHaveAttribute("title", "trustedrouter/user-testslug");
  await expect(page.locator('[data-action="add-model"]')).toHaveCount(0);
});
