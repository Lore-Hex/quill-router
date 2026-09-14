import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../../", import.meta.url));
const base = process.env.TR_PREVIEW_URL || "http://127.0.0.1:8137";
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  await page.goto(`${base}/green-tokens`);
  await page.setContent(`<!doctype html><html><head><style>
    @font-face { font-family: Archivo; src: url('${base}/static/fonts/archivo-latin.woff2'); }
    @font-face { font-family: Spectral; src: url('${base}/static/fonts/spectral-300-latin.woff2'); font-weight: 300; }
    * { box-sizing: border-box; } body { margin: 0; width: 1200px; height: 630px; overflow: hidden; background: #0b2119; color: #f4f5ed; font-family: Archivo, sans-serif; }
    .art { position: absolute; width: 1200px; height: 800px; bottom: -160px; }
    .brand { position: relative; display: flex; gap: 15px; align-items: center; margin: 44px 70px 0; font-size: 22px; }
    .brand img { width: 30px; height: 30px; }
    h1 { position: relative; font: 300 76px/1.08 Spectral, Georgia, serif; text-align: center; margin: 30px 70px 0; }
    p { position: relative; text-align: center; font-size: 26px; color: #d1dfd3; margin-top: 15px; }
    small { position: relative; display: block; text-align: center; font-size: 15px; color: #c2dbc9; }
  </style></head><body>
    <img class="art" src="${base}/static/green-tokens-hero.webp" alt="">
    <div class="brand"><img src="${base}/static/favicon.svg" alt="">TrustedRouter</div>
    <h1>Green tokens.</h1>
    <p>Your next million tokens. Powered by renewable energy.</p>
    <small>100% renewable inference electricity, declared by Regolo.</small>
  </body></html>`);
  await page.evaluate(() => document.fonts.ready);
  await page.locator(".art").evaluate(img => img.decode());
  await page.locator(".brand img").evaluate(img => img.decode());
  await page.screenshot({ path: path.join(root, "src/trusted_router/static/og/green-tokens.png") });
} finally { await browser.close(); }
