/**
 * Keyboard shortcuts the chat client wires up:
 *   * ⌘/Ctrl+Enter      send
 *   * ⌘/Ctrl+N          new chat
 *   * ⌘/Ctrl+E          export JSON
 *   * ⌘/Ctrl+F          open in-chat search
 *   * ⌘/Ctrl+J / K      next / prev chat
 *   * /                 focus input
 *   * K                 add model
 *   * Esc               stop streams / close menus
 *   * ?                 show help overlay
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    waitForStreamToFinish,
    setLocalStorageState,
    chatStateWithModels,
    getLocalStorageState,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

const META_KEY = process.platform === "darwin" ? "Meta" : "Control";

test("Cmd+Enter from the input sends the message", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await page.locator("[data-chat-input]").fill("Shortcut test");
    await page.keyboard.press(`${META_KEY}+Enter`);
    await waitForStreamToFinish(page);
    await expect(page.locator(".chat-msg-user-text")).toContainText(
        "Shortcut test",
    );
});

test("Cmd+N creates a new chat", async ({ page }) => {
    await page.goto("/chat");
    const before = await page.locator(".chat-sidebar-item").count();
    await page.keyboard.press(`${META_KEY}+n`);
    const after = await page.locator(".chat-sidebar-item").count();
    expect(after).toBeGreaterThan(before);
});

test("Cmd+J switches to the next chat, Cmd+K to previous", async ({ page }) => {
    await page.goto("/chat");
    // Create 3 chats so navigation has somewhere to go
    for (let i = 0; i < 3; i++) {
        await page.locator('[data-action="new-chat"]').click();
    }
    const initial = (await getLocalStorageState(page)).activeChatId;
    await page.keyboard.press(`${META_KEY}+j`);
    const afterJ = (await getLocalStorageState(page)).activeChatId;
    expect(afterJ).not.toBe(initial);
    await page.keyboard.press(`${META_KEY}+k`);
    const afterK = (await getLocalStorageState(page)).activeChatId;
    expect(afterK).toBe(initial);
});

test("`?` opens the keyboard-help overlay", async ({ page }) => {
    await page.goto("/chat");
    // Click outside any input to ensure focus is at body
    await page.locator(".chat-thread").click();
    await page.keyboard.press("?");
    await expect(page.locator(".chat-help-overlay")).toBeVisible();
});

test("Cmd+F opens the in-chat search bar", async ({ page }) => {
    await page.goto("/chat");
    await page.keyboard.press(`${META_KEY}+f`);
    await expect(page.locator(".chat-search-bar")).toBeVisible();
});

test("`/` focuses the chat input", async ({ page }) => {
    await page.goto("/chat");
    // Click outside the input first so it's not focused
    await page.locator(".chat-thread").click();
    await page.keyboard.press("/");
    await expect(page.locator("[data-chat-input]")).toBeFocused();
});

test("`K` triggers Add Model when input isn't focused", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await page.locator(".chat-thread").click(); // unfocus the input
    await page.keyboard.press("k");
    // Picker should open
    await expect(page.locator(".chat-model-picker-panel")).toBeVisible();
});
