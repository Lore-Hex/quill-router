const fs = require("node:fs");
const { defineConfig, devices } = require("@playwright/test");

function authorizationHeader() {
  const path = process.env.TR_ROLLOUT_SMOKE_AUTH_HEADER_FILE;
  if (!path) throw new Error("TR_ROLLOUT_SMOKE_AUTH_HEADER_FILE is required");
  const line = fs.readFileSync(path, "utf8").trim();
  const prefix = "Authorization: ";
  if (!line.startsWith(prefix)) throw new Error("authorization header file is malformed");
  return line.slice(prefix.length);
}

const storageState = process.env.TR_ROLLOUT_SMOKE_PLAYWRIGHT_STORAGE_STATE;
if (!storageState) throw new Error("TR_ROLLOUT_SMOKE_PLAYWRIGHT_STORAGE_STATE is required");
const outputDir = process.env.TR_ROLLOUT_SMOKE_PLAYWRIGHT_OUTPUT_DIR;
if (!outputDir) throw new Error("TR_ROLLOUT_SMOKE_PLAYWRIGHT_OUTPUT_DIR is required");

module.exports = defineConfig({
  testDir: "tests/browser",
  testMatch: "production_rollout_smoke.spec.js",
  outputDir,
  timeout: 60_000,
  workers: 1,
  retries: 0,
  reporter: "line",
  use: {
    storageState,
    extraHTTPHeaders: { Authorization: authorizationHeader() },
    // Production credentials must not be serialized into a failure trace.
    screenshot: "off",
    trace: "off",
    video: "off",
  },
  projects: [{ name: "firefox", use: { ...devices["Desktop Firefox"] } }],
});
