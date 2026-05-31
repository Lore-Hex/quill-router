import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the /chat playground end-to-end suite.
 *
 * Strategy:
 *   * Tests run against a LOCAL TR server (uvicorn on :8765) with the
 *     memory storage backend. CI doesn't need GCP/AWS auth to run them.
 *   * Outbound calls to api.quillrouter.com are mocked via page.route()
 *     in each spec — see fixtures/api-mock.ts for the canonical handlers.
 *   * Tests are sharded by browser engine: Chromium (primary), WebKit
 *     (Safari/iOS surrogate), Firefox (sanity).
 *
 * The webServer block is what makes `playwright test` start the TR
 * server automatically — no manual `uvicorn` before running tests.
 * Reuses an existing server on retry runs so consecutive `npm test`
 * invocations don't pay the cold-start tax.
 */
export default defineConfig({
    testDir: "./specs",
    fullyParallel: true,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    workers: process.env.CI ? 2 : undefined,
    reporter: process.env.CI ? "github" : "list",
    timeout: 30_000,
    expect: { timeout: 5_000 },

    use: {
        baseURL: process.env.TR_E2E_BASE_URL || "http://127.0.0.1:8765",
        trace: "retain-on-failure",
        screenshot: "only-on-failure",
        video: "retain-on-failure",
        // Disable HTTPS certificate checks — TR runs HTTP locally.
        ignoreHTTPSErrors: true,
    },

    projects: [
        {
            name: "chromium",
            use: { ...devices["Desktop Chrome"] },
        },
        {
            name: "webkit",
            use: { ...devices["Desktop Safari"] },
        },
        {
            name: "firefox",
            use: { ...devices["Desktop Firefox"] },
        },
        // iOS Safari surrogate — used by mobile.spec.ts
        {
            name: "mobile-safari",
            use: { ...devices["iPhone 14"] },
            testMatch: /mobile\.spec\.ts/,
        },
    ],

    webServer: {
        // Spins up TR via uvicorn before tests start. The
        // pre_start_e2e.py script seeds an active auth session for
        // the "signed-in" fixture and prints its cookie value, which
        // is read by fixtures/signed-in.ts at use() time.
        command:
            "TR_STORAGE_BACKEND=memory " +
            "TR_ENVIRONMENT=local " +
            "uv run --frozen uvicorn trusted_router.main:app " +
            "--host 127.0.0.1 --port 8765",
        url: "http://127.0.0.1:8765/chat",
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
        stdout: "pipe",
        stderr: "pipe",
        cwd: "..",
    },
});
