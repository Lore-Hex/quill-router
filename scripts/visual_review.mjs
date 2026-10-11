#!/usr/bin/env node
// Usage: node scripts/visual_review.mjs --base-url http://127.0.0.1:18083 --output-dir /tmp/site-review
// Optional: --routes /,/docs,/docs/telemetry --widths 390,1440 --height 1000 --overlap 200 --max-tiles 60
// Uses the project's Playwright dependency. In an isolated worktree without node_modules,
// set NODE_PATH=/absolute/path/to/the/main-checkout/node_modules. No server or test suite is started.
// Screenshots are evidence for visual review, not an automatic design approval. The console
// route is captured as a signed-out visitor; redirects and HTTP errors are recorded honestly.
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
const { chromium } = createRequire(import.meta.url)('playwright');

const defaults = {
  'base-url': 'http://127.0.0.1:18083',
  'output-dir': '/tmp/trustedrouter-visual-review',
  routes: '/,/docs,/docs/telemetry,/docs/agent-setup,/docs/provider-routing,/pricing,/trust,/models,/console/api-keys',
  widths: '1440,390,320', height: '1000', overlap: '200', 'max-tiles': '60',
};
const options = { ...defaults };
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, '');
  if (!(key in defaults) || process.argv[i + 1] === undefined) {
    throw new Error(`Unknown or incomplete option: ${process.argv[i]}`);
  }
  options[key] = process.argv[i + 1];
}
const positiveInt = (value, label) => {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1) throw new Error(`${label} must be a positive integer`);
  return n;
};
const base = new URL(options['base-url']);
if (!['http:', 'https:'].includes(base.protocol)) throw new Error('Use an HTTP(S) preview URL');
const widths = options.widths.split(',').map(w => positiveInt(w, 'width'));
const height = positiveInt(options.height, 'height');
const overlap = positiveInt(options.overlap, 'overlap');
const maxTiles = positiveInt(options['max-tiles'], 'max-tiles');
if (overlap >= height) throw new Error('overlap must be less than height');
const routes = options.routes.split(',').map(route => {
  if (!route.startsWith('/') || route.startsWith('//')) throw new Error('Routes must start with a single /');
  const url = new URL(route, base);
  if (url.origin !== base.origin) throw new Error('Routes must remain on the preview origin');
  return url.href;
});
const output = path.resolve(options['output-dir']);
await mkdir(output, { recursive: true });
const manifest = {
  startedAt: new Date().toISOString(), baseUrl: base.href, outputDirectory: output,
  capture: { widths, height, overlap, maxTiles, authentication: 'signed-out', browser: 'chromium' },
  reviewRequired: true, pages: [],
};
const save = () => writeFile(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
const browser = await chromium.launch({ headless: true });
let failed = false;
try {
  for (const [routeIndex, url] of routes.entries()) {
    for (const width of widths) {
      const record = { requestedUrl: url, width, height, screenshots: [], errors: [], blockedRequests: [] };
      manifest.pages.push(record);
      const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1, serviceWorkers: 'block' });
      // Public read-only preview: never submit analytics, forms, auth actions, or other writes.
      await context.route('**/*', async route => {
        const request = route.request();
        if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
          record.blockedRequests.push({ method: request.method(), url: request.url() });
          // A synthetic response avoids manufacturing console errors for blocked telemetry.
          await route.fulfill({ status: 204, body: '' });
        } else await route.continue();
      });
      const page = await context.newPage();
      page.on('pageerror', error => record.errors.push({ kind: 'javascript', message: error.message }));
      page.on('console', message => {
        if (message.type() === 'error') record.errors.push({ kind: 'console', message: message.text(), location: message.location() });
      });
      try {
        const response = await page.goto(url, { waitUntil: 'load', timeout: 30000 });
        record.httpStatus = response?.status() ?? null;
        record.finalUrl = page.url();
        record.redirected = record.finalUrl !== url;
        record.title = await page.title();
        await page.evaluate(() => Promise.race([document.fonts.ready, new Promise(resolve => setTimeout(resolve, 3000))]));
        record.document = await page.evaluate(() => ({
          viewportWidth: innerWidth, scrollWidth: document.documentElement.scrollWidth,
          scrollHeight: Math.max(document.documentElement.scrollHeight, document.body.scrollHeight),
          overflowingElements: [...document.querySelectorAll('main *')].filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && (r.left < -1 || r.right > innerWidth + 1);
          }).slice(0, 30).map(el => ({ tag: el.tagName, id: el.id, className: String(el.className),
            text: el.textContent.trim().slice(0, 100) })),
        }));
        record.horizontalOverflow = record.document.scrollWidth > width + 1;
        const slug = new URL(url).pathname.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '') || 'home';
        let y = 0;
        for (let tile = 0; tile < maxTiles; tile++) {
          await page.evaluate(top => scrollTo({ top, behavior: 'instant' }), y);
          await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const position = await page.evaluate(() => ({ y: scrollY,
            total: Math.max(document.documentElement.scrollHeight, document.body.scrollHeight) }));
          const file = `${routeIndex}-${slug}-${width}-${String(tile + 1).padStart(3, '0')}.png`;
          await page.screenshot({ path: path.join(output, file), animations: 'disabled' });
          record.screenshots.push({ file, y: position.y, height });
          const bottom = Math.max(0, position.total - height);
          if (position.y >= bottom - 1) break;
          if (tile === maxTiles - 1) record.truncated = true;
          y = Math.min(position.y + height - overlap, bottom);
        }
        record.captureStatus = 'captured';
        if (record.httpStatus >= 400 || record.horizontalOverflow || record.truncated || record.errors.length) failed = true;
      } catch (error) {
        record.captureStatus = 'failed';
        record.finalUrl = page.url();
        record.errors.push({ kind: 'capture', message: error.message });
        failed = true;
      } finally {
        await context.close();
        await save();
      }
      console.log(`${record.captureStatus}: ${width}px ${record.finalUrl} (${record.screenshots.length} tiles)`);
    }
  }
} finally {
  await browser.close();
  manifest.finishedAt = new Date().toISOString();
  await save();
}
console.log(`Review the screenshots and manifest: ${path.join(output, 'manifest.json')}`);
process.exitCode = failed ? 1 : 0;
