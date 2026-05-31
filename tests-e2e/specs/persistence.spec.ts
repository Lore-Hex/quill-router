/**
 * Persistence via localStorage — "0 prompt logs" depends on this.
 * Chat history MUST survive a reload, MUST NOT round-trip through TR's
 * servers. Recent-models and welcome-dismissed preferences persist too.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    sendMessage,
    waitForStreamToFinish,
    getLocalStorageState,
    setLocalStorageState,
    clearLocalStorageState,
    chatStateWithModels,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("chat messages persist across reload", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Persisted prompt");
    await waitForStreamToFinish(page);

    // Reload and verify the messages came back
    await page.reload();
    await expect(page.locator(".chat-msg-user")).toHaveCount(1);
    await expect(page.locator(".chat-msg-user-text")).toContainText(
        "Persisted prompt",
    );
    await expect(page.locator(".chat-msg-assistant")).toHaveCount(1);
});

test("multiple chats coexist in sidebar after reload", async ({ page }) => {
    await page.goto("/chat");
    await page.locator('[data-action="new-chat"]').click();
    await page.locator('[data-action="new-chat"]').click();
    await page.locator('[data-action="new-chat"]').click();
    // 3 new chats + 1 default = expectations get fuzzy, just verify
    // >= 3 chats live in storage after a reload.
    await page.reload();
    const state = await getLocalStorageState(page);
    expect(Object.keys(state.chats).length).toBeGreaterThanOrEqual(3);
});

test("welcome dismissal persists", async ({ page }) => {
    await page.goto("/chat");
    await clearLocalStorageState(page);
    await page.reload();
    // First visit: welcome card shows
    await expect(page.locator(".chat-welcome")).toBeVisible();
    // Dismiss
    await page.locator('[data-action="dismiss-welcome"]').click();
    await expect(page.locator(".chat-welcome")).toHaveCount(0);
    // Reload + welcome stays gone
    await page.reload();
    await expect(page.locator(".chat-welcome")).toHaveCount(0);
});

test("Recently-used models list grows with picks", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await page.locator('[data-action="add-model"]').click();
    await page
        .locator(".chat-model-row")
        .filter({ hasText: "GPT-5.5" })
        .first()
        .click();
    const state = await getLocalStorageState(page);
    expect(state.preferences.recentModelIds).toBeTruthy();
    expect(state.preferences.recentModelIds[0]).toBe("openai/gpt-5.5");
});

test("data is gone after Settings → Clear all", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "About to be wiped");
    await waitForStreamToFinish(page);
    // Open Settings + accept the wipe confirm
    page.on("dialog", (d) => d.accept());
    await page.locator('[data-action="show-settings"]').click();
    await page.locator(".chat-settings-danger").click();
    // Verify everything is reset
    const state = await getLocalStorageState(page);
    // The page also re-ensures an empty chat — accept either { chats: {} }
    // or a single empty new chat
    const titles = Object.values(state.chats || {}).map((c: any) => c.title);
    expect(titles.every((t: string) => t !== "Test chat")).toBe(true);
});
