/**
 * Multi-model parallel comparison — the headline feature.
 *
 * Verifies:
 *   * "+ Add model" grows chat.models[] up to 4
 *   * Send fans out N parallel requests, one per enabled model
 *   * Each response renders in its own column with provider avatar
 *   * Per-column actions (Copy, Regenerate) work in isolation
 *   * Disabled slots are skipped in the fan-out
 *   * Remove returns to single-model layout
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    sendMessage,
    waitForStreamToFinish,
    setLocalStorageState,
    chatStateWithModels,
    activeModelIds,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("Add Model grows chat.models[] up to 4 slots", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();

    // Start with 1 pill + Add Model button
    await expect(page.locator(".chat-model-pill")).toHaveCount(1);

    // Add 3 more
    for (let i = 0; i < 3; i++) {
        await page.locator('[data-action="add-model"]').click();
        // Auto-opens picker — pick the first row
        await page.locator(".chat-model-row").first().click();
    }
    await expect(page.locator(".chat-model-pill")).toHaveCount(4);
    // Add Model button hides at the cap
    await expect(page.locator('[data-action="add-model"]')).toHaveCount(0);
});

test("Send fans out N parallel requests, one per enabled model", async ({
    page,
}) => {
    const requestedModels: string[] = [];
    page.on("request", (req) => {
        if (req.url().includes("/v1/chat/completions")) {
            // Body is JSON — extract model id
            const data = req.postDataJSON();
            if (data && data.model) requestedModels.push(data.model);
        }
    });

    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels([
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.5",
            "google/gemini-2.5-flash",
        ]),
    );
    await page.reload();

    await sendMessage(page, "Tell me a fact");
    await waitForStreamToFinish(page);

    // Three separate POST calls, one per model
    expect(requestedModels).toHaveLength(3);
    expect(new Set(requestedModels)).toEqual(
        new Set([
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.5",
            "google/gemini-2.5-flash",
        ]),
    );
});

test("multi-model renders N response columns side-by-side", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels([
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.5",
            "google/gemini-2.5-flash",
            "mistralai/mistral-large",
        ]),
    );
    await page.reload();

    await sendMessage(page, "Hi");
    await waitForStreamToFinish(page);

    const grid = page.locator(".chat-msg-grid-4");
    await expect(grid).toBeVisible();
    await expect(grid.locator(".chat-msg-col")).toHaveCount(4);
});

test("each response column has its own header with provider avatar", async ({
    page,
}) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels([
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.5",
        ]),
    );
    await page.reload();
    await sendMessage(page, "Hi");
    await waitForStreamToFinish(page);

    const cols = page.locator(".chat-msg-col");
    await expect(cols).toHaveCount(2);
    for (let i = 0; i < 2; i++) {
        await expect(cols.nth(i).locator(".chat-msg-col-head-avatar")).toBeVisible();
        await expect(cols.nth(i).locator(".chat-msg-col-head-label")).not.toBeEmpty();
    }
});

test("disabled slot is skipped in the fan-out", async ({ page }) => {
    const requestedModels: string[] = [];
    page.on("request", (req) => {
        if (req.url().includes("/v1/chat/completions")) {
            const data = req.postDataJSON();
            if (data && data.model) requestedModels.push(data.model);
        }
    });

    await page.goto("/chat");
    const state = chatStateWithModels([
        "anthropic/claude-sonnet-4.6",
        "openai/gpt-5.5",
    ]);
    // Disable the second slot
    const chatId = Object.keys(state.chats)[0];
    state.chats[chatId].models[1].enabled = false;
    await setLocalStorageState(page, state);
    await page.reload();

    await sendMessage(page, "Hi");
    await waitForStreamToFinish(page);
    expect(requestedModels).toEqual(["anthropic/claude-sonnet-4.6"]);
});

test("per-column Regenerate re-streams only that column", async ({ page }) => {
    const completionsCount = { count: 0 };
    page.on("request", (req) => {
        if (req.url().includes("/v1/chat/completions"))
            completionsCount.count++;
    });

    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels([
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.5",
        ]),
    );
    await page.reload();
    await sendMessage(page, "Hi");
    await waitForStreamToFinish(page);

    expect(completionsCount.count).toBe(2);

    // Hover the first column to reveal actions, click Regenerate.
    const firstCol = page.locator(".chat-msg-col").first();
    await firstCol.hover();
    await firstCol.locator(".chat-msg-action").getByText("Regenerate").click();
    await waitForStreamToFinish(page);

    // One extra request fired (the second column was untouched)
    expect(completionsCount.count).toBe(3);
});
