/**
 * Settings overlay — show-settings action opens an overlay with
 * default system prompt + default model + Enter-to-send toggle +
 * preset list + Clear-all-data button.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    getLocalStorageState,
    chatStateWithModels,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

async function openSettings(page: any): Promise<void> {
    await page.locator('[data-action="show-settings"]').first().click();
    await expect(page.locator(".chat-settings-overlay")).toBeVisible();
}

test("Settings overlay opens via the show-settings action", async ({ page }) => {
    await page.goto("/chat");
    await openSettings(page);
    await expect(page.locator(".chat-settings-panel")).toBeVisible();
});

test("editing the default system prompt persists to localStorage", async ({
    page,
}) => {
    await page.goto("/chat");
    await openSettings(page);
    const ta = page.locator('[data-setting="default_system_prompt"]');
    await ta.fill("You are a precise expert.");
    // Allow input event to fire and save
    await page.waitForTimeout(150);
    const state = await getLocalStorageState(page);
    expect(state.preferences.defaultSystemPrompt).toBe("You are a precise expert.");
});

test("editing the default model id persists to localStorage", async ({
    page,
}) => {
    await page.goto("/chat");
    await openSettings(page);
    const input = page.locator('[data-setting="default_model_id"]');
    await input.fill("openai/gpt-5.5");
    await page.waitForTimeout(150);
    const state = await getLocalStorageState(page);
    expect(state.preferences.lastModelId).toBe("openai/gpt-5.5");
});

test("toggling Enter-to-send persists", async ({ page }) => {
    await page.goto("/chat");
    await openSettings(page);
    const cb = page.locator('[data-setting="enter_to_send"]');
    const before = await cb.isChecked();
    await cb.click();
    await page.waitForTimeout(150);
    const state = await getLocalStorageState(page);
    expect(state.preferences.enter_to_send).toBe(!before);
});

test("Clear All wipes chats + preferences after confirm", async ({ page }) => {
    page.on("dialog", (d) => d.accept());
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"], "Wipe-me"),
    );
    await page.reload();
    await openSettings(page);
    await page.locator(".chat-settings-danger[data-wipe]").click();
    // Overlay should close + an empty chat replaces the old data
    await expect(page.locator(".chat-settings-overlay")).toHaveCount(0);
    const state = await getLocalStorageState(page);
    // After wipe, ensureActiveChat() creates one new empty chat
    const chatTitles = Object.values(state.chats).map((c: any) => c.title);
    expect(chatTitles).not.toContain("Wipe-me");
});

test("Backdrop close button dismisses the overlay", async ({ page }) => {
    await page.goto("/chat");
    await openSettings(page);
    await page.locator(".chat-settings-close").click();
    await expect(page.locator(".chat-settings-overlay")).toHaveCount(0);
});

test("Saved presets surface in the settings list", async ({ page }) => {
    await page.goto("/chat");
    const state = chatStateWithModels(["anthropic/claude-sonnet-4.6"]);
    state.preferences = {
        ...state.preferences,
        presets: [
            { name: "Strict", params: { temperature: 0, top_p: 1, max_tokens: 512 } },
            { name: "Creative", params: { temperature: 1.2, top_p: 0.95, max_tokens: 2048 } },
        ],
    } as any;
    await setLocalStorageState(page, state);
    await page.reload();
    await openSettings(page);
    const presets = page.locator(".chat-settings-preset");
    await expect(presets).toHaveCount(2);
    await expect(presets.nth(0)).toContainText("Strict");
    await expect(presets.nth(1)).toContainText("Creative");
});
