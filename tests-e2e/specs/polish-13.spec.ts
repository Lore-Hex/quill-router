/**
 * Polish round 13: opaque modal replacements for native dialogs,
 * smart cost formatting, Sys-active class, tab title, picker chrome.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis, setChatCompletionResponse } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
    sendMessage,
    waitForStreamToFinish,
    openModelPickerFromPill,
} from "../fixtures/helpers";
import { buildSseBody } from "../fixtures/sse";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("Esc cancels a prompt modal without persisting", async ({ page }) => {
    await page.goto("/chat");
    const state = chatStateWithModels(
        ["anthropic/claude-sonnet-4.6"],
        "Before",
    );
    const aId = Object.keys(state.chats)[0];
    await setLocalStorageState(page, state);
    await page.reload();
    const titleBtn = page.locator(".chat-sidebar-title").first();
    await titleBtn.dblclick();
    const input = page.locator(".chat-prompt-input");
    await expect(input).toBeVisible();
    await input.fill("Never saved");
    await input.press("Escape");
    await expect(input).toHaveCount(0);
    // Original title still wins
    await expect(page.locator(".chat-sidebar-title-text")).toContainText(
        "Before",
    );
});

test("Sys button gets is-active class when a custom system prompt is set", async ({
    page,
}) => {
    await page.goto("/chat");
    const state = chatStateWithModels(["anthropic/claude-sonnet-4.6"]);
    state.chats[Object.keys(state.chats)[0]].shared_system_prompt =
        "You are a precise expert.";
    await setLocalStorageState(page, state);
    await page.reload();
    const sysBtn = page.locator('[data-action="toggle-system-prompt"]');
    await expect(sysBtn).toHaveClass(/is-active/);
});

test("Sys button is NOT active on a vanilla chat", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    const sysBtn = page.locator('[data-action="toggle-system-prompt"]');
    await expect(sysBtn).not.toHaveClass(/is-active/);
});

test("Tab title reflects the active chat title", async ({ page }) => {
    await page.goto("/chat");
    const state = chatStateWithModels(
        ["anthropic/claude-sonnet-4.6"],
        "Onboarding Bug Investigation",
    );
    await setLocalStorageState(page, state);
    await page.reload();
    // give the title-render cycle a beat
    await page.waitForTimeout(100);
    await expect(page).toHaveTitle(/Onboarding Bug Investigation/);
});

test("Cost shows cents under a dollar instead of $0.0000", async ({ page }) => {
    // Drive a cost of about 3¢ via the SSE usage block
    const body = buildSseBody({
        parts: [{ content: "Hello" }],
        promptTokens: 1000,
        completionTokens: 200,
    });
    await setChatCompletionResponse(page, body);
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Hi");
    await waitForStreamToFinish(page);
    const meta = page.locator(".chat-msg-meta").first();
    const text = (await meta.textContent()) || "";
    // Either "<0.1¢", "X.XX¢", "$X.XX", or "$0" — never "$0.0000"
    expect(text).not.toMatch(/\$0\.0000/);
});

test("Picker shows footer keyboard-hint bar", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await openModelPickerFromPill(page, 0);
    const footer = page.locator(".chat-model-picker-footer");
    await expect(footer).toBeVisible();
    await expect(footer).toContainText("navigate");
    await expect(footer).toContainText("select");
    await expect(footer).toContainText("close");
});

test("Picker filter chips show a count of matching models", async ({
    page,
}) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await openModelPickerFromPill(page, 0);
    // Free chip should show "(N)" for some N ≥ 1 because GPT-5.4 Nano is free
    const freeCount = page.locator('[data-count="free"]');
    await expect(freeCount).toBeVisible();
    const text = (await freeCount.textContent()) || "";
    expect(text).toMatch(/\(\d+\)/);
});

test("Picker shows a friendly empty state when search has no matches", async ({
    page,
}) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await openModelPickerFromPill(page, 0);
    await page.locator(".chat-model-picker-search").fill("xyzzy-no-such-model");
    const empty = page.locator(".chat-model-picker-empty");
    await expect(empty).toBeVisible();
    await expect(empty).toContainText("No models match");
});
