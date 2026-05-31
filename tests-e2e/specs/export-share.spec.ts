/**
 * Export to JSON / Markdown + Share via URL hash.
 *
 *  - export-json triggers a download with .json contents containing
 *    the chat object
 *  - export-md triggers a download with .md contents
 *  - share-link encodes the chat into location.hash; re-opening the
 *    URL imports the chat as a new entry
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

async function seedConvo(page: any): Promise<void> {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(
            ["anthropic/claude-sonnet-4.6"],
            "Roundtrip-Test-Chat",
        ),
    );
    await page.reload();
    await sendMessage(page, "Question for export");
    await waitForStreamToFinish(page);
}

test("export-json downloads a .json file with chat contents", async ({
    page,
}) => {
    await seedConvo(page);
    const downloadPromise = page.waitForEvent("download");
    await page.locator('[data-action="export-json"]').first().click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/\.json$/);
    const stream = await download.createReadStream();
    let body = "";
    await new Promise<void>((resolve) => {
        stream!.on("data", (b) => (body += b.toString()));
        stream!.on("end", () => resolve());
    });
    const parsed = JSON.parse(body);
    expect(parsed.title).toBe("Roundtrip-Test-Chat");
    expect(Array.isArray(parsed.messages)).toBe(true);
});

test("export-md downloads a .md file with the rendered chat", async ({
    page,
}) => {
    await seedConvo(page);
    const downloadPromise = page.waitForEvent("download");
    await page.locator('[data-action="export-md"]').first().click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/\.md$/);
    const stream = await download.createReadStream();
    let body = "";
    await new Promise<void>((resolve) => {
        stream!.on("data", (b) => (body += b.toString()));
        stream!.on("end", () => resolve());
    });
    expect(body).toContain("Roundtrip-Test-Chat");
    expect(body).toContain("Question for export");
});

test("share-link copies a URL with a #share= fragment", async ({
    page,
    context,
}) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await seedConvo(page);
    await page.locator('[data-action="share-link"]').first().click();
    // Toast replaces the old alert("Share link copied")
    await expect(page.locator(".chat-toast")).toBeVisible();
    const clip = await page.evaluate(() => navigator.clipboard.readText());
    expect(clip).toContain("/chat#share=");
});

test("opening a #share= URL imports the chat as a new entry", async ({
    page,
    context,
}) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await seedConvo(page);
    await page.locator('[data-action="share-link"]').first().click();
    await page.waitForTimeout(150);
    const url = await page.evaluate(() => navigator.clipboard.readText());
    expect(url).toContain("#share=");

    // Open in a fresh context so localStorage is empty
    const cleanCtx = await context.browser()!.newContext();
    await mockExternalApis(await cleanCtx.newPage()); // no-op route stash
    const fresh = await cleanCtx.newPage();
    await mockExternalApis(fresh);
    await fresh.goto(url);
    // Allow importSharedChatFromHash() to run
    await fresh.waitForTimeout(400);
    const state = await fresh.evaluate(() =>
        JSON.parse(localStorage.getItem("tr_chat_state_v1") || "{}"),
    );
    const titles = Object.values(state.chats || {}).map((c: any) => c.title);
    // Imported chat is suffixed with "(shared)" per chat.js:2403
    expect(titles.some((t: string) => t.includes("(shared)"))).toBe(true);
    await cleanCtx.close();
});
