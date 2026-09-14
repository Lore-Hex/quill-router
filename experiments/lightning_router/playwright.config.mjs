import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests/browser", workers: 1, timeout: 30000,
  use: { baseURL: "http://127.0.0.1:8094", channel: process.env.LR_BROWSER_CHANNEL || undefined, headless: true, screenshot: "only-on-failure" },
  webServer: { command: "uv run --no-sync python -m tests.browser_server", url: "http://127.0.0.1:8094/health", reuseExistingServer: false, timeout: 30000 },
});
