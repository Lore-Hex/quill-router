// Local-only smoke coverage for the marketing page and server-rendered console.
const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests/browser",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:18081",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "TR_ENVIRONMENT=test TR_SENTRY_DSN= TR_STRIPE_SECRET_KEY= TR_STRIPE_WEBHOOK_SECRET= TR_GOOGLE_CLIENT_ID= TR_GOOGLE_CLIENT_SECRET= TR_GITHUB_CLIENT_ID= TR_GITHUB_CLIENT_SECRET= uv run uvicorn trusted_router.main:app --host 127.0.0.1 --port 18081",
    url: "http://127.0.0.1:18081/health",
    reuseExistingServer: true,
    timeout: 30_000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
