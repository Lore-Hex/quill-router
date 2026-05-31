/**
 * Per-chat system prompt + per-model parameter sliders + presets.
 *
 *  - toggle-system-prompt action shows the system prompt panel
 *  - Typing into the textarea persists shared_system_prompt
 *  - Dragging a slider updates params.temperature in localStorage
 *  - Saving + loading a preset
 *  - Routing select sets provider_preferences.sort_by
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

async function seed(page: any): Promise<void> {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
}

test("toggle-system-prompt opens + closes the system prompt panel", async ({
    page,
}) => {
    await seed(page);
    const panel = page.locator("[data-chat-system-prompt]");
    // Hidden by default
    await expect(panel).toBeHidden();
    await page.locator('[data-action="toggle-system-prompt"]').first().click();
    await expect(panel).toBeVisible();
    await page.locator('[data-action="toggle-system-prompt"]').first().click();
    await expect(panel).toBeHidden();
});

test("Editing the system prompt persists to shared_system_prompt", async ({
    page,
}) => {
    await seed(page);
    await page.locator('[data-action="toggle-system-prompt"]').first().click();
    const ta = page.locator("[data-chat-system-prompt-input]");
    await ta.fill("You are a precise expert. Never speculate.");
    // Allow the input listener to fire saveState
    await page.waitForTimeout(150);
    const state = await getLocalStorageState(page);
    const chat = state.chats[state.activeChatId];
    expect(chat.shared_system_prompt).toBe(
        "You are a precise expert. Never speculate.",
    );
});

test("Per-model dropdown surfaces a temperature slider", async ({ page }) => {
    await seed(page);
    await page
        .locator('[data-action="toggle-model-dropdown"]')
        .first()
        .click();
    const slider = page.locator('input[data-param="temperature"]');
    await expect(slider).toBeVisible();
});

test("Dragging the temperature slider persists params.temperature", async ({
    page,
}) => {
    await seed(page);
    await page
        .locator('[data-action="toggle-model-dropdown"]')
        .first()
        .click();
    const slider = page.locator('input[data-param="temperature"]').first();
    // Set value programmatically + dispatch input/change so listeners
    // fire. Range inputs in Playwright dragging is unreliable.
    await slider.evaluate((el: HTMLInputElement) => {
        el.value = "0";
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await page.waitForTimeout(150);
    const state = await getLocalStorageState(page);
    const slot = state.chats[state.activeChatId].models[0];
    expect(slot.params.temperature).toBe(0);
});

test("Saving a preset stores it in preferences", async ({ page }) => {
    page.on("dialog", async (d) => {
        await d.accept("my-preset");
    });
    await seed(page);
    await page
        .locator('[data-action="toggle-model-dropdown"]')
        .first()
        .click();
    await page.locator('[data-action="save-preset"]').first().click();
    await page.waitForTimeout(150);
    const state = await getLocalStorageState(page);
    const names = (state.preferences.presets || []).map((p: any) => p.name);
    expect(names).toContain("my-preset");
});

test("Loading a preset applies its params to the slot", async ({ page }) => {
    await page.goto("/chat");
    const state = chatStateWithModels(["anthropic/claude-sonnet-4.6"]);
    state.preferences = {
        ...state.preferences,
        presets: [
            {
                name: "Deterministic",
                params: {
                    temperature: 0,
                    top_p: 1,
                    top_k: 0,
                    max_tokens: 256,
                    frequency_penalty: 0,
                    presence_penalty: 0,
                    repetition_penalty: 1,
                    min_p: 0,
                    top_a: 0,
                },
            },
        ],
    } as any;
    await setLocalStorageState(page, state);
    await page.reload();
    await page
        .locator('[data-action="toggle-model-dropdown"]')
        .first()
        .click();
    await page
        .locator('.chat-dd-preset[data-preset-name="Deterministic"]')
        .first()
        .click();
    await page.waitForTimeout(150);
    const after = await getLocalStorageState(page);
    expect(after.chats[after.activeChatId].models[0].params.temperature).toBe(0);
    expect(after.chats[after.activeChatId].models[0].params.max_tokens).toBe(
        256,
    );
});

test("Routing select stores provider_preferences.sort_by", async ({ page }) => {
    await seed(page);
    await page
        .locator('[data-action="toggle-model-dropdown"]')
        .first()
        .click();
    await page
        .locator('select[data-action="set-routing-sort"]')
        .first()
        .selectOption("latency");
    await page.waitForTimeout(150);
    const state = await getLocalStorageState(page);
    const slot = state.chats[state.activeChatId].models[0];
    expect(slot.provider_preferences?.sort_by).toBe("latency");
});
