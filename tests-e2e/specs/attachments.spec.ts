/**
 * Image attachments tray.
 *
 *  - Selecting an image from the hidden file input renders a thumb
 *    in the tray
 *  - The remove button drops the attachment
 *  - Sending while attachments are present clears them
 *  - Tray hidden when no attachments
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
    sendMessage,
    waitForStreamToFinish,
} from "../fixtures/helpers";
// A 1x1 transparent PNG, base64-encoded. Inlining keeps the suite
// self-contained — no test fixture file on disk to track.
const PNG_1X1_BASE64 =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

async function loadChat(page: any): Promise<void> {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
}

test("Attachment tray is hidden until something is attached", async ({
    page,
}) => {
    await loadChat(page);
    await expect(page.locator("[data-chat-attachments]")).toBeHidden();
});

test("Selecting an image inserts a thumbnail into the tray", async ({
    page,
}) => {
    await loadChat(page);
    const buf = Buffer.from(PNG_1X1_BASE64, "base64");
    await page.locator("[data-chat-file-input]").setInputFiles({
        name: "pixel.png",
        mimeType: "image/png",
        buffer: buf,
    });
    await expect(page.locator("[data-chat-attachments]")).toBeVisible();
    const thumb = page.locator(".chat-attachment-thumb");
    await expect(thumb).toHaveCount(1);
    await expect(thumb.locator("img")).toBeVisible();
});

test("Remove button drops the attachment + hides the tray", async ({
    page,
}) => {
    await loadChat(page);
    const buf = Buffer.from(PNG_1X1_BASE64, "base64");
    await page.locator("[data-chat-file-input]").setInputFiles({
        name: "pixel.png",
        mimeType: "image/png",
        buffer: buf,
    });
    await expect(page.locator(".chat-attachment-thumb")).toHaveCount(1);
    await page.locator(".chat-attachment-remove").click();
    await expect(page.locator(".chat-attachment-thumb")).toHaveCount(0);
    await expect(page.locator("[data-chat-attachments]")).toBeHidden();
});

test("Sending consumes the attachments (tray clears)", async ({ page }) => {
    await loadChat(page);
    const buf = Buffer.from(PNG_1X1_BASE64, "base64");
    await page.locator("[data-chat-file-input]").setInputFiles({
        name: "pixel.png",
        mimeType: "image/png",
        buffer: buf,
    });
    await expect(page.locator(".chat-attachment-thumb")).toHaveCount(1);
    await sendMessage(page, "describe this image");
    await waitForStreamToFinish(page);
    await expect(page.locator("[data-chat-attachments]")).toBeHidden();
});

test("Send body includes the image_url alongside the text content", async ({
    page,
}) => {
    let lastRequestBody: any = null;
    await page.route("**/v1/chat/completions", async (route) => {
        try {
            lastRequestBody = JSON.parse(route.request().postData() || "{}");
        } catch (_) {}
        // Let the default mock from mockExternalApis fall through —
        // we already intercepted, so fulfill with a minimal SSE.
        await route.fulfill({
            status: 200,
            headers: { "content-type": "text/event-stream" },
            body:
                'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n' +
                'data: {"choices":[{"finish_reason":"stop"}], "usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n' +
                "data: [DONE]\n\n",
        });
    });
    await loadChat(page);
    const buf = Buffer.from(PNG_1X1_BASE64, "base64");
    await page.locator("[data-chat-file-input]").setInputFiles({
        name: "pixel.png",
        mimeType: "image/png",
        buffer: buf,
    });
    await sendMessage(page, "describe this image");
    await waitForStreamToFinish(page);
    // The user message in the request body should now be a multipart
    // array — text + image_url
    const lastMsg = lastRequestBody.messages[lastRequestBody.messages.length - 1];
    expect(Array.isArray(lastMsg.content)).toBe(true);
    const types = lastMsg.content.map((c: any) => c.type);
    expect(types).toContain("text");
    expect(types).toContain("image_url");
});
