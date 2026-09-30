const { defineConfig, devices } = require("@playwright/test");
const base = require("./playwright.config");

// Keep legacy coverage intact; exercise the rollout flag on a separate server.
module.exports = defineConfig({
  ...base,
  testDir: "tests/homepage-browser",
  use: { ...base.use, baseURL: "http://127.0.0.1:18082" },
  webServer: {
    ...base.webServer,
    command: "TR_HOMEPAGE_LANDSCAPE_ENABLED=true " + base.webServer.command.replaceAll("18081", "18082"),
    url: "http://127.0.0.1:18082/health",
    reuseExistingServer: false,
  },
  projects: [
    { name: "homepage-desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "homepage-mobile", use: { ...devices["iPhone 13"], browserName: "chromium" } },
  ],
});
