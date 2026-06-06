/**
 * Centralized page.route() handlers for the external APIs the /chat
 * playground talks to. Tests call `mockExternalApis(page)` once at
 * the top of each test (or via beforeEach) to install the mocks.
 *
 * Mocked surfaces:
 *   * GET  api.trustedrouter.com/v1/models — returns the static catalog
 *     from sse.modelsCatalog()
 *   * POST api.trustedrouter.com/v1/chat/completions — returns a
 *     "Hello world" SSE stream by default; tests can override via
 *     setChatCompletionResponse(page, body) before invoking Send.
 *   * POST /internal/chat/issue-browser-key — returns a fake
 *     sk-tr-... key + sets the tr_chat_key cookie. Production has
 *     this route gated on session cookie; we bypass that for tests
 *     by mocking the wire response directly.
 */

import { Page } from "@playwright/test";
import { helloSse, modelsCatalog } from "./sse";

const DEFAULT_FAKE_KEY = "sk-tr-test-fakekey1234567890";

/** Install the standard mocks on a page. Idempotent. */
export async function mockExternalApis(page: Page): Promise<void> {
    // /v1/models — catalog
    await page.route("**/v1/models", async (route) => {
        await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(modelsCatalog()),
        });
    });

    // /v1/chat/completions — SSE stream. Default is hello-world; per-
    // test overrides go through setChatCompletionResponse() which
    // stashes the body on window.__TR_TEST_OVERRIDE__ and we read it
    // here.
    await page.route("**/v1/chat/completions", async (route) => {
        const body = await page.evaluate(
            () => (window as any).__TR_TEST_OVERRIDE__ ?? null,
        );
        const sse = body || helloSse();
        await route.fulfill({
            status: 200,
            headers: {
                "content-type": "text/event-stream",
                "cache-control": "no-cache",
            },
            body: sse,
        });
    });

    // Browser-key issuance — return a fake key + set the one-shot cookie.
    await page.route("**/internal/chat/issue-browser-key", async (route) => {
        await route.fulfill({
            status: 200,
            headers: {
                "set-cookie": `tr_chat_key=${DEFAULT_FAKE_KEY}; Path=/chat; SameSite=Lax; Max-Age=86400`,
                "content-type": "application/json",
            },
            body: JSON.stringify({
                data: {
                    raw_key: DEFAULT_FAKE_KEY,
                    key_hash: "hash_test_abc",
                    name: "chat-browser-test",
                    expires_at: new Date(
                        Date.now() + 30 * 24 * 3600 * 1000,
                    ).toISOString(),
                    limit_microdollars: 5_000_000,
                },
            }),
        });
    });
}

/** Override the next chat/completions response with a custom SSE body. */
export async function setChatCompletionResponse(
    page: Page,
    body: string,
): Promise<void> {
    await page.evaluate((b) => {
        (window as any).__TR_TEST_OVERRIDE__ = b;
    }, body);
}

/** Drop a previously-set override. */
export async function clearChatCompletionResponse(page: Page): Promise<void> {
    await page.evaluate(() => {
        delete (window as any).__TR_TEST_OVERRIDE__;
    });
}

/** Sentinel value the test fixtures use as the fake browser key. */
export const FAKE_BROWSER_KEY = DEFAULT_FAKE_KEY;
