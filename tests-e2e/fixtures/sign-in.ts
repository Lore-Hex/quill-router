/**
 * Sign-in helpers for the e2e suite.
 *
 * The chat page reads `tr_signed_in=1` (a non-HttpOnly hint cookie)
 * to know whether to gate the Send button. Tests don't actually go
 * through OAuth — they plant the hint cookie directly. For tests that
 * need a real `tr_session` cookie (e.g. when hitting
 * /internal/chat/issue-browser-key against the live route instead of
 * the mocked one), use `realSignIn()` which seeds a session in the
 * server's in-memory store via a test-only debug endpoint.
 *
 * The mocked-key path (via fixtures/api-mock.ts) is sufficient for
 * 95% of chat tests; reach for realSignIn only when the contract
 * involves the actual session/key plumbing.
 */

import { BrowserContext, Page } from "@playwright/test";

/** Plant the JS-readable signed-in hint cookie. Fast path for most tests. */
export async function plantSignedInHint(context: BrowserContext, baseURL: string): Promise<void> {
    const url = new URL(baseURL);
    await context.addCookies([
        {
            name: "tr_signed_in",
            value: "1",
            domain: url.hostname,
            path: "/",
            httpOnly: false,
            secure: false,
            sameSite: "Lax",
        },
    ]);
}

/** Helper assertion — page thinks the user is signed in. */
export async function expectSignedIn(page: Page): Promise<boolean> {
    return await page.evaluate(() => {
        const w = window as any;
        if (typeof w.hasSignedInHint === "function") return w.hasSignedInHint();
        return document.cookie.split(";").some((c) => c.trim() === "tr_signed_in=1");
    });
}
