const { test, expect } = require("@playwright/test");

async function activationPage(page, responder) {
  const events = [];
  const calls = [];
  await page.route("**/analytics/events", async (route) => {
    events.push(route.request().postDataJSON());
    await route.fulfill({ status: 204 });
  });
  await page.route("**/chat-proxy/v1/chat/completions", async (route) => {
    calls.push({ body: route.request().postDataJSON(), headers: route.request().headers() });
    await responder(route, calls.length);
  });
  await page.route("**/onboarding-regression", (route) => route.fulfill({
    contentType: "text/html",
    body: `<html><head><script defer src="/static/console.js"></script></head><body>
      <div data-first-call-flow data-endpoint="/chat-proxy/v1/chat/completions" data-key-source="test-key">
        <code id="test-key">sk-tr-test-only-never-real</code>
        <button data-action="run-first-call"><span data-run-label>Run my first API request</span></button>
        <div data-call-error hidden><strong data-call-error-title></strong><p data-call-error-message></p><a data-call-error-action hidden></a></div>
        <div data-call-result hidden><span data-result-output></span><span data-result-model></span><span data-result-provider></span><span data-result-latency></span><span data-result-cost></span></div>
      </div></body></html>`,
  }));
  await page.goto("/onboarding-regression");
  return { events, calls };
}

test("onboarding budget survives reasoning and emits a paired visible-answer success", async ({ page }) => {
  const { events, calls } = await activationPage(page, (route) => {
    const budget = route.request().postDataJSON().max_tokens;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({
      model: "test/reasoning-model", choices: [{ message: { content: budget >= 128 ? "PONG" : "", reasoning_content: "private reasoning" }, finish_reason: "length" }],
    }) });
  });
  await page.getByRole("button").click();
  await expect(page.locator("[data-call-result]")).toBeVisible();
  await expect(page.locator("[data-result-output]")).toHaveText("PONG");
  await expect.poll(() => events.length).toBe(2);
  expect(calls[0].body.max_tokens).toBe(512);
  expect(events.map((e) => e.event)).toEqual(["onboarding_call_started", "onboarding_call_succeeded"]);
  expect(events[0].attempt_id).toMatch(/^[a-f0-9-]{36}$/);
  expect(events[1].attempt_id).toBe(events[0].attempt_id);
  expect(calls[0].headers["x-request-id"]).toBe(events[0].attempt_id);
  expect(events[1].http_status).toBe(200);
  expect(events[1].elapsed_ms).toBeGreaterThanOrEqual(0);
  expect(JSON.stringify(events)).not.toMatch(/sk-tr|private reasoning|PONG/);
});

for (const [name, response, reason, status] of [
  ["reasoning exhaustion", { status: 200, body: JSON.stringify({ choices: [{ message: { content: "", reasoning_content: "secret" }, finish_reason: "length" }] }) }, "output_budget_exhausted", 200],
  ["empty output", { status: 200, body: JSON.stringify({ choices: [{ message: { content: "" }, finish_reason: "stop" }] }) }, "empty_output", 200],
  ["malformed JSON", { status: 200, body: "not JSON" }, "invalid_response", 200],
  ["HTTP failure", { status: 402, body: "private upstream diagnostic" }, "http_error", 402],
]) {
  test(`onboarding classifies ${name} and retries with a new attempt`, async ({ page }) => {
    const { events } = await activationPage(page, (route, n) => route.fulfill(n === 1
      ? { contentType: "application/json", ...response }
      : { contentType: "application/json", body: JSON.stringify({ choices: [{ message: { content: "PONG" } }] }) }));
    await page.getByRole("button").click();
    await expect(page.locator("[data-call-error]")).toBeVisible();
    await expect.poll(() => events.length).toBe(2);
    expect(events[1]).toMatchObject({ event: "onboarding_call_failed", attempt_id: events[0].attempt_id, failure_reason: reason, http_status: status });
    expect(events[1].elapsed_ms).toBeGreaterThanOrEqual(0);
    await page.getByRole("button").click();
    await expect(page.locator("[data-call-result]")).toBeVisible();
    await expect.poll(() => events.length).toBe(4);
    expect(events[2].attempt_id).not.toBe(events[0].attempt_id);
    expect(events[3]).toMatchObject({ event: "onboarding_call_succeeded", attempt_id: events[2].attempt_id });
    expect(JSON.stringify(events)).not.toMatch(/secret|private upstream|not JSON|sk-tr/);
  });
}

test("onboarding reports a network failure once", async ({ page }) => {
  const { events } = await activationPage(page, (route) => route.abort("failed"));
  await page.getByRole("button").click();
  await expect.poll(() => events.length).toBe(2);
  expect(events[1]).toMatchObject({ event: "onboarding_call_failed", failure_reason: "network_error", http_status: 0, attempt_id: events[0].attempt_id });
});

test("onboarding timeout restores the button and emits one outcome", async ({ page }) => {
  await page.clock.install();
  const { events, calls } = await activationPage(page, async () => {});
  await page.getByRole("button").click();
  await expect.poll(() => calls.length).toBe(1);
  await page.clock.fastForward(75001);
  await expect.poll(() => events.length).toBe(2);
  expect(events[1]).toMatchObject({ event: "onboarding_call_failed", failure_reason: "timeout", attempt_id: events[0].attempt_id });
  await expect(page.getByRole("button")).toBeEnabled();
});

test("missing key has a paired failure without sending inference", async ({ page }) => {
  const { events, calls } = await activationPage(page, (route) => route.abort());
  await page.locator("#test-key").evaluate((node) => { node.textContent = ""; });
  await page.getByRole("button").click();
  await expect.poll(() => events.length).toBe(2);
  expect(calls).toHaveLength(0);
  expect(events[1]).toMatchObject({ event: "onboarding_call_failed", attempt_id: events[0].attempt_id, failure_reason: "missing_key", http_status: 0 });
});

test("telemetry delivery failure does not fail a visible answer", async ({ page }) => {
  const { calls } = await activationPage(page, (route) => route.fulfill({
    contentType: "application/json", body: JSON.stringify({ choices: [{ message: { content: "PONG" } }] }),
  }));
  await page.route("**/analytics/events", (route) => route.abort("failed"));
  await page.getByRole("button").click();
  await expect(page.locator("[data-result-output]")).toHaveText("PONG");
  await expect(page.locator("[data-call-error]")).toBeHidden();
  expect(calls).toHaveLength(1);
});
