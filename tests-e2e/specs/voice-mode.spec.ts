/**
 * Voice mode — entry button + permission flow + Esc exit.
 *
 * Real browser-native Web Speech / SpeechSynthesis can't be driven
 * from Playwright (the engines hook system audio), so these tests
 * stub the SR + speechSynthesis APIs on window and assert the UI
 * surface + the conversational loop wiring.
 */
import { test, expect } from "@playwright/test";
import { mockExternalApis } from "../fixtures/api-mock";
import { plantSignedInHint } from "../fixtures/sign-in";
import {
    setLocalStorageState,
    chatStateWithModels,
} from "../fixtures/helpers";

test.beforeEach(async ({ context, page, baseURL }) => {
    await mockExternalApis(page);
    await plantSignedInHint(context, baseURL!);
});

/**
 * Inject fakes for the Web Speech APIs + getUserMedia BEFORE chat.js
 * runs, so feature detection sees them as present and the overlay
 * opens. The fake SR doesn't emit results — these tests only cover
 * the UI surface, not the full STT/TTS loop.
 */
async function stubSpeechApis(page: any): Promise<void> {
    await page.addInitScript(() => {
        const noop = () => {};
        class FakeSR {
            continuous = false;
            interimResults = false;
            lang = "en-US";
            onresult: any = null;
            onerror: any = null;
            onend: any = null;
            start = noop;
            stop = noop;
            abort = noop;
        }
        (window as any).SpeechRecognition = FakeSR;
        (window as any).webkitSpeechRecognition = FakeSR;
        (window as any).speechSynthesis = {
            speak: noop,
            cancel: noop,
            pause: noop,
            resume: noop,
            getVoices: () => [],
            speaking: false,
            paused: false,
            pending: false,
        };
        // Fake getUserMedia that grants a dummy MediaStream-like object
        const fakeStream = {
            getTracks: () => [{ stop: noop, kind: "audio" }],
        };
        (navigator as any).mediaDevices = {
            getUserMedia: () => Promise.resolve(fakeStream),
        };
    });
}

test("Voice mode entry button is visible in the header", async ({ page }) => {
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    const btn = page.locator('[data-action="enter-voice-mode"]');
    await expect(btn).toBeVisible();
});

test("Clicking Voice mode opens the overlay with a listening orb", async ({
    page,
}) => {
    await stubSpeechApis(page);
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await page.locator('[data-action="enter-voice-mode"]').click();
    await expect(page.locator(".chat-voice-overlay")).toBeVisible();
    await expect(page.locator(".chat-voice-orb")).toBeVisible();
    await expect(page.locator("[data-voice-status]")).toContainText(
        /listening/i,
    );
});

test("Esc exits voice mode", async ({ page }) => {
    await stubSpeechApis(page);
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await page.locator('[data-action="enter-voice-mode"]').click();
    await expect(page.locator(".chat-voice-overlay")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator(".chat-voice-overlay")).toHaveCount(0);
});

test("Clicking the backdrop also exits voice mode", async ({ page }) => {
    await stubSpeechApis(page);
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await page.locator('[data-action="enter-voice-mode"]').click();
    await page.locator(".chat-voice-backdrop").click();
    await expect(page.locator(".chat-voice-overlay")).toHaveCount(0);
});

test("Voice mode shows a toast when Speech API is unavailable", async ({
    page,
}) => {
    // No stubSpeechApis() — feature-detect should fail
    await page.addInitScript(() => {
        delete (window as any).SpeechRecognition;
        delete (window as any).webkitSpeechRecognition;
        delete (window as any).speechSynthesis;
    });
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await page.locator('[data-action="enter-voice-mode"]').click();
    const toast = page.locator(".chat-toast.is-danger");
    await expect(toast).toBeVisible();
    await expect(toast).toContainText(/Web Speech|microphone|voice/i);
    // Overlay does NOT open
    await expect(page.locator(".chat-voice-overlay")).toHaveCount(0);
});

test("Voice mode shows a toast when mic permission is denied", async ({
    page,
}) => {
    await page.addInitScript(() => {
        const noop = () => {};
        class FakeSR {
            continuous = false;
            interimResults = false;
            lang = "en-US";
            onresult: any = null;
            onerror: any = null;
            onend: any = null;
            start = noop;
            stop = noop;
            abort = noop;
        }
        (window as any).SpeechRecognition = FakeSR;
        (window as any).speechSynthesis = {
            speak: noop,
            cancel: noop,
            getVoices: () => [],
            speaking: false,
            paused: false,
            pending: false,
        };
        (navigator as any).mediaDevices = {
            getUserMedia: () =>
                Promise.reject(
                    Object.assign(new Error("Permission denied"), {
                        name: "NotAllowedError",
                    }),
                ),
        };
    });
    await page.goto("/chat");
    await setLocalStorageState(
        page,
        chatStateWithModels(["anthropic/claude-sonnet-4.6"]),
    );
    await page.reload();
    await page.locator('[data-action="enter-voice-mode"]').click();
    const toast = page.locator(".chat-toast.is-danger");
    await expect(toast).toBeVisible();
    await expect(toast).toContainText(/denied|microphone/i);
    await expect(page.locator(".chat-voice-overlay")).toHaveCount(0);
});
