/**
 * Common helpers used across chat-playground specs.
 *   * waitForStreamToFinish: stays watching until the streaming
 *     caret is gone (proxy for "stream is done")
 *   * getLocalStorageState: pulls the saved chat state for assertions
 *   * sendMessage: types into the input + clicks Send
 *   * openModelPicker: opens the picker from a pill or Add Model
 */

import { Page, expect } from "@playwright/test";

export async function waitForStreamToFinish(page: Page, timeoutMs = 8_000): Promise<void> {
    await expect(page.locator(".chat-msg-bubble.is-streaming")).toHaveCount(0, {
        timeout: timeoutMs,
    });
}

export async function waitForFirstToken(page: Page, timeoutMs = 5_000): Promise<void> {
    await expect(page.locator(".chat-msg-bubble.is-streaming").first()).toBeVisible({
        timeout: timeoutMs,
    });
}

export async function sendMessage(page: Page, text: string): Promise<void> {
    const input = page.locator("[data-chat-input]");
    await input.fill(text);
    await page.locator("[data-chat-send]").click();
}

export async function getLocalStorageState(page: Page): Promise<any> {
    return await page.evaluate(() => {
        const raw = localStorage.getItem("tr_chat_state_v1");
        return raw ? JSON.parse(raw) : null;
    });
}

export async function setLocalStorageState(page: Page, state: any): Promise<void> {
    await page.evaluate((s) => {
        localStorage.setItem("tr_chat_state_v1", JSON.stringify(s));
    }, state);
}

export async function clearLocalStorageState(page: Page): Promise<void> {
    await page.evaluate(() => {
        localStorage.removeItem("tr_chat_state_v1");
        sessionStorage.removeItem("tr_chat_key");
    });
}

export async function openModelPickerFromPill(page: Page, slotIdx = 0): Promise<void> {
    // Click the pill to open its dropdown, then "Change model" inside.
    const pills = page.locator('[data-action="toggle-model-dropdown"]');
    await pills.nth(slotIdx).click();
    await page.locator('[data-action="open-model-picker"]').first().click();
}

export async function activeModelIds(page: Page): Promise<string[]> {
    return await page.evaluate(() => {
        const raw = localStorage.getItem("tr_chat_state_v1");
        if (!raw) return [];
        const state = JSON.parse(raw);
        const chat = state.chats?.[state.activeChatId];
        return (chat?.models ?? []).map((m: any) => m.model_id);
    });
}

/**
 * Build a deterministic empty chat state with N models for a test.
 * Lets specs skip the picker-driven setup when they're testing a
 * downstream behavior.
 */
export function chatStateWithModels(modelIds: string[], chatTitle = "Test chat"): any {
    const chatId = "c_test_" + Math.random().toString(36).slice(2, 8);
    return {
        chats: {
            [chatId]: {
                id: chatId,
                title: chatTitle,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
                models: modelIds.map((id) => ({
                    model_id: id,
                    system_prompt: "",
                    params: {
                        temperature: 1.0,
                        top_p: 1.0,
                        top_k: 0,
                        max_tokens: 1024,
                        frequency_penalty: 0,
                        presence_penalty: 0,
                        repetition_penalty: 1.0,
                        min_p: 0,
                        top_a: 0,
                    },
                    enabled: true,
                    label: "",
                })),
                shared_system_prompt: "",
                messages: [],
            },
        },
        activeChatId: chatId,
        preferences: { welcome_dismissed: true },
    };
}
