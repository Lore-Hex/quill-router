import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

// Local QA only: no paid inference, leads, or production mutations.
const base = process.env.TR_PREVIEW_URL || "http://127.0.0.1:8098";
assert(["127.0.0.1", "localhost"].includes(new URL(base).hostname));
const out = process.env.TR_QA_OUTPUT || "/tmp/tr-savings-qa";
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ headless: true });
const failures = [];
try {
  for (const [name, width, height, theme] of [["desktop", 1440, 1000, "dark"], ["wide", 1920, 1080, "dark"], ["mobile", 390, 844, "dark"], ["narrow", 320, 740, "dark"], ["light", 1440, 1000, "light"]]) {
    const page = await browser.newPage({ viewport: { width, height }, reducedMotion: "reduce" });
    page.on("pageerror", error => failures.push(`${name}: ${error.message}`));
    await page.addInitScript(theme => localStorage.setItem("tr-theme", theme), theme);
    await page.goto(`${base}/token-exchange/savings`, { waitUntil: "networkidle" });
    await page.evaluate(() => document.fonts.ready);
    const text = id => page.locator(`#tx-${id}`).innerText();
    assert.equal(await text("saved"), "$1,198,630");
    assert.equal(await text("annual"), "$14,383,560");
    assert(await page.locator(".tx-gateway img").evaluate(img => img.complete && img.naturalWidth > 0));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: horizontal overflow`);
    const overflow = await page.locator("main h1, main h2, main p, main button, main strong, main label").evaluateAll(elements => elements.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.textContent));
    assert.deepEqual(overflow, [], `${name}: text overflow`);
    await page.screenshot({ path: `${out}/${name}.png`, fullPage: true });
    await page.screenshot({ path: `${out}/${name}-first.png` });
    await page.getByRole("switch").uncheck();
    assert.equal(await text("total"), "$3,350,000");
    assert.equal(await text("saved"), "$0");
    assert.equal(await text("fee"), "$0");
    assert.equal(await page.locator('#tx-request-log [data-route="exchange"]').count(), 0);
    await page.getByRole("switch").check();
    await page.locator("#tx-spend").fill("100");
    await page.locator("#tx-share").fill("100");
    await page.locator("#tx-price").fill("100");
    assert.equal(await text("total"), "$105.50");
    assert.equal(await text("savings-label"), "Estimated monthly increase");
    assert.equal(await text("percent"), "5.5% more on tokens");
    await page.locator("#tx-spend").fill("");
    assert.equal(await page.locator("#tx-spend").getAttribute("aria-invalid"), "true");
    assert(await page.locator("#tx-copy").isDisabled());
    await page.getByRole("button", { name: "Reset example" }).click();
    assert.equal(await text("saved"), "$1,198,630");
    assert(await page.locator("#tx-copy").isEnabled());
    await page.locator("#tx-share").focus();
    await page.keyboard.press("ArrowRight");
    assert.equal(await text("share-label"), "41%");
    await page.goto(`${base}/token-exchange/savings#spend=123.45&share=100&discount=80&enabled=0`);
    await page.reload();
    assert.equal(await text("total"), "$123.45");
    assert.equal(await text("saved"), "$0");
    // Clipboard denial still provides a selectable link with identical assumptions.
    await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { value: { writeText: () => Promise.reject(new Error("denied")) } }));
    await page.locator("#tx-copy").click();
    await page.locator("#tx-copy-fallback").waitFor({ state: "visible" });
    const copied = new URL(await page.locator("#tx-copy-fallback").inputValue());
    assert.equal(copied.hash, "#spend=123.45&share=100&discount=80&enabled=0");
    await page.locator("#tx-spend").fill("0");
    assert.equal(await text("total"), "$0");
    assert.equal(await text("percent"), "0.0% less on tokens");
    await page.close();
  }
  assert.deepEqual(failures, []);
  console.log("Savings calculator: five viewports/themes, toggles, arithmetic, invalid input, keyboard, sharing and clipboard fallback passed.");
} finally { await browser.close(); }
