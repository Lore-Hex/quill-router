/**
 * Model picker — searchable, grouped by provider, with filter chips,
 * recently-used section, keyboard nav, and in-use indicator.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
    openModelPickerFromPill,
    activeModelIds,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

async function setupAndOpenPicker(page: any): Promise<void> {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    // Wait briefly for /v1/models to load before opening picker
    await page.waitForTimeout(300);
    await openModelPickerFromPill(page, 0);
}

test("picker opens with search input focused", async ({ page }) => {
    await setupAndOpenPicker(page);
    await expect(page.locator(".chat-model-picker-panel")).toBeVisible();
    await expect(page.locator(".chat-model-picker-search")).toBeFocused();
});

test("picker groups rows by provider with sorted headers", async ({ page }) => {
    await setupAndOpenPicker(page);
    const headers = page.locator(".chat-model-picker-group");
    const count = await headers.count();
    expect(count).toBeGreaterThan(1);
    // Headers should be the provider slugs and appear at least once each
    const names = await headers.allInnerTexts();
    expect(names.some((n) => n.includes("anthropic"))).toBe(true);
    expect(names.some((n) => n.includes("openai"))).toBe(true);
});

test("typing in search filters rows", async ({ page }) => {
    await setupAndOpenPicker(page);
    await page.locator(".chat-model-picker-search").fill("gpt");
    const rows = page.locator(".chat-model-row");
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);
    const names = await rows.allInnerTexts();
    expect(names.every((n) => n.toLowerCase().includes("gpt"))).toBe(true);
});

test("Free filter chip narrows to free-tier models", async ({ page }) => {
    await setupAndOpenPicker(page);
    await page.locator('.chat-picker-filter[data-filter="free"]').click();
    const rows = page.locator(".chat-model-row");
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);
    // Every visible row should carry the Free tag
    const freeTags = page.locator(".chat-model-row .chat-tag-free");
    await expect(freeTags).toHaveCount(count);
});

test("Vision filter chip narrows to vision-capable models", async ({
    page,
}) => {
    await setupAndOpenPicker(page);
    await page.locator('.chat-picker-filter[data-filter="vision"]').click();
    const rows = page.locator(".chat-model-row");
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);
    const visionTags = page.locator(".chat-model-row .chat-tag-vision");
    await expect(visionTags).toHaveCount(count);
});

test("In-use indicator surfaces on the active model row", async ({ page }) => {
    await setupAndOpenPicker(page);
    // The active row should carry the is-active-model class
    const activeRows = page.locator(".chat-model-row.is-active-model");
    expect(await activeRows.count()).toBeGreaterThanOrEqual(1);
});

test("ArrowDown then Enter selects the highlighted model", async ({ page }) => {
    await setupAndOpenPicker(page);
    // ArrowDown to highlight first row, then Enter to pick
    await page.locator(".chat-model-picker-search").press("ArrowDown");
    await page.locator(".chat-model-picker-search").press("Enter");
    // Picker closes
    await expect(page.locator(".chat-model-picker-panel")).toHaveCount(0);
    // The picked model is now the active model in the chat
    const ids = await activeModelIds(page);
    expect(ids.length).toBe(1);
});

test("Esc closes the picker", async ({ page }) => {
    await setupAndOpenPicker(page);
    await page.locator(".chat-model-picker-search").press("Escape");
    await expect(page.locator(".chat-model-picker-panel")).toHaveCount(0);
});

test("Clicking a model row updates the active chat", async ({ page }) => {
    await setupAndOpenPicker(page);
    await page.locator(".chat-model-row").filter({ hasText: "GPT-5.5" }).first().click();
    await expect(page.locator(".chat-model-picker-panel")).toHaveCount(0);
    const ids = await activeModelIds(page);
    expect(ids[0]).toBe("openai/gpt-5.5");
});
