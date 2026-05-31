/**
 * Anonymous-user contract — the user's hard product constraint:
 * "No actually sending tokens until they've done sign-in."
 *
 * These tests are the most important in the suite. They lock the
 * invariant that an anonymous click on Send fires ZERO requests to
 * api.quillrouter.com — only the sign-in modal opens.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { sendMessage } from "../fixtures/helpers";

test.beforeEach(async ({ page }) => {
    await mockExternalApis(page);
});

test("anonymous load — page renders without auth", async ({ page }) => {
    await page.goto("/chat");
    await expect(page.locator("[data-chat-shell]")).toBeVisible();
    await expect(page.locator("[data-chat-input]")).toBeVisible();
    await expect(page.locator("[data-chat-send]")).toBeVisible();
});

test("anonymous Send opens the sign-in modal, fires NO inference request", async ({
    page,
}) => {
    // Watch for any request to api.quillrouter.com/v1/chat/completions
    // — if it fires while signed-out, the user's constraint is broken.
    const inferenceCalls: string[] = [];
    page.on("request", (req) => {
        if (req.url().includes("/v1/chat/completions")) {
            inferenceCalls.push(req.url());
        }
    });

    await page.goto("/chat");
    await sendMessage(page, "Hello");

    // The sign-in modal should be open
    await expect(page.locator("#signinModal")).toBeVisible();
    expect(inferenceCalls).toEqual([]);
});

test("anonymous click on a suggested prompt fills input but doesn't send", async ({
    page,
}) => {
    const inferenceCalls: string[] = [];
    page.on("request", (req) => {
        if (req.url().includes("/v1/chat/completions")) {
            inferenceCalls.push(req.url());
        }
    });

    await page.goto("/chat");
    // The empty-state cards: each .chat-suggest has data-prompt. Click
    // the first one and verify it fills the input but Send still gates.
    const firstSuggest = page.locator(".chat-suggest").first();
    await firstSuggest.click();
    const input = page.locator("[data-chat-input]");
    await expect(input).not.toHaveValue("");

    await page.locator("[data-chat-send]").click();
    await expect(page.locator("#signinModal")).toBeVisible();
    expect(inferenceCalls).toEqual([]);
});

test("anonymous fetches /v1/models (it's public)", async ({ page }) => {
    let modelsCalled = false;
    page.on("request", (req) => {
        if (req.url().endsWith("/v1/models")) modelsCalled = true;
    });
    await page.goto("/chat");
    // Wait for the chat client to fetch the catalog on mount.
    await page.waitForTimeout(500);
    expect(modelsCalled).toBe(true);
});

test("anonymous load does NOT fetch /internal/chat/issue-browser-key", async ({
    page,
}) => {
    const keyIssueCalls: string[] = [];
    page.on("request", (req) => {
        if (req.url().includes("/internal/chat/issue-browser-key")) {
            keyIssueCalls.push(req.url());
        }
    });
    await page.goto("/chat");
    // Type a prompt + click Send while signed out
    await sendMessage(page, "Test");
    // Wait briefly for any async fetches to attempt
    await page.waitForTimeout(300);
    expect(keyIssueCalls).toEqual([]);
});
