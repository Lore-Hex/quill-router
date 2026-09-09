const { expect, test } = require("@playwright/test");
const controls = require("../../src/trusted_router/static/chat_controls.js");
const http = require("node:http");

const MODEL = "z-ai/glm-5.2";
const endpoints = [
  { provider: "zai", provider_name: "Z.ai", supported_parameters: ["temperature", "seed"], trustedrouter: { reasoning_modes: ["off", "on"] } },
  { provider: "novita", provider_name: "Novita", supported_parameters: ["temperature"], trustedrouter: { reasoning_modes: [] } },
];
const sse = (text) => 'data: ' + JSON.stringify({ choices: [{ delta: { content: text, reasoning: "Original reasoning" } }], usage: { prompt_tokens: 10, completion_tokens: 5, cost_microdollars: 7 } }) + '\n\ndata: [DONE]\n\n';

test("request controls migrate legacy routing, preserve zero, and never relax a pin", () => {
  expect(controls.preferences({ sort_by: "cost" })).toEqual({ sort: "price" });
  expect(controls.preferences({ sort_by: "uptime" })).toEqual({});
  expect(controls.overrides({ params: {} }, [])).toEqual({});
  const slot = { params: { seed: 0 }, reasoning_mode: "off", provider_preferences: { only: ["zai"], allow_fallbacks: true, sort_by: "latency" } };
  expect(controls.overrides(slot, endpoints)).toEqual({ seed: 0, reasoning: { enabled: false }, provider: { only: ["zai"], allow_fallbacks: false, sort: "latency" } });
  expect(slot.provider_preferences.allow_fallbacks).toBe(true);
  expect(() => controls.overrides(slot, [])).toThrow(/pinned provider/);
  expect(() => controls.overrides({ ...slot, provider_preferences: { only: ["novita"] } }, endpoints)).toThrow(/pinned provider/);
  expect(() => controls.overrides({ params: {}, reasoning_mode: "on" }, [])).toThrow(/No provider supports/);
});

for (const seed of [-1, 1.5, 2147483648, NaN, Infinity, "7"]) {
  test(`invalid seed ${seed} cannot be silently sent or discarded`, () => {
    expect(() => controls.overrides({ params: { seed } }, endpoints)).toThrow(/Seed must/);
  });
}

test("local edits retain billing/provenance, reject blanks, and remove stale generated reasoning", () => {
  const original = { content: "Original", reasoning: "Secret reasoning", tool_calls: [{}], cost_microdollars: 7, tokens_in: 10, tokens_out: 5, selected_provider: "zai" };
  const response = structuredClone(original);
  expect(() => controls.editResponse(response, " \n ", "now")).toThrow();
  expect(response).toEqual(original);
  expect(controls.editResponse(response, "Original", "now")).toBe(false);
  expect(controls.editResponse(response, "Revised", "now")).toBe(true);
  expect(response).toEqual({ content: "Revised", cost_microdollars: 7, tokens_in: 10, tokens_out: 5, selected_provider: "zai", edited_at: "now" });
});

test("legacy duplicate columns get distinct identities and existing identities never change", () => {
  const chat = {
    models: [{ slot_id: "a", model_id: MODEL }, { slot_id: "b", model_id: MODEL }],
    messages: [{ responses: [{ model_id: MODEL, content: "First" }, { model_id: MODEL, content: "Second" }] }],
  };
  controls.linkLegacyResponses(chat);
  expect(chat.messages[0].responses.map((r) => r.slot_id)).toEqual(["a", "b"]);
  chat.models.reverse();
  controls.linkLegacyResponses(chat);
  expect(chat.messages[0].responses.map((r) => r.slot_id)).toEqual(["a", "b"]);
});

async function setup(page, context) {
  await context.addCookies([{ name: "tr_signed_in", value: "1", url: "http://127.0.0.1:18081" }]);
  await page.route("**/auth/session", (route) => route.fulfill({ json: { data: { user: { id: "test" }, workspace: { id: "workspace" } } } }));
  await page.route("**/internal/chat/issue-browser-key", (route) => route.fulfill({ json: { data: { raw_key: "sk-tr-test-only" } } }));
  await page.route(/\/v1\/models(?:\/picker)?$/, (route) => route.fulfill({ json: { data: [{ id: MODEL, name: "GLM 5.2", context_length: 10000, pricing: {} }] } }));
  await page.route("**/v1/models/*/*/endpoints", (route) => route.fulfill({ json: { data: endpoints } }));
  const requests = [];
  await page.route("**/v1/chat/completions", (route) => {
    requests.push(route.request().postDataJSON());
    return route.fulfill({ contentType: "text/event-stream", body: sse("Original answer") });
  });
  await page.goto(`/chat?model=${MODEL}`);
  return requests;
}

async function send(page, text = "Hello") {
  await page.getByRole("textbox", { name: "Chat input" }).fill(text);
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.locator(".chat-msg-md").last()).toHaveText("Original answer");
  await expect(page.getByRole("button", { name: "Send message", exact: true })).toBeVisible();
}

