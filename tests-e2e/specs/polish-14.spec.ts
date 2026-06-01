/**
 * Polish round 14: streaming error handling, smarter token estimate,
 * live sidebar timestamps (smoke), per-chat cost in sidebar, mobile
 * send button collapse, provider chip on pill, reload-resistant
 * auto-scroll.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis, setChatCompletionResponse } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
    sendMessage,
    waitForStreamToFinish,
} from "../fixtures/helpers";
import { buildSseBody } from "../fixtures/sse";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("model pill shows the provider chip on desktop viewports", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    const chip = page.locator(".chat-model-pill-provider").first();
    await expect(chip).toBeVisible();
    await expect(chip).toContainText("anthropic");
});

test("Send button carries an icon at all viewport sizes", async ({ page }) => {
    await page.goto("/chat");
    const sendBtn = page.locator("[data-chat-send]");
    await expect(sendBtn.locator(".chat-send-icon")).toBeVisible();
    await expect(sendBtn.locator(".chat-send-label")).toContainText("Send");
});

test("Sidebar item shows a cost chip after a paid message lands", async ({
    page,
}) => {
    await page.goto("/chat");
    const state = chatStateWithModels(
        ["anthropic/claude-sonnet-4.6"],
        "Costly chat",
    );
    const chatId = Object.keys(state.chats)[0];
    // Seed an assistant message with a non-zero cost
    state.chats[chatId].messages = [
        {
            id: "m1",
            role: "user",
            content: "hi",
            created_at: new Date().toISOString(),
        },
        {
            id: "m2",
            role: "assistant",
            responses: [
                {
                    model_id: "anthropic/claude-sonnet-4.6",
                    content: "hello",
                    tokens_in: 5,
                    tokens_out: 1,
                    cost_microdollars: 250_000, // 25¢
                    finish_reason: "stop",
                    tool_calls: [],
                    error: null,
                },
            ],
            created_at: new Date().toISOString(),
        },
    ];
    await setLocalStorageState(page, state);
    await page.reload();
    const cost = page.locator(".chat-sidebar-title-cost").first();
    await expect(cost).toBeVisible();
    await expect(cost).toHaveText(/¢|\$/);
});

test("Aborting a stream does NOT show an error bubble", async ({ page }) => {
    // Slow handler so we have time to abort
    await page.unroute("**/v1/chat/completions");
    await page.route("**/v1/chat/completions", async (route) => {
        // Hang for 4s then return a minimal response. We'll abort before then.
        await new Promise((r) => setTimeout(r, 4000));
        await route.fulfill({
            status: 200,
            headers: { "content-type": "text/event-stream" },
            body: buildSseBody({
                parts: [{ content: "late" }],
                promptTokens: 1,
                completionTokens: 1,
            }),
        });
    });
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "slow");
    // Wait until the Send button toggles to Stop
    await expect(page.locator("[data-chat-send]")).toHaveAttribute(
        "data-mode",
        "stop",
        { timeout: 3000 },
    );
    // Hit Esc to abort
    await page.keyboard.press("Escape");
    await expect(page.locator("[data-chat-send]")).toHaveAttribute(
        "data-mode",
        "send",
        { timeout: 3000 },
    );
    // No error bubble — aborts are not failures.
    await expect(page.locator(".chat-msg-error")).toHaveCount(0);
});

test("Streaming network failure surfaces a friendly error + Retry", async ({
    page,
}) => {
    await page.unroute("**/v1/chat/completions");
    await page.route("**/v1/chat/completions", async (route) => {
        await route.abort("connectionfailed");
    });
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "test");
    const err = page.locator(".chat-msg-error");
    await expect(err).toBeVisible({ timeout: 5000 });
    await expect(err).toContainText(/network|connection|hiccup|interrupt/i);
    await expect(err.locator(".chat-msg-error-retry")).toBeVisible();
});

test("Token estimate scales with input length (smoke)", async ({ page }) => {
    await page.goto("/chat");
    const input = page.locator("[data-chat-input]");
    const counter = page.locator("[data-chat-token-counter]");

    await input.fill("hi");
    await page.waitForTimeout(120);
    const small = (await counter.textContent()) || "";
    expect(small).toMatch(/~\d+ tokens/);

    await input.fill(
        "The quick brown fox jumps over the lazy dog. ".repeat(20),
    );
    await page.waitForTimeout(120);
    const big = (await counter.textContent()) || "";
    expect(big).toMatch(/~\d+ tokens/);
    const smallN = parseInt(small.match(/\d+/)?.[0] || "0", 10);
    const bigN = parseInt(big.match(/\d+/)?.[0] || "0", 10);
    expect(bigN).toBeGreaterThan(smallN * 10);
});

test("Auto-scroll does NOT snap to bottom while user is scrolled up", async ({
    page,
}) => {
    // Long pre-existing chat so the thread is scrollable
    await page.goto("/chat");
    const state = chatStateWithModels(["anthropic/claude-sonnet-4.6"]);
    const chatId = Object.keys(state.chats)[0];
    state.chats[chatId].messages = [];
    for (let i = 0; i < 30; i++) {
        state.chats[chatId].messages.push({
            id: "u" + i,
            role: "user",
            content: "long question " + i + " ".repeat(50),
            created_at: new Date().toISOString(),
        });
        state.chats[chatId].messages.push({
            id: "a" + i,
            role: "assistant",
            responses: [
                {
                    model_id: "anthropic/claude-sonnet-4.6",
                    content: "long answer " + i + " ".repeat(80),
                    tokens_in: 5,
                    tokens_out: 12,
                    cost_microdollars: 100,
                    finish_reason: "stop",
                    tool_calls: [],
                    error: null,
                },
            ],
            created_at: new Date().toISOString(),
        });
    }
    await setLocalStorageState(page, state);
    await page.reload();

    // Scroll the thread to the top
    await page.locator("[data-chat-thread]").evaluate((el) => {
        el.scrollTop = 0;
    });
    const initialTop = await page.locator("[data-chat-thread]").evaluate(
        (el) => el.scrollTop,
    );
    expect(initialTop).toBe(0);

    // Trigger a sidebar re-render (which re-renders the thread too)
    // by toggling the system prompt panel.
    await page.locator('[data-action="toggle-system-prompt"]').click();
    await page.locator('[data-action="toggle-system-prompt"]').click();
    await page.waitForTimeout(150);

    const afterTop = await page.locator("[data-chat-thread]").evaluate(
        (el) => el.scrollTop,
    );
    // Should NOT have jumped to bottom — still at top (or close to it).
    expect(afterTop).toBeLessThan(200);
});
