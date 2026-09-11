import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../../", import.meta.url));
const base = process.env.TR_PREVIEW_URL || "http://127.0.0.1:8096";
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  await page.goto(`${base}/token-exchange`);
  await page.setContent(`<!doctype html><html><head><style>
    @font-face { font-family: Archivo; src: url('${base}/static/fonts/archivo-latin.woff2'); }
    @font-face { font-family: Spectral; src: url('${base}/static/fonts/spectral-300-latin.woff2'); font-weight: 300; }
    * { box-sizing: border-box; } body { margin: 0; width: 1200px; height: 630px; overflow: hidden; background: #0a0e0b; color: #ede8db; font-family: Archivo, sans-serif; }
    .art { position: absolute; width: 1200px; height: 800px; bottom: -110px; }
    .brand { position: relative; display: flex; gap: 15px; align-items: center; margin: 52px 80px 0; font-size: 24px; }
    svg { width: 34px; height: 34px; }
    h1 { position: relative; font: 300 66px/1.08 Spectral, Georgia, serif; text-align: center; margin: 24px 70px 0; }
    p { position: relative; text-align: center; font-size: 22px; color: #bdcbbd; margin-top: 17px; }
  </style></head><body>
    <img class="art" src="${base}/static/enterprise/token-exchange-hero.webp" alt="">
    <div class="brand"><svg viewBox="0 0 24 24" fill="none" stroke="#a9cdb9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M1.4 7.5H6.5L11 12M1.4 16.5H4L8.5 12M1.4 12H15.5M17.4 12H22.6"/><circle cx="15.5" cy="12" r="1.9"/></svg>TrustedRouter</div>
    <h1>The token exchange.</h1>
    <p>Enterprise AI, bought on your terms. Privacy with proof.</p>
  </body></html>`);
  await page.evaluate(() => document.fonts.ready);
  await page.locator(".art").evaluate(img => img.decode());
  await page.screenshot({ path: path.join(root, "src/trusted_router/static/og/token-exchange.png") });
} finally { await browser.close(); }
