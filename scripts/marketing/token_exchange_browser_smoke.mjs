import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { chromium } from "playwright";

// Run against a local preview with a fake SES sender. Never submits production leads.
const base = process.env.TR_PREVIEW_URL || "http://127.0.0.1:8096";
assert(["127.0.0.1", "localhost"].includes(new URL(base).hostname));
const root = fileURLToPath(new URL("../../", import.meta.url));
const out = process.env.TR_QA_OUTPUT || "/tmp/tr-token-exchange-qa";
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ headless: true });
const failures = [];
try {
  for (const [name, width, height, theme] of [["desktop", 1440, 1000, "dark"], ["wide", 1920, 1080, "dark"], ["mobile", 390, 844, "dark"], ["narrow", 320, 740, "dark"], ["light", 1440, 1000, "light"]]) {
    const page = await browser.newPage({ viewport: { width, height }, reducedMotion: "reduce" });
    page.on("pageerror", error => failures.push(`${name}: ${error.message}`));
    await page.addInitScript(theme => localStorage.setItem("tr-theme", theme), theme);
    await page.goto(`${base}/token-exchange`, { waitUntil: "networkidle" });
    await page.evaluate(() => document.fonts.ready);
    assert(await page.locator(".tm-hero-art").evaluate(img => img.complete && img.naturalWidth > 0));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: horizontal overflow`);
    const textOverflow = await page.locator("main h1, main h2, main h3, main p, main button, main dt, main dd").evaluateAll(elements => elements.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.textContent));
    assert.deepEqual(textOverflow, [], `${name}: text overflow`);
    await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
    await page.screenshot({ path: path.join(out, `${name}-hero.png`) });
    await page.getByRole("link", { name: "Get the enterprise brief" }).click();
    await page.locator("#brief-email").fill("not-an-email");
    await page.getByRole("button", { name: "Download the brief" }).click();
    assert(!(await page.locator("#brief-email").evaluate(input => input.validity.valid)));
    if (name === "mobile") {
      await page.route("**/token-exchange/brief", route => route.fulfill({ status: 503, contentType: "application/json", body: '{"error":"delivery_unavailable"}' }));
      await page.locator("#brief-email").fill("ada@example.com");
      await page.getByRole("button", { name: "Download the brief" }).click();
      await page.locator('#brief-status[data-state="error"]').waitFor();
      assert(await page.getByRole("button", { name: "Download the brief" }).isEnabled());
      await page.screenshot({ path: path.join(out, "mobile-error.png") });
      await page.unroute("**/token-exchange/brief");
    }
    await page.locator("#brief-email").fill("ada@example.com");
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download the brief" }).click();
    const download = await downloadPromise;
    assert.equal(download.suggestedFilename(), "TrustedRouter-Enterprise-Brief.pdf");
    const downloaded = await readFile(await download.path());
    const original = await readFile(path.join(root, "src/trusted_router/data/enterprise/TrustedRouter-Enterprise-Brief.pdf"));
    const hash = bytes => createHash("sha256").update(bytes).digest("hex");
    assert.equal(hash(downloaded), hash(original));
    await page.locator('#brief-status[data-state="success"]').waitFor();
    await page.screenshot({ path: path.join(out, `${name}-download.png`) });
    await page.close();
  }
  assert.deepEqual(failures, []);
  console.log("Five viewports/themes, invalid email, delivery failure/retry, PDF byte equality: passed.");
} finally { await browser.close(); }
