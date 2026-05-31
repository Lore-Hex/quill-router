/**
 * Stop button + Esc abort behaviour during streaming.
 *
 * Uses a slow mocked SSE handler (page.route with an artificial
 * delay) so the test has time to observe the Send→Stop transition
 * and abort the stream.
 */
import { test, expect, Page } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
    sendMessage,
} from "../fixtures/helpers";
import { buildSseBody, modelsCatalog } from "../fixtures/sse";

// Install our own slow handler instead of mockExternalApis() so we
// can interleave delays between SSE frames. The handler does the
// same /v1/models + key-issue mocks as the default, plus a slow
// chat/completions.
async function installSlowMocks(page: Page): Promise<void> {
    await page.route("**/v1/models", async (route) => {
        await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(modelsCatalog()),
        });
    });
    await page.route("**/internal/chat/issue-browser-key", async (route) => {
        await route.fulfill({
            status: 200,
            headers: {
                "set-cookie":
                    "tr_chat_key=sk-tr-test-fakekey1234567890; Path=/chat; SameSite=Lax; Max-Age=86400",
                "content-type": "application/json",
            },
            body: JSON.stringify({
                data: {
                    raw_key: "sk-tr-test-fakekey1234567890",
                    expires_at: new Date(
                        Date.now() + 30 * 24 * 3600 * 1000,
                    ).toISOString(),
                    limit_microdollars: 5_000_000,
                    name: "chat-browser-test",
                },
            }),
        });
    });
    await page.route("**/v1/chat/completions", async (route) => {
        // Hold the SSE body open for 4 seconds before responding so the
        // test has time to click Stop or press Esc.
        await new Promise((r) => setTimeout(r, 4_000));
        await route.fulfill({
            status: 200,
            headers: {
                "content-type": "text/event-stream",
                "cache-control": "no-cache",
            },
            body: buildSseBody({
                parts: [{ content: "delayed-token" }],
                promptTokens: 5,
                completionTokens: 1,
            }),
        });
    });
}

test.beforeEach(async ({ context, page, baseURL }) => {
    await plantSignedInHint(context, baseURL!);
});

test("Send button switches to Stop while streaming", async ({ page }) => {
    await installSlowMocks(page);
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Slow request");
    // While the slow handler is still hanging, the button shows Stop
    const sendBtn = page.locator("[data-chat-send]");
    await expect(sendBtn).toHaveAttribute("data-mode", "stop", { timeout: 3000 });
    await expect(sendBtn).toHaveClass(/is-stop/);
});

test("Clicking Stop aborts the in-flight stream", async ({ page }) => {
    await installSlowMocks(page);
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Slow request");
    const sendBtn = page.locator("[data-chat-send]");
    await expect(sendBtn).toHaveAttribute("data-mode", "stop", { timeout: 3000 });
    await sendBtn.click();
    // Back to Send
    await expect(sendBtn).toHaveAttribute("data-mode", "send", { timeout: 3000 });
});

test("Esc aborts the in-flight stream", async ({ page }) => {
    await installSlowMocks(page);
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Slow request");
    const sendBtn = page.locator("[data-chat-send]");
    await expect(sendBtn).toHaveAttribute("data-mode", "stop", { timeout: 3000 });
    await page.keyboard.press("Escape");
    await expect(sendBtn).toHaveAttribute("data-mode", "send", { timeout: 3000 });
});
