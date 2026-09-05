/**
 * Quiet empty state + suggested-prompt grid. Suggestions only fill the draft.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    clearLocalStorageState,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("Fresh chats show a simple heading without an onboarding panel", async ({ page }) => {
    await page.goto("/chat");
    await clearLocalStorageState(page);
    await page.reload();
    await expect(page.locator(".chat-welcome")).toHaveCount(0);
    await expect(page.locator(".chat-empty h2")).toHaveText("What are we working on?");
});

test("Suggested-prompt grid renders 4-ish cards", async ({ page }) => {
    await page.goto("/chat");
    await clearLocalStorageState(page);
    await page.reload();
    const suggestions = page.locator(".chat-suggest");
    const count = await suggestions.count();
    expect(count).toBeGreaterThanOrEqual(3);
});

test("Clicking a suggestion fills input without sending", async ({ page }) => {
    let inferenceCount = 0;
    await page.route("**/v1/chat/completions", async (route) => {
        inferenceCount++;
        await route.abort();
    });
    await page.goto("/chat");
    await clearLocalStorageState(page);
    await page.reload();
    const firstSuggest = page.locator(".chat-suggest").first();
    const promptText = await firstSuggest.getAttribute("data-prompt");
    await firstSuggest.click();
    const input = page.locator("[data-chat-input]");
    await expect(input).toBeFocused();
    expect(await input.inputValue()).toBe(promptText);
    expect(inferenceCount).toBe(0);
});

test("Welcome banner is gone after dismissed=true seed", async ({ page }) => {
    await page.goto("/chat");
    await page.evaluate(() => {
        localStorage.setItem(
            "tr_chat_state_v1",
            JSON.stringify({
                chats: {},
                activeChatId: null,
                preferences: { welcome_dismissed: true },
            }),
        );
    });
    await page.reload();
    await expect(page.locator(".chat-welcome")).toHaveCount(0);
    // But suggestions still appear on the empty chat
    await expect(page.locator(".chat-suggest").first()).toBeVisible();
});
