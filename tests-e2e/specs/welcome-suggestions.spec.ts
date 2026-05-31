/**
 * First-visit welcome banner + suggested-prompt grid.
 *
 *  - Welcome banner shows on a brand-new install
 *  - Dismiss persists welcome_dismissed=true
 *  - Suggested prompt buttons fill the input on click (do NOT send)
 *  - When welcome_dismissed=true, banner is gone but suggestions
 *    still render on an empty chat
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    getLocalStorageState,
    clearLocalStorageState,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("Welcome banner renders on a fresh install", async ({ page }) => {
    await page.goto("/chat");
    await clearLocalStorageState(page);
    await page.reload();
    await expect(page.locator(".chat-welcome")).toBeVisible();
    await expect(page.locator(".chat-welcome h3")).toContainText(
        "Compare models",
    );
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

test("Dismissing the welcome banner persists to preferences", async ({
    page,
}) => {
    await page.goto("/chat");
    await clearLocalStorageState(page);
    await page.reload();
    await page.locator(".chat-welcome-close").click();
    await expect(page.locator(".chat-welcome")).toHaveCount(0);
    const state = await getLocalStorageState(page);
    expect(state.preferences.welcome_dismissed).toBe(true);
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
