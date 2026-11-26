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
import { deflateSync, inflateSync } from "node:zlib";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    getLocalStorageState,
    chatStateWithModels,
    sendMessage,
    waitForStreamToFinish,
} from "../fixtures/helpers";

// Clipboard tests in this file must not overwrite one another's share URL.
test.describe.configure({ mode: "default" });

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
    await page.route("**/auth/session", route => route.fulfill({
        json: { data: { user: { id: "export-user" }, workspace: { id: "export-workspace" } } },
    }));
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
    await expect(page.locator(".chat-msg-md").last()).toContainText("Hello world!");
    await waitForStreamToFinish(page);
}

test("local libraries render and sanitize a legacy compressed share and export it again", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const cdnRequests: string[] = [];
    await page.route("https://cdn.jsdelivr.net/**", route => {
        cdnRequests.push(route.request().url());
        return route.abort();
    });
    const state = chatStateWithModels(["test/model"], "Décembre 東京");
    const chat = state.chats[state.activeChatId];
    chat.messages.push({
        id: "example", role: "assistant", responses: [{
            model_id: "test/model",
            content: "**Résumé**\n\n```javascript\nconst answer = 42;\n```\n\n<iframe title='omitted'></iframe>",
        }],
    });
    const legacy = deflateSync(JSON.stringify({ v: 1, chat })).toString("base64");
    await page.goto("/chat#share=" + legacy);
    const markdown = page.locator(".chat-msg-md");
    await expect(markdown.locator("strong")).toHaveText("Résumé");
    await expect(markdown.locator(".hljs-keyword")).toHaveText("const");
    await expect(markdown.locator("iframe")).toHaveCount(0);
    await page.locator('[data-action="share-link"]').first().click();
    await expect(page.locator(".chat-toast")).toBeVisible();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    const fragment = new URL(copied).hash.slice("#share=".length);
    const exported = JSON.parse(inflateSync(Buffer.from(fragment, "base64")).toString("utf8"));
    expect(exported.chat.title).toBe(chat.title + " (shared)");
    expect(exported.chat.messages[0].responses[0].content).toBe(chat.messages[0].responses[0].content);
    expect(cdnRequests).toEqual([]);
});

test("export-json downloads a .json file with chat contents", async ({
    page,
}) => {
    await seedConvo(page);
    const downloadPromise = page.waitForEvent("download");
    await page.keyboard.press("Control+e");
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
    await expect(page.locator(".chat-toast")).toBeVisible();
    const url = await page.evaluate(() => navigator.clipboard.readText());
    expect(url).toContain("#share=");

    // Open in a fresh context so localStorage is empty
    const cleanCtx = await context.browser()!.newContext();
    const fresh = await cleanCtx.newPage();
    await mockExternalApis(fresh);
    await fresh.goto(url);
    await expect(fresh.locator(".chat-header-chat-title")).toHaveText("Roundtrip-Test-Chat (shared)");
    const state = await fresh.evaluate(() =>
        JSON.parse(localStorage.getItem("tr_chat_state_v1") || "{}"),
    );
    const titles = Object.values(state.chats || {}).map((c: any) => c.title);
    // Imported chat is suffixed with "(shared)" per chat.js:2403
    expect(titles.some((t: string) => t.includes("(shared)"))).toBe(true);
    await cleanCtx.close();
});