async function openControls(page) {
  await page.locator(".chat-model-pill").first().click();
  await expect(page.getByLabel("Provider", { exact: true })).toBeEnabled();
}

test("reasoning off, provider pin, seed and temperature survive reload and reach the request", async ({ page, context }) => {
  const requests = await setup(page, context);
  await openControls(page);
  await page.getByLabel("Provider", { exact: true }).selectOption("zai");
  await page.getByLabel("Reasoning", { exact: true }).selectOption("off");
  await page.getByLabel("Seed (optional)").fill("0");
  await page.getByLabel("Seed (optional)").press("Tab");
  await page.locator('[data-param="temperature"]').focus();
  await page.locator('[data-param="temperature"]').press("Home");
  await page.reload();
  await openControls(page);
  await expect(page.getByLabel("Provider", { exact: true })).toHaveValue("zai");
  await expect(page.getByLabel("Reasoning", { exact: true })).toHaveValue("off");
  await expect(page.getByLabel("Seed (optional)")).toHaveValue("0");
  await page.getByRole("textbox", { name: "Chat input" }).click();
  await send(page);
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({ model: MODEL, temperature: 0, seed: 0, reasoning: { enabled: false }, provider: { only: ["zai"], allow_fallbacks: false } });
  expect(requests[0].provider).not.toHaveProperty("sort_by");
});

test("unsupported route disables reasoning choices and cannot silently ignore a saved Off", async ({ page, context }) => {
  const requests = await setup(page, context);
  await openControls(page);
  await page.getByLabel("Reasoning", { exact: true }).selectOption("off");
  await page.getByLabel("Provider", { exact: true }).selectOption("novita");
  await expect(page.locator('[data-action="set-reasoning"] option[value="off"]')).toHaveJSProperty("disabled", true);
  await page.getByRole("textbox", { name: "Chat input" }).fill("Hello");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.locator(".chat-msg-error")).toContainText("pinned provider");
  expect(requests).toHaveLength(0);
});

test("a pinned provider failure does not trigger a second request to a different provider", async ({ page, context }) => {
  await setup(page, context);
  await openControls(page);
  await page.getByLabel("Provider", { exact: true }).selectOption("zai");
  let calls = 0;
  await page.route("**/v1/chat/completions", (route) => {
    calls++;
    expect(route.request().postDataJSON().provider).toEqual({ only: ["zai"], allow_fallbacks: false });
    return route.fulfill({ status: 503, json: { error: { message: "Pinned provider unavailable" } } });
  });
  await page.getByRole("textbox", { name: "Chat input" }).fill("Hello");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.locator(".chat-msg-error")).toContainText("Pinned provider unavailable");
  expect(calls).toBe(1);
});

test("saved controls fail closed when provider capability lookup is unavailable", async ({ page, context }) => {
  const requests = await setup(page, context);
  await openControls(page);
  await page.getByLabel("Reasoning", { exact: true }).selectOption("off");
  await page.route("**/v1/models/*/*/endpoints", (route) => route.fulfill({ status: 503 }));
  await page.reload();
  await page.getByRole("textbox", { name: "Chat input" }).fill("Hello");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.locator(".chat-msg-error")).toContainText("Provider settings are unavailable");
  expect(requests).toHaveLength(0);
});

test("invalid entered seed blocks sending rather than using an old seed", async ({ page, context }) => {
  const requests = await setup(page, context);
  await openControls(page);
  await page.getByLabel("Seed (optional)").fill("-1");
  await page.getByLabel("Seed (optional)").press("Tab");
  await page.locator(".chat-model-pill").first().click();
  await page.getByRole("textbox", { name: "Chat input" }).fill("Hello");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.locator(".chat-msg-error")).toContainText("Seed must be a whole number");
  expect(requests).toHaveLength(0);
});

test("regenerating an edited reply clears its edit marker and uses the same provider settings", async ({ page, context }) => {
  const requests = await setup(page, context);
  await openControls(page);
  await page.getByLabel("Provider", { exact: true }).selectOption("zai");
  await page.getByLabel("Reasoning", { exact: true }).selectOption("off");
  await page.locator(".chat-model-pill").first().click();
  await send(page);
  await page.getByRole("button", { name: "Edit response", exact: true }).click();
  await page.getByRole("textbox", { name: "Edit assistant response" }).fill("Revised answer");
  await page.getByRole("button", { name: "Save edit", exact: true }).click();
  await page.getByRole("button", { name: "Regenerate", exact: true }).click();
  await expect(page.locator(".chat-msg-md")).toHaveText("Original answer");
  await expect(page.locator(".chat-msg-edited")).toHaveCount(0);
  expect(requests).toHaveLength(2);
  expect(requests[1]).toMatchObject({ provider: { only: ["zai"], allow_fallbacks: false }, reasoning: { enabled: false }, messages: [{ role: "user", content: "Hello" }] });
});

