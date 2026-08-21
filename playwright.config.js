// Local-only smoke coverage for the six service surfaces behind the tested
// production URL-map contract. The server is an in-memory test dispatcher: it
// never contacts Cloud Run, payment providers, email providers, or inference
// providers.
const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests/browser",
  timeout: 30_000,
  // All six apps deliberately share one in-memory Store. A single worker keeps
  // stateful browser journeys deterministic and mirrors one user's sequence.
  workers: 1,
  use: {
    // Keep APIRequestContext-compatible loopback as the default for the
    // existing suite. The split matrix navigates the three explicit
    // *.localhost hostnames in Chromium.
    baseURL: "http://127.0.0.1:18081",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "uv run --frozen --offline uvicorn tests.browser.six_surface_server:app --host 127.0.0.1 --port 18081 --lifespan off",
    url: "http://127.0.0.1:18081/health",
    // Reusing an arbitrary process on this port can make a split regression
    // look green against a stale combined app.
    reuseExistingServer: false,
    timeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          // macOS does not consistently resolve subdomains of localhost via
          // getaddrinfo. Keep the browser-domain matrix deterministic and
          // entirely on loopback.
          args: ["--host-resolver-rules=MAP *.localhost 127.0.0.1"],
        },
      },
    },
  ],
});
