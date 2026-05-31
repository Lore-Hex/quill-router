"use strict";
/* TrustedRouter chat playground at /chat.
 *
 * Chunk 1 ships the page shell + sign-in gate hook. Chunk 2 wires
 * up the model picker, streaming, sidebar, parameter sliders, etc.
 * Chunk 3 adds multi-model parallel send. Chunk 4 is polish.
 *
 * Design:
 *   * Vanilla JS, no framework — matches the no-Alpine philosophy of
 *     dashboard.js
 *   * State lives in localStorage (privacy: TR's homepage advertises
 *     "0 prompt/output logs"; server-side history would contradict
 *     that)
 *   * Send button gated client-side on `hasSignedInHint()` from
 *     dashboard.js — signed-out clicks pop the existing #signinModal
 *     instead of firing any provider inference
 *   * Browser-side API key: auto-issued via
 *     POST /internal/chat/issue-browser-key on first signed-in Send;
 *     server returns the raw key in a one-shot tr_chat_key cookie;
 *     we read it here, copy to sessionStorage, clear the cookie
 */

(function () {
    // Defer to dashboard.js for `hasSignedInHint()` + `openSigninModal()`.
    // Both are global-attached in dashboard.js. If dashboard.js hasn't
    // loaded yet (script order race), fall back to direct cookie read.
    function isSignedIn() {
        if (typeof window.hasSignedInHint === "function") {
            return window.hasSignedInHint();
        }
        return document.cookie
            .split(";")
            .map((c) => c.trim())
            .some((c) => c === "tr_signed_in=1");
    }

    function openSigninModal() {
        if (typeof window.openSigninModal === "function") {
            window.openSigninModal();
            return;
        }
        const dialog = document.getElementById("signinModal");
        if (dialog && typeof dialog.showModal === "function" && !dialog.open) {
            dialog.showModal();
        }
    }

    // One-shot bootstrap: read tr_chat_key cookie if present, copy to
    // sessionStorage, clear the cookie. The server only sets this
    // cookie in response to a `/internal/chat/issue-browser-key` call
    // (chunk 2). For chunk 1, this is just the pickup wiring.
    function bootstrapBrowserKey() {
        const cookieName =
            (window.__TR_CHAT__ && window.__TR_CHAT__.keyCookieName) || "tr_chat_key";
        const match = document.cookie
            .split(";")
            .map((c) => c.trim())
            .find((c) => c.startsWith(cookieName + "="));
        if (!match) return;
        const raw = decodeURIComponent(match.slice(cookieName.length + 1));
        try {
            sessionStorage.setItem("tr_chat_key", raw);
        } catch {
            // sessionStorage may be unavailable in some private-mode
            // contexts. Send flow will fall back to a re-issuance call.
        }
        // Clear the cookie immediately. Path must match the one set
        // by the server (=/chat).
        document.cookie =
            cookieName + "=; path=/chat; expires=Thu, 01 Jan 1970 00:00:00 GMT";
    }

    // MVP send-gate placeholder. Real send flow (model picker, stream
    // parsing, multi-model fan-out) ships in chunk 2.
    function handleSendClick(event) {
        event.preventDefault();
        if (!isSignedIn()) {
            // The user's hard constraint: NO request to
            // api.quillrouter.com fires when signed out.
            openSigninModal();
            return;
        }
        // Chunk 2 will replace this with the actual stream-completion
        // flow.
        console.info("chat: signed in — chunk 2 wires the actual send");
    }

    function init() {
        bootstrapBrowserKey();
        const sendBtn = document.querySelector("[data-chat-send]");
        if (sendBtn) {
            sendBtn.addEventListener("click", handleSendClick);
        }
        // New-chat / sidebar-toggle / system-prompt-toggle — wired in chunk 2.
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
