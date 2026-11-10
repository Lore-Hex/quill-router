// Provider evidence page: the in-browser attestation check, offline.
// Network calls are answered from a recorded real attestation document and
// the browser clock is set to the moment it was recorded, so the signature
// and certificate checks run for real without depending on a live enclave.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fixture = require('./verified-fixture.json');
const verifier = require('./verified.js');
const base = process.argv[2] || 'http://127.0.0.1:8098';
const output = process.env.NYTE_PREVIEW_DIR || require('node:os').tmpdir();
const OTHER = 'ab'.repeat(48);

async function rejects(label, bytes, nonce, now, reason) {
  await assert.rejects(verifier.verifyAttestation(bytes, nonce, now), error => error.message.includes(reason), label);
}

async function verifierControls() {
  const bytes = verifier.fromBase64(fixture.attestation_document);
  const good = await verifier.verifyAttestation(bytes, fixture.nonce, fixture.now);
  assert.equal(good.pcr0, fixture.pcr0);
  assert.equal(good.certificates, 5);
  await rejects('replayed document', bytes, '00000000-0000-4000-8000-000000000000', fixture.now, 'not fresh');
  await rejects('expired certificate', bytes, fixture.nonce, fixture.now + 30 * 86400e3, 'validity period');
  const at = Buffer.from(bytes).indexOf(Buffer.from(fixture.pcr0, 'hex'));
  assert.ok(at > 0);
  const measurement = Uint8Array.from(bytes); measurement[at + 5] ^= 1;
  await rejects('altered measurement', measurement, fixture.nonce, fixture.now, 'attestation signature is invalid');
  const signature = Uint8Array.from(bytes); signature[signature.length - 3] ^= 1;
  await rejects('altered signature', signature, fixture.nonce, fixture.now, 'attestation signature is invalid');
  // A certificate that is not AWS's: flip a byte inside the root certificate.
  const rootCertificate = verifier.cbor(verifier.cbor(bytes)[2]).get('cabundle')[0];
  const root = Buffer.from(bytes).indexOf(Buffer.from(rootCertificate));
  assert.ok(root > 0);
  const forged = Uint8Array.from(bytes); forged[root + 100] ^= 1;
  await rejects('forged root', forged, fixture.nonce, fixture.now, 'AWS Nitro root');
  assert.deepEqual(['verified', 'pending', 'mismatch', 'incomplete'],
    [verifier.verdict('aa', 'bb', 'AA'), verifier.verdict('aa', 'AA', 'bb'), verifier.verdict('aa', 'bb', 'cc'), verifier.verdict('aa', null, 'cc')]);
  assert.equal(verifier.verdict('aa', 'aa', null), 'incomplete', 'a missing NYTE record is not a pending review');
  console.log('PASS verifier: genuine accepted; replay, expiry, altered measurement, altered signature, forged root rejected');
}

// Each case: the fingerprints the two public records report, and what the page must conclude.
const CASES = [
  {name: 'verified', provider: fixture.pcr0, nyte: fixture.pcr0, title: 'Verified just now', states: ['ok', 'ok', 'ok', 'ok']},
  {name: 'pending', provider: fixture.pcr0, nyte: OTHER, title: 'Newer release, review in progress', states: ['ok', 'ok', 'ok', 'wait']},
  {name: 'mismatch', provider: OTHER, nyte: OTHER, title: 'Not verified', states: ['ok', 'ok', 'bad', 'wait']},
  {name: 'incomplete', provider: fixture.pcr0, nyte: null, title: 'Check incomplete', states: ['ok', 'ok', 'ok', 'bad']},
  {name: 'failed', enclaveDown: true, provider: fixture.pcr0, nyte: fixture.pcr0, title: 'Check could not finish', states: ['bad', 'waiting', 'waiting', 'waiting']},
  {name: 'mismatch', replay: true, provider: fixture.pcr0, nyte: fixture.pcr0, title: 'Not verified', states: ['ok', 'bad', 'waiting', 'waiting']},
];

async function open(browser, width, scenario) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.install({time: fixture.now});
  // The page asks for a nonce it chose; the recording can only answer its own.
  await page.addInitScript(nonce => { crypto.randomUUID = () => nonce; }, scenario.replay ? '00000000-0000-4000-8000-000000000000' : fixture.nonce);
  const cors = {'access-control-allow-origin': '*'};
  await page.route('https://enclave.trymaple.ai/**', route => scenario.enclaveDown
    ? route.fulfill({status: 503, headers: cors, body: 'unavailable'})
    : route.fulfill({json: {attestation_document: fixture.attestation_document}, headers: cors}));
  await page.route('https://raw.githubusercontent.com/MaplePrivacyLabs/**', route => route.fulfill({json: {PCR0: scenario.provider}, headers: cors}));
  await page.route('https://raw.githubusercontent.com/Lore-Hex/**', route => scenario.nyte
    ? route.fulfill({json: {pcr0: scenario.nyte, source_commit: 'c0ffee', eif_sha256: 'beef', review: 'https://github.com/Lore-Hex/quill-cloud-proxy/issues/1'}, headers: cors})
    : route.fulfill({status: 404, headers: cors, body: 'missing'}));
  await page.route('https://trustedrouter.com/**', route => route.fulfill({status: 204, headers: cors, body: ''}));
  await page.goto(`${base}/verified/maple.html`, {waitUntil: 'networkidle'});
  await page.waitForFunction(() => !['idle', 'running'].includes(document.getElementById('live-check').dataset.verdict));
  return {context, page, errors};
}

(async () => {
  await verifierControls();
  const browser = await chromium.launch({headless: true});
  try {
    for (const scenario of CASES) {
      const {context, page, errors} = await open(browser, 1440, scenario);
      assert.equal(await page.locator('#live-check').getAttribute('data-verdict'), scenario.name);
      assert.equal(await page.locator('.verdict-title').textContent(), scenario.title);
      assert.deepEqual(await page.locator('[data-step]').evaluateAll(rows => rows.map(row => row.dataset.state)), scenario.states);
      if (scenario.nyte && !scenario.enclaveDown && !scenario.replay) {
        assert.equal(await page.locator('dd[data-record="source_commit"]').textContent(), 'c0ffee');
        assert.equal(await page.locator('a[data-record="review"]').getAttribute('href'), 'https://github.com/Lore-Hex/quill-cloud-proxy/issues/1');
      }
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`PASS ${scenario.title}${scenario.replay ? ' (replayed document)' : ''}`);
    }
    for (const width of [1440, 390, 320]) {
      const {context, page, errors} = await open(browser, width, CASES[1]);
      await page.evaluate(() => document.fonts.ready);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `evidence overflow at ${width}`);
      await page.getByRole('button', {name: 'Run the check again'}).click();
      await page.waitForFunction(() => document.getElementById('live-check').dataset.verdict === 'pending');
      await page.screenshot({path: `${output}/nyte-verified-maple-${width}.png`, fullPage: true});
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`PASS ${width}px: layout, rerun`);
    }
    const context = await browser.newContext({javaScriptEnabled: false});
    const page = await context.newPage();
    await page.goto(`${base}/verified/maple.html`);
    assert.ok((await page.locator('.verdict-text').textContent()).includes('manual check below'));
    assert.ok((await page.locator('#manual pre').textContent()).includes('enclave.trymaple.ai/attestation/'));
    await context.close();
    console.log('PASS without JavaScript: manual check stands on its own');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