test("editing is local, preserves cost, persists, and changes subsequent assistant history", async ({ page, context }) => {
  const requests = await setup(page, context);
  await send(page);
  await page.getByRole("button", { name: "Edit response", exact: true }).click();
  await page.getByRole("textbox", { name: "Edit assistant response" }).fill("Revised answer");
  await page.getByRole("button", { name: "Save edit", exact: true }).click();
  expect(requests).toHaveLength(1);
  await expect(page.locator(".chat-msg-edited")).toHaveText("Edited");
  await expect(page.locator(".chat-msg-reasoning")).toHaveCount(0);
  await expect(page.locator("[data-chat-usage-cost]")).toHaveText("$0.000007");
  await page.reload();
  await expect(page.locator(".chat-msg-md")).toHaveText("Revised answer");
  await send(page, "Continue using that answer");
  expect(requests).toHaveLength(2);
  expect(requests[1].messages).toEqual([{ role: "user", content: "Hello" }, { role: "assistant", content: "Revised answer" }, { role: "user", content: "Continue using that answer" }]);
  expect(requests[1]).not.toHaveProperty("reasoning");
});

test("cancel, Escape, empty edits, and pending-edit sends do not change history or call inference", async ({ page, context }) => {
  const requests = await setup(page, context);
  await send(page);
  for (const cancel of ["button", "escape"]) {
    await page.getByRole("button", { name: "Edit response", exact: true }).click();
    await page.getByRole("textbox", { name: "Edit assistant response" }).fill("Not saved");
    if (cancel === "button") await page.getByRole("button", { name: "Cancel", exact: true }).click();
    else await page.getByRole("textbox", { name: "Edit assistant response" }).press("Escape");
    await expect(page.locator(".chat-msg-md")).toHaveText("Original answer");
  }
  await page.getByRole("button", { name: "Edit response", exact: true }).click();
  await page.getByRole("textbox", { name: "Edit assistant response" }).fill(" \n ");
  await page.getByRole("button", { name: "Save edit", exact: true }).click();
  await expect(page.locator("[data-assistant-editor]")).toBeVisible();
  await page.getByRole("textbox", { name: "Chat input" }).fill("Next");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  expect(requests).toHaveLength(1);
  await expect(page.locator("[data-chat-usage-cost]")).toHaveText("$0.000007");
});

test("duplicate model slots keep separate edited histories", async ({ page, context }) => {
  const requests = await setup(page, context);
  await openControls(page);
  await page.getByRole("button", { name: "Duplicate", exact: true }).click();
  await send(page);
  await expect(page.locator(".chat-msg-md")).toHaveCount(2);
  await page.getByRole("button", { name: "Edit response", exact: true }).nth(1).click();
  await page.getByRole("textbox", { name: "Edit assistant response" }).fill("Second column revised");
  await page.getByRole("button", { name: "Save edit", exact: true }).click();
  await send(page, "Next");
  expect(requests).toHaveLength(4);
  expect(requests.slice(2).map((r) => r.messages.find((m) => m.role === "assistant").content).sort()).toEqual(["Original answer", "Second column revised"]);
});

test("reasoning controls and editing fit desktop and mobile; edited markup is sanitized", async ({ page, context }, info) => {
  await setup(page, context);
  await send(page);
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 850 });
    await openControls(page);
    await expect(page.getByLabel("Reasoning", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    const box = await page.locator(".chat-model-dropdown").boundingBox();
    expect(box.x + box.width).toBeLessThanOrEqual(width + 1);
    await page.screenshot({ path: info.outputPath(`controls-${width}.png`) });
    await page.locator(".chat-model-pill").first().click();
    await page.getByRole("button", { name: "Edit response", exact: true }).click();
    await page.getByRole("textbox", { name: "Edit assistant response" }).fill('<img src=x onerror="window.editXss=true"> Revised');
    await page.screenshot({ path: info.outputPath(`edit-${width}.png`) });
    await page.getByRole("button", { name: "Save edit", exact: true }).click();
    expect(await page.evaluate(() => window.editXss)).toBeUndefined();
    expect(await page.locator(".chat-msg-md [onerror]").count()).toBe(0);
  }
});

test("editing is blocked while a provider is streaming", async ({ page, context }) => {
  await setup(page, context);
  let finish;
  const server = http.createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Access-Control-Allow-Origin": "*" });
    res.write('data: {"choices":[{"delta":{"content":"Partial"}}]}\n\n');
    finish = () => res.end("data: [DONE]\n\n");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  await page.route("**/v1/chat/completions", (route) => route.continue({ url: `http://127.0.0.1:${server.address().port}/stream` }));
  try {
    await page.getByRole("textbox", { name: "Chat input" }).fill("Hello");
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await expect(page.locator(".chat-msg-md")).toHaveText("Partial");
    // This partial response is updated in place; no edit action exists yet.
    await expect(page.getByRole("button", { name: "Edit response", exact: true })).toHaveCount(0);
    finish();
    await expect(page.getByRole("button", { name: "Edit response", exact: true })).toBeEnabled();
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});
