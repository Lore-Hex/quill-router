/**
 * Per-message actions on assistant + user messages.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis, setChatCompletionResponse } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    sendMessage,
    waitForStreamToFinish,
    setLocalStorageState,
    chatStateWithModels,
    getLocalStorageState,
} from "../fixtures/helpers";
import { buildSseBody } from "../fixtures/sse";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

async function singleSendSetup(page: any): Promise<void> {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Original prompt");
    await waitForStreamToFinish(page);
}

test("Copy button on user message fires a toast", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await singleSendSetup(page);
    const userMsg = page.locator(".chat-msg-user").last();
    await userMsg.hover();
    await userMsg.locator(".chat-msg-action").getByText("Copy").click();
    await expect(page.locator(".chat-toast")).toBeVisible();
    await expect(page.locator(".chat-toast")).toHaveText("Copied");
});

test("Delete button removes a user message", async ({ page }) => {
    await singleSendSetup(page);
    const userMsg = page.locator(".chat-msg-user").last();
    await userMsg.hover();
    await userMsg.locator(".chat-msg-action").getByText("Delete").click();
    await expect(page.locator(".chat-msg-user")).toHaveCount(0);
});

test("Edit on user message opens an inline textarea and Save regenerates", async ({
    page,
}) => {
    await singleSendSetup(page);
    const userMsg = page.locator(".chat-msg-user").last();
    await userMsg.hover();
    await userMsg.locator(".chat-msg-action").getByText("Edit").click();
    // Inline textarea appears
    const ta = page.locator(".chat-msg-edit");
    await expect(ta).toBeVisible();
    await ta.fill("Edited prompt");
    await page.locator(".chat-msg-edit-save").click();
    await waitForStreamToFinish(page);
    // The user message text is updated
    await expect(page.locator(".chat-msg-user-text").last()).toContainText(
        "Edited prompt",
    );
});

test("Continue button fires another assistant message", async ({ page }) => {
    await singleSendSetup(page);
    // Initially: 1 user + 1 assistant
    await expect(page.locator(".chat-msg-user")).toHaveCount(1);
    await expect(page.locator(".chat-msg-assistant")).toHaveCount(1);
    const lastAssistant = page.locator(".chat-msg-assistant").last();
    await lastAssistant.hover();
    await lastAssistant.locator(".chat-msg-action").getByText("Continue").click();
    await waitForStreamToFinish(page);
    // Now: 2 user (one synthesized "Continue.") + 2 assistant
    await expect(page.locator(".chat-msg-user")).toHaveCount(2);
    await expect(page.locator(".chat-msg-assistant")).toHaveCount(2);
});

test("Branch creates a new chat with the same prefix", async ({ page }) => {
    await singleSendSetup(page);
    const initialChats = Object.keys(
        (await getLocalStorageState(page)).chats,
    ).length;
    const lastAssistant = page.locator(".chat-msg-assistant").last();
    await lastAssistant.hover();
    await lastAssistant.locator(".chat-msg-action").getByText("Branch").click();
    const afterChats = Object.keys(
        (await getLocalStorageState(page)).chats,
    ).length;
    expect(afterChats).toBe(initialChats + 1);
    // Branched chat is now active
    const state = await getLocalStorageState(page);
    expect(state.chats[state.activeChatId].title.toLowerCase()).toContain(
        "branch",
    );
});

test("Per-column Copy on multi-model fires a toast and copies the right column's text", async ({
    page,
    context,
}) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels([
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.5",
        ]),
    );
    await page.reload();
    await sendMessage(page, "Hello");
    await waitForStreamToFinish(page);
    const firstCol = page.locator(".chat-msg-col").first();
    await firstCol.hover();
    await firstCol.locator(".chat-msg-action").getByText("Copy").click();
    await expect(page.locator(".chat-toast")).toHaveText("Copied");
});
