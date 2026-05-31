/**
 * Sidebar — chat list management.
 *   * New chat button creates one
 *   * Search filters by title + message content
 *   * Pin / Unpin reorders into PINNED bucket
 *   * Delete removes a chat (with confirm)
 *   * Double-click title renames (prompt)
 *   * Date buckets render correctly
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    getLocalStorageState,
    chatStateWithModels,
    sendMessage,
    waitForStreamToFinish,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("New chat button creates an empty chat", async ({ page }) => {
    await page.goto("/chat");
    const before = await page.locator(".chat-sidebar-item").count();
    await page.locator('[data-action="new-chat"]').click();
    const after = await page.locator(".chat-sidebar-item").count();
    expect(after).toBeGreaterThanOrEqual(before + 1);
});

test("Sidebar search filters by chat title", async ({ page }) => {
    await page.goto("/chat");
    // Seed two chats with distinct titles
    const stateA = chatStateWithModels(["anthropic/claude-sonnet-4.6"], "Apple");
    const apple = stateA.chats[Object.keys(stateA.chats)[0]];
    const stateB = chatStateWithModels(["anthropic/claude-sonnet-4.6"], "Banana");
    const banana = stateB.chats[Object.keys(stateB.chats)[0]];
    await setLocalStorageState(page, {
        chats: { [apple.id]: apple, [banana.id]: banana },
        activeChatId: apple.id,
        preferences: { welcome_dismissed: true },
    });
    await page.reload();
    await expect(page.locator(".chat-sidebar-item")).toHaveCount(2);

    await page.locator("[data-chat-sidebar-search]").fill("Banana");
    await expect(page.locator(".chat-sidebar-item")).toHaveCount(1);
    await expect(page.locator(".chat-sidebar-title-text")).toContainText("Banana");

    await page.locator("[data-chat-sidebar-search]").fill("");
    await expect(page.locator(".chat-sidebar-item")).toHaveCount(2);
});

test("Pinning a chat floats it to a PINNED bucket", async ({ page }) => {
    await page.goto("/chat");
    const chatA = chatStateWithModels(["anthropic/claude-sonnet-4.6"], "Older");
    const aId = Object.keys(chatA.chats)[0];
    chatA.chats[aId].updated_at = new Date(Date.now() - 86400_000 * 3).toISOString();
    const chatB = chatStateWithModels(["anthropic/claude-sonnet-4.6"], "Newer");
    const bId = Object.keys(chatB.chats)[0];
    await setLocalStorageState(page, {
        chats: { [aId]: chatA.chats[aId], [bId]: chatB.chats[bId] },
        activeChatId: aId,
        preferences: { welcome_dismissed: true },
    });
    await page.reload();
    // Pin "Older"
    const items = page.locator(".chat-sidebar-item");
    const olderItem = items.filter({ hasText: "Older" });
    await olderItem.hover();
    await olderItem.locator(".chat-sidebar-pin").click();
    // First bucket header is PINNED now
    await expect(page.locator(".chat-sidebar-bucket").first()).toHaveText("PINNED");
});

test("Delete confirmation removes the chat", async ({ page }) => {
    await page.goto("/chat");
    const stateA = chatStateWithModels(["anthropic/claude-sonnet-4.6"], "Target");
    const aId = Object.keys(stateA.chats)[0];
    await setLocalStorageState(page, {
        chats: { [aId]: stateA.chats[aId] },
        activeChatId: aId,
        preferences: { welcome_dismissed: true },
    });
    await page.reload();
    const item = page.locator(".chat-sidebar-item").filter({ hasText: "Target" });
    await item.hover();
    await item.locator(".chat-sidebar-delete").click();
    // Modal confirm replaces native confirm()
    await expect(page.locator(".chat-prompt-panel")).toBeVisible();
    await page.locator(".chat-prompt-confirm.is-danger").click();
    await expect(page.locator(".chat-sidebar-item").filter({ hasText: "Target" })).toHaveCount(0);
});

test("Double-clicking a sidebar title triggers a rename prompt", async ({
    page,
}) => {
    await page.goto("/chat");
    const stateA = chatStateWithModels(
        ["anthropic/claude-sonnet-4.6"],
        "Original name",
    );
    const aId = Object.keys(stateA.chats)[0];
    await setLocalStorageState(page, {
        chats: { [aId]: stateA.chats[aId] },
        activeChatId: aId,
        preferences: { welcome_dismissed: true },
    });
    await page.reload();
    const titleBtn = page.locator(".chat-sidebar-title").first();
    await titleBtn.dblclick();
    // Inline modal replaces native prompt()
    const input = page.locator(".chat-prompt-input");
    await expect(input).toBeVisible();
    await input.fill("Renamed chat");
    await input.press("Enter");
    await expect(page.locator(".chat-sidebar-title-text")).toContainText(
        "Renamed chat",
    );
});

test("Search also matches against the last user message", async ({ page }) => {
    await page.goto("/chat");
    const stateA = chatStateWithModels(
        ["anthropic/claude-sonnet-4.6"],
        "Untitled-A",
    );
    const aId = Object.keys(stateA.chats)[0];
    stateA.chats[aId].messages.push({
        id: "m1",
        role: "user",
        content: "Tell me about UNICORNS",
        created_at: new Date().toISOString(),
    });
    await setLocalStorageState(page, {
        chats: { [aId]: stateA.chats[aId] },
        activeChatId: aId,
        preferences: { welcome_dismissed: true },
    });
    await page.reload();
    await page.locator("[data-chat-sidebar-search]").fill("unicorn");
    await expect(page.locator(".chat-sidebar-item")).toHaveCount(1);
});

test("Sidebar items show relative timestamps", async ({ page }) => {
    await page.goto("/chat");
    const stateA = chatStateWithModels(
        ["anthropic/claude-sonnet-4.6"],
        "Just made",
    );
    const aId = Object.keys(stateA.chats)[0];
    await setLocalStorageState(page, {
        chats: { [aId]: stateA.chats[aId] },
        activeChatId: aId,
        preferences: { welcome_dismissed: true },
    });
    await page.reload();
    await expect(page.locator(".chat-sidebar-title-time").first()).toBeVisible();
});
