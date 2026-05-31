/**
 * Code-block adornments: every <pre><code class="language-xxx">
 * inside an assistant bubble gets a language label and a Copy button
 * appended via decorateCodeBlocks().
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis, setChatCompletionResponse } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
    sendMessage,
    waitForStreamToFinish,
} from "../fixtures/helpers";
import { markdownSse } from "../fixtures/sse";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

async function sendMarkdownReply(page: any): Promise<void> {
    await setChatCompletionResponse(page, markdownSse());
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Show me python code");
    await waitForStreamToFinish(page);
}

test("Markdown response renders code block in a <pre><code>", async ({
    page,
}) => {
    await sendMarkdownReply(page);
    const pre = page.locator(".chat-msg-md pre").first();
    await expect(pre).toBeVisible();
    await expect(pre.locator("code")).toBeVisible();
});

test("Language label is appended to the code block", async ({ page }) => {
    await sendMarkdownReply(page);
    const lang = page.locator(".chat-code-lang").first();
    await expect(lang).toBeVisible();
    await expect(lang).toContainText("python");
});

test("Copy button is appended to the code block", async ({ page }) => {
    await sendMarkdownReply(page);
    const btn = page.locator(".chat-code-copy").first();
    await expect(btn).toBeVisible();
    await expect(btn).toHaveText("Copy");
});

test("Clicking Copy updates the button label to Copied for 1.2s", async ({
    page,
    context,
}) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await sendMarkdownReply(page);
    const btn = page.locator(".chat-code-copy").first();
    await btn.click();
    await expect(btn).toHaveText("Copied");
    // Reverts after the timeout
    await page.waitForTimeout(1400);
    await expect(btn).toHaveText("Copy");
});

test("Copy button writes code text (not surrounding markdown) to clipboard", async ({
    page,
    context,
}) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await sendMarkdownReply(page);
    await page.locator(".chat-code-copy").first().click();
    await page.waitForTimeout(150);
    const clip = await page.evaluate(() => navigator.clipboard.readText());
    // The markdownSse() fixture has "def add(a, b)" in the code
    expect(clip).toContain("def add");
    expect(clip).not.toContain("# Result");
});

test("Markdown headings and lists also render", async ({ page }) => {
    await sendMarkdownReply(page);
    await expect(page.locator(".chat-msg-md h1")).toContainText("Result");
    await expect(page.locator(".chat-msg-md li").first()).toContainText(
        "works",
    );
});
