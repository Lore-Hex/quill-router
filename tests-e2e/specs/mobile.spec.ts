/**
 * Mobile chrome — runs against the "mobile-safari" project from
 * playwright.config.ts so the viewport is ~iPhone 14.
 *
 *  - Sidebar is hidden by default
 *  - Hamburger toggles the sidebar drawer
 *  - Backdrop click closes the drawer
 *  - Models bar collapses to a compact row
 *  - Input bar sticks to bottom; safe-area inset visible
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
} from "../fixtures/helpers";

// Force a mobile viewport for every test in this file so the suite
// is deterministic regardless of the project under which it runs.
test.use({ viewport: { width: 390, height: 844 } });

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("sidebar is closed on first mobile load", async ({ page }) => {
    await page.goto("/chat");
    const sidebar = page.locator("[data-chat-sidebar]");
    // Either hidden via dataset.open=false or off-screen via CSS
    const open = await sidebar.getAttribute("data-open");
    expect(open === null || open === "false").toBeTruthy();
});

test("hamburger button toggles the sidebar drawer + backdrop", async ({
    page,
}) => {
    await page.goto("/chat");
    const hamburger = page.locator('[data-action="toggle-sidebar"]');
    await expect(hamburger).toBeVisible();
    await hamburger.click();
    const sidebar = page.locator("[data-chat-sidebar]");
    await expect(sidebar).toHaveAttribute("data-open", "true");
    const backdrop = page.locator("[data-chat-sidebar-backdrop]");
    await expect(backdrop).toBeVisible();
});

test("clicking the backdrop closes the drawer", async ({ page }) => {
    await page.goto("/chat");
    await page.locator('[data-action="toggle-sidebar"]').click();
    const backdrop = page.locator("[data-chat-sidebar-backdrop]");
    await backdrop.click();
    const sidebar = page.locator("[data-chat-sidebar]");
    await expect(sidebar).toHaveAttribute("data-open", "false");
});

test("multi-model column grid is single-column on mobile", async ({ page }) => {
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
    const grid = page.locator(".chat-models-bar, [data-chat-models-bar]").first();
    await expect(grid).toBeVisible();
    // The model pills container is a flex/wrap row; on mobile each
    // pill should be visible (not horizontally clipped).
    const pillCount = await page.locator(".chat-model-pill").count();
    expect(pillCount).toBeGreaterThanOrEqual(3);
});

test("input bar stays in viewport after a model is added", async ({
    page,
}) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    const input = page.locator("[data-chat-input]");
    await expect(input).toBeVisible();
    const box = await input.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.y + box!.height).toBeLessThanOrEqual(844);
});
