/**
 * Single-model signed-in flow:
 *   1. Plant the signed-in hint cookie
 *   2. Send a prompt
 *   3. /internal/chat/issue-browser-key fires once → returns fake key
 *   4. /v1/chat/completions fires with the right Authorization header
 *   5. Streamed tokens render in the bubble
 *   6. Cost + usage meta appear after [DONE]
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis, FAKE_BROWSER_KEY } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    sendMessage,
    waitForStreamToFinish,
    setLocalStorageState,
} from "../fixtures/helpers";
import { chatStateWithModels } from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("Send fires key-issue then chat/completions with the issued key", async ({
    page,
}) => {
    const keyIssueCalls: string[] = [];
    const completionsCalls: { authHeader: string | null }[] = [];
    page.on("request", (req) => {
        if (req.url().includes("/internal/chat/issue-browser-key")) {
            keyIssueCalls.push(req.url());
        }
        if (req.url().includes("/v1/chat/completions")) {
            completionsCalls.push({
                authHeader: req.headers()["authorization"] ?? null,
            });
        }
    });

    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Hello");
    await waitForStreamToFinish(page);

    expect(keyIssueCalls.length).toBe(1);
    expect(completionsCalls.length).toBe(1);
    expect(completionsCalls[0].authHeader).toBe("Bearer " + FAKE_BROWSER_KEY);
});

test("streamed tokens render in the assistant bubble", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Say hello");
    await waitForStreamToFinish(page);

    const lastAssistant = page.locator(".chat-msg-assistant").last();
    await expect(lastAssistant.locator(".chat-msg-md")).toContainText(
        "Hello world!",
    );
});

test("user message bubble carries the prompt text", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "What's the time?");
    const userBubble = page.locator(".chat-msg-user").last();
    await expect(userBubble.locator(".chat-msg-user-text")).toHaveText(
        "What's the time?",
    );
});

test("cost and token meta render after stream completes", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Hi");
    await waitForStreamToFinish(page);

    const lastAssistant = page.locator(".chat-msg-assistant").last();
    const meta = lastAssistant.locator(".chat-msg-meta");
    await expect(meta).toBeVisible();
    await expect(meta).toContainText("$");
    await expect(meta).toContainText("in");
    await expect(meta).toContainText("out");
});

test("Sending second message preserves the conversation history", async ({
    page,
}) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "First");
    await waitForStreamToFinish(page);
    await sendMessage(page, "Second");
    await waitForStreamToFinish(page);

    // 2 user messages + 2 assistant messages
    await expect(page.locator(".chat-msg-user")).toHaveCount(2);
    await expect(page.locator(".chat-msg-assistant")).toHaveCount(2);
});
