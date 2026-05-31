/**
 * Two specialised SSE payloads:
 *   * Reasoning content (o1-style / Claude thinking) → renders into
 *     a <details class="chat-msg-reasoning"> with a "Thinking" summary
 *   * Tool calls → render into <details class="chat-msg-tools">
 *     with the JSON inside a <pre>
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
import { reasoningSse, toolCallSse } from "../fixtures/sse";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

test("Reasoning chunks render in a collapsible Thinking section", async ({
    page,
}) => {
    await setChatCompletionResponse(page, reasoningSse());
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Why?");
    await waitForStreamToFinish(page);
    const reas = page.locator(".chat-msg-reasoning");
    await expect(reas).toBeVisible();
    await expect(reas.locator("summary")).toContainText("Thinking");
    await expect(reas.locator(".chat-msg-reasoning-body")).toContainText(
        "Let me think",
    );
});

test("Reasoning + answer both render; answer is the visible content", async ({
    page,
}) => {
    await setChatCompletionResponse(page, reasoningSse());
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Why?");
    await waitForStreamToFinish(page);
    await expect(page.locator(".chat-msg-md")).toContainText("Paris");
});

test("Tool calls render in a collapsible Tool-calls section", async ({
    page,
}) => {
    await setChatCompletionResponse(page, toolCallSse());
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "What's the weather in Paris?");
    await waitForStreamToFinish(page);
    const tools = page.locator(".chat-msg-tools");
    await expect(tools).toBeVisible();
    await expect(tools.locator("summary")).toContainText("Tool calls");
});

test("Tool-call JSON content includes the function name + arguments", async ({
    page,
}) => {
    await setChatCompletionResponse(page, toolCallSse());
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "What's the weather in Paris?");
    await waitForStreamToFinish(page);
    const pre = page.locator(".chat-msg-tools pre");
    const text = (await pre.textContent()) || "";
    expect(text).toContain("get_weather");
    expect(text).toContain("Paris");
});

test("Expanding the Thinking section reveals reasoning body", async ({
    page,
}) => {
    await setChatCompletionResponse(page, reasoningSse());
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await sendMessage(page, "Why?");
    await waitForStreamToFinish(page);
    const details = page.locator(".chat-msg-reasoning");
    // Make sure expanded (details element)
    await details.evaluate((el: HTMLDetailsElement) => (el.open = true));
    await expect(details.locator(".chat-msg-reasoning-body")).toBeVisible();
});
