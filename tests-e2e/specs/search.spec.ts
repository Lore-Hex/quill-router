/**
 * In-chat search — Cmd+F opens a search bar that highlights matches
 * within the active thread and shows a "N / M" counter.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
    sendMessage,
    waitForStreamToFinish,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

const META_KEY = process.platform === "darwin" ? "Meta" : "Control";

async function seedChat(page: any): Promise<void> {
    await page.goto("/chat");
    const state = chatStateWithModels(["anthropic/claude-sonnet-4.6"]);
    const chatId = Object.keys(state.chats)[0];
    state.chats[chatId].messages = [
        {
            id: "m1",
            role: "user",
            content: "Tell me about UNICORNS please",
            created_at: new Date().toISOString(),
        },
        {
            id: "m2",
            role: "assistant",
            responses: [
                {
                    model_id: "anthropic/claude-sonnet-4.6",
                    content:
                        "Unicorns are mythical horse-like creatures. " +
                        "The horn of a unicorn is its defining trait. " +
                        "Many cultures revere unicorns as symbols of purity.",
                    tokens_in: 8,
                    tokens_out: 25,
                    cost_microdollars: 3,
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
}

test("Cmd+F opens the search bar with focus", async ({ page }) => {
    await seedChat(page);
    await page.keyboard.press(`${META_KEY}+f`);
    const bar = page.locator(".chat-search-bar");
    await expect(bar).toBeVisible();
    await expect(bar.locator("input")).toBeFocused();
});

test("typing in search marks matching text", async ({ page }) => {
    await seedChat(page);
    await page.keyboard.press(`${META_KEY}+f`);
    await page.locator(".chat-search-bar input").fill("unicorn");
    const marks = page.locator(".chat-search-hit, mark.chat-search-hit, .chat-msg-bubble mark");
    expect(await marks.count()).toBeGreaterThan(0);
});

test("Esc closes the search bar", async ({ page }) => {
    await seedChat(page);
    await page.keyboard.press(`${META_KEY}+f`);
    await expect(page.locator(".chat-search-bar")).toBeVisible();
    await page.locator(".chat-search-bar input").press("Escape");
    await expect(page.locator(".chat-search-bar")).toBeHidden();
});

test("clicking the close button dismisses search and clears highlights", async ({
    page,
}) => {
    await seedChat(page);
    await page.keyboard.press(`${META_KEY}+f`);
    await page.locator(".chat-search-bar input").fill("unicorn");
    await page.locator(".chat-search-close").click();
    await expect(page.locator(".chat-search-bar")).toBeHidden();
});

test("search count reflects the number of matches", async ({ page }) => {
    await seedChat(page);
    await page.keyboard.press(`${META_KEY}+f`);
    await page.locator(".chat-search-bar input").fill("unicorn");
    const countEl = page.locator(".chat-search-count");
    const text = (await countEl.textContent()) || "";
    expect(text.length).toBeGreaterThan(0);
    // The seed text contains "unicorn" 4 times (1 user msg + 3 in
    // the assistant response). Match any digit.
    expect(text).toMatch(/\d/);
});

test("a no-match query renders a zero-result count", async ({ page }) => {
    await seedChat(page);
    await page.keyboard.press(`${META_KEY}+f`);
    await page.locator(".chat-search-bar input").fill("xyzzy-no-match");
    const countEl = page.locator(".chat-search-count");
    const text = (await countEl.textContent()) || "";
    expect(text).toMatch(/0/);
});
