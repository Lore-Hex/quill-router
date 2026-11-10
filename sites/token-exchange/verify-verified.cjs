// Provider evidence page: the in-browser attestation checks, offline.
// Network calls are answered from a recorded real attestation document and
// the browser clock is set to the moment it was recorded, so the signature
// and certificate checks run for real without depending on a live enclave.
// Gateway tokens are signed here with a throwaway key served as Google's.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fixture = require('./verified-fixture.json');
const verifier = require('./verified.js');
const base = process.argv[2] || 'http://127.0.0.1:8098';
const output = process.env.NYTE_PREVIEW_DIR || require('node:os').tmpdir();
const OTHER = 'ab'.repeat(48);
const REPLAYED = '00000000-0000-4000-8000-000000000000';

// --- A stand-in for Google's signing key and the gateway's token. ------------
const google = crypto.generateKeyPairSync('rsa', {modulusLength: 2048});
const stranger = crypto.generateKeyPairSync('rsa', {modulusLength: 2048});
const KEYS = {keys: [{...google.publicKey.export({format: 'jwk'}), kid: 'test-key', alg: 'RS256', use: 'sig'}]};
const DIGEST = `sha256:${'5e'.repeat(32)}`;
const COMMIT = '45ff2be8';
const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');

function gatewayToken(change = {}, signer = google.privateKey, header = {alg: 'RS256', kid: 'test-key', typ: 'JWT'}) {
  const issued = Math.floor(fixture.now / 1000) - 120;
  const claims = {
    iss: 'https://confidentialcomputing.googleapis.com', aud: 'quill-cloud', iat: issued, nbf: issued, exp: issued + 3600,
    hwmodel: 'GCP_INTEL_TDX', swname: 'CONFIDENTIAL_SPACE', dbgstat: 'disabled-since-boot',
    submods: {container: {image_digest: DIGEST, image_reference: `us-docker.pkg.dev/example/quill/enclave-multi:gcp-release-${COMMIT}`}},
    ...change,
  };
  const signed = `${encode(header)}.${encode(claims)}`;
  return `${signed}.${crypto.sign('sha256', Buffer.from(signed), signer).toString('base64url')}`;
}

// A check must fail for the stated reason, and say whether it was a failed
// proof (`rejected`) or something the browser could not evaluate.
async function refuses(label, promise, reason, kind) {
  await assert.rejects(promise, error => {
    assert.ok(error.message.includes(reason), `${label}: ${error.message}`);
    assert.equal(Boolean(error.rejected), kind === 'rejected', `${label}: rejected flag`);
    assert.equal(Boolean(error.clock), kind === 'clock', `${label}: clock flag`);
    return true;
  }, label);
}

// CBOR byte-string header, for rebuilding the COSE structure that gets signed.
const bstr = bytes => Buffer.concat([
  bytes.length < 24 ? Buffer.from([0x40 + bytes.length]) : bytes.length < 256 ? Buffer.from([0x58, bytes.length]) : Buffer.from([0x59, bytes.length >> 8, bytes.length & 255]),
  Buffer.from(bytes),
]);

// An attacker's document: change the measurement and sign it again with their
// own key. With `rekey`, also put that key into the leaf certificate.
function resigned(bytes, rekey) {
  const attacker = crypto.generateKeyPairSync('ec', {namedCurve: 'P-384'});
  const forged = Buffer.from(bytes);
  const measurement = forged.indexOf(Buffer.from(fixture.pcr0, 'hex'));
  forged[measurement + 5] ^= 1;
  if (rekey) {
    const point = jwk => Buffer.concat([Buffer.from([4]), Buffer.from(jwk.x, 'base64url'), Buffer.from(jwk.y, 'base64url')]);
    const leaf = new crypto.X509Certificate(Buffer.from(verifier.cbor(verifier.cbor(bytes)[2]).get('certificate')));
    const at = forged.indexOf(point(leaf.publicKey.export({format: 'jwk'})));
    assert.ok(at > 0, 'leaf key located');
    point(attacker.publicKey.export({format: 'jwk'})).copy(forged, at);
  }
  const [protectedHeader, , payload] = verifier.cbor(forged);
  const structure = Buffer.concat([Buffer.from([0x84, 0x6a]), Buffer.from('Signature1'), bstr(protectedHeader), Buffer.from([0x40]), bstr(payload)]);
  crypto.sign('sha384', structure, {key: attacker.privateKey, dsaEncoding: 'ieee-p1363'}).copy(forged, forged.length - 96);
  return Uint8Array.from(forged);
}

async function verifierControls() {
  const bytes = verifier.fromBase64(fixture.attestation_document);
  const attest = (document, nonce = fixture.nonce, now = fixture.now) => verifier.verifyAttestation(document, nonce, now);
  const good = await attest(bytes);
  assert.equal(good.pcr0, fixture.pcr0);
  assert.equal(good.certificates, 5);
  await refuses('replayed document', attest(bytes, REPLAYED), 'not fresh', 'rejected');
  await refuses('expired certificate', attest(bytes, fixture.nonce, fixture.now + 30 * 86400e3), 'validity period', 'clock');
  const at = Buffer.from(bytes).indexOf(Buffer.from(fixture.pcr0, 'hex'));
  assert.ok(at > 0);
  const measurement = Uint8Array.from(bytes); measurement[at + 5] ^= 1;
  await refuses('altered measurement', attest(measurement), 'attestation signature is invalid', 'rejected');
  const signature = Uint8Array.from(bytes); signature[signature.length - 3] ^= 1;
  await refuses('altered signature', attest(signature), 'attestation signature is invalid', 'rejected');
  // A certificate that is not AWS's: flip a byte inside the root certificate.
  const rootCertificate = verifier.cbor(verifier.cbor(bytes)[2]).get('cabundle')[0];
  const root = Buffer.from(bytes).indexOf(Buffer.from(rootCertificate));
  assert.ok(root > 0);
  const forged = Uint8Array.from(bytes); forged[root + 100] ^= 1;
  await refuses('forged root', attest(forged), 'AWS Nitro root', 'rejected');
  // Only the certificate signatures stop an attacker who swaps in their own leaf key.
  await refuses('re-signed without the leaf key', attest(resigned(bytes, false)), 'attestation signature is invalid', 'rejected');
  await refuses('re-keyed leaf certificate', attest(resigned(bytes, true)), 'certificate signature in the chain is invalid', 'rejected');
  // Something that is not an attestation document is a failed check, not a failed proof.
  await refuses('not a document', attest(Uint8Array.from([0x84, 0x40, 0xa0, 0x41, 0x00, 0x40])), 'attestation document', 'unreadable');

  assert.deepEqual(['verified', 'pending', 'mismatch', 'incomplete', 'incomplete', 'incomplete'], [
    verifier.verdict('aa', 'bb', 'AA'), verifier.verdict('aa', 'AA', 'bb'), verifier.verdict('aa', 'bb', 'cc'),
    verifier.verdict('aa', null, 'cc'), verifier.verdict('aa', 'aa', null), verifier.verdict('aa', null, 'aa'),
  ]);
  console.log('PASS enclave verifier: genuine accepted; replay, expiry, altered measurement, altered signature, forged root, re-keyed leaf rejected');
}

async function gatewayControls() {
  const check = (token, keys = KEYS.keys, now = fixture.now) => verifier.verifyGatewayToken(token, keys, 'quill-cloud', now);
  const good = await check(gatewayToken());
  assert.deepEqual([good.imageDigest, good.hardware, good.issuedAt], [DIGEST, 'GCP_INTEL_TDX', (Math.floor(fixture.now / 1000) - 120) * 1000]);
  assert.equal((await check(gatewayToken({aud: ['other', 'quill-cloud']}))).imageDigest, DIGEST);
  await refuses('signed by someone else', check(gatewayToken({}, stranger.privateKey)), 'signature is invalid', 'rejected');
  const [head, body, tail] = gatewayToken().split('.');
  const edited = encode({...JSON.parse(Buffer.from(body, 'base64url')), dbgstat: 'enabled'});
  await refuses('edited after signing', check(`${head}.${edited}.${tail}`), 'signature is invalid', 'rejected');
  await refuses('unknown key', check(gatewayToken({}, google.privateKey, {alg: 'RS256', kid: 'another'})), 'published Confidential Space keys', 'rejected');
  await refuses('unsigned token', check(gatewayToken({}, google.privateKey, {alg: 'none', kid: 'test-key'})), 'published Confidential Space keys', 'rejected');
  await refuses('no keys loaded', check(gatewayToken(), null), 'published Confidential Space keys', 'rejected');
  await refuses('wrong issuer', check(gatewayToken({iss: 'https://example.com'})), 'not issued by Google', 'rejected');
  await refuses('wrong audience', check(gatewayToken({aud: 'someone-else'})), 'different service', 'rejected');
  await refuses('debug image', check(gatewayToken({dbgstat: 'enabled'})), 'debugging off', 'rejected');
  await refuses('not Confidential Space', check(gatewayToken({swname: 'GCE'})), 'debugging off', 'rejected');
  await refuses('expired token', check(gatewayToken(), KEYS.keys, fixture.now + 2 * 3600e3), 'validity period', 'clock');
  await refuses('token from the future', check(gatewayToken(), KEYS.keys, fixture.now - 3600e3), 'validity period', 'clock');
  await refuses('no image named', check(gatewayToken({submods: {}})), 'does not name a container image', 'unreadable');
  await refuses('not a token', check('<html>'), 'not an attestation token', 'unreadable');
  console.log('PASS gateway verifier: genuine accepted; forged, edited, unknown key, unsigned, wrong issuer, wrong audience, debug, expired rejected');
}

// Each case: what the public sources report, and what each panel must conclude.
// `pin` is the Maple fingerprint built into the gateway release (default: the live one).
const OK = ['ok', 'ok', 'ok', 'ok'];
const STOPPED = ['bad', 'waiting', 'waiting', 'waiting'];
const ATTESTED = 'Gateway attested, and it accepts this enclave';
const CASES = [
  {name: 'verified', title: 'Verified just now', states: OK, gateway: 'verified', gatewayTitle: ATTESTED, gatewayStates: OK},
  {name: 'pending', nyte: OTHER, pin: OTHER, title: 'Newer release, review in progress', states: ['ok', 'ok', 'ok', 'wait'], gateway: 'pending', gatewayTitle: 'Gateway attested, and holding back', gatewayStates: ['ok', 'ok', 'ok', 'wait']},
  {name: 'mismatch', provider: OTHER, nyte: OTHER, pin: OTHER, title: 'Not verified', states: ['ok', 'ok', 'bad', 'bad'], gateway: 'pending', gatewayTitle: 'Gateway attested, and holding back', gatewayStates: ['ok', 'ok', 'ok', 'wait']},
  {name: 'incomplete', nyte: null, title: 'Check incomplete', states: ['ok', 'ok', 'ok', 'bad'], gateway: 'verified', gatewayTitle: ATTESTED, gatewayStates: OK},
  {name: 'incomplete', label: 'unreadable provider record', provider: 5, title: 'Check incomplete', states: ['ok', 'ok', 'bad', 'ok'], gateway: 'verified', gatewayTitle: ATTESTED, gatewayStates: OK},
  {name: 'incomplete', label: 'empty NYTE record', nyte: 'null', title: 'Check incomplete', states: ['ok', 'ok', 'ok', 'bad'], gateway: 'verified', gatewayTitle: ATTESTED, gatewayStates: OK},
  {name: 'failed', label: 'enclave unreachable', enclave: 'down', title: 'Check could not finish', states: STOPPED, gateway: 'incomplete', gatewayTitle: 'Gateway attested', gatewayStates: OK},
  {name: 'failed', label: 'enclave reply malformed', enclave: 'malformed', title: 'Check could not finish', states: STOPPED, gateway: 'incomplete', gatewayTitle: 'Gateway attested', gatewayStates: OK},
  {name: 'mismatch', label: 'replayed document', replay: true, title: 'Not verified', states: ['ok', 'bad', 'waiting', 'waiting'], gateway: 'incomplete', gatewayTitle: 'Gateway attested', gatewayStates: OK},
  {name: 'verified', label: 'gateway token forged', token: gatewayToken({}, stranger.privateKey), title: 'Verified just now', states: OK, gateway: 'mismatch', gatewayTitle: 'Gateway not verified', gatewayStates: ['ok', 'bad', 'waiting', 'waiting']},
  {name: 'verified', label: 'gateway debug image', token: gatewayToken({dbgstat: 'enabled'}), title: 'Verified just now', states: OK, gateway: 'mismatch', gatewayTitle: 'Gateway not verified', gatewayStates: ['ok', 'bad', 'waiting', 'waiting']},
  {name: 'verified', label: 'gateway image unpublished', accepted: [`sha256:${'00'.repeat(32)}`], title: 'Verified just now', states: OK, gateway: 'mismatch', gatewayTitle: 'Gateway not verified', gatewayStates: ['ok', 'ok', 'bad', 'waiting']},
  {name: 'verified', label: 'gateway token expired', token: gatewayToken({exp: Math.floor(fixture.now / 1000) - 600}), title: 'Verified just now', states: OK, gateway: 'failed', gatewayTitle: 'Gateway check could not finish', gatewayStates: ['ok', 'bad', 'waiting', 'waiting'], gatewayText: 'clock'},
  {name: 'verified', label: 'gateway unreachable', token: null, title: 'Verified just now', states: OK, gateway: 'failed', gatewayTitle: 'Gateway check could not finish', gatewayStates: STOPPED},
  {name: 'verified', label: 'gateway release record missing', accepted: null, title: 'Verified just now', states: OK, gateway: 'failed', gatewayTitle: 'Gateway check could not finish', gatewayStates: ['ok', 'ok', 'bad', 'waiting']},
];
const scenarioValue = (scenario, key, fallback) => (key in scenario ? scenario[key] : fallback);
const finished = () => ['live-check', 'gateway-check'].every(id => !['idle', 'running'].includes(document.getElementById(id).dataset.verdict));

async function open(browser, width, scenario) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => requests.push(request));
  await page.clock.install({time: fixture.now});
  // The page asks for a nonce it chose; the recording can only answer its own.
  await page.addInitScript(nonce => { crypto.randomUUID = () => nonce; }, scenario.replay ? REPLAYED : fixture.nonce);
  const cors = {'access-control-allow-origin': '*'};
  const missing = route => route.fulfill({status: 404, headers: cors, body: 'missing'});
  const record = (route, value) => (value === null ? missing(route) : value === 'null' ? route.fulfill({body: 'null', contentType: 'application/json', headers: cors}) : route.fulfill({json: value, headers: cors}));
  await page.route('https://trustedrouter.com/**', route => route.fulfill({status: 204, headers: cors, body: ''}));
  await page.route('https://enclave.trymaple.ai/**', async route => {
    if (scenario.delay) await new Promise(resolve => setTimeout(resolve, scenario.delay));
    if (scenario.enclave === 'down') return route.fulfill({status: 503, headers: cors, body: 'unavailable'});
    return route.fulfill({json: scenario.enclave === 'malformed' ? {error: 'busy'} : {attestation_document: fixture.attestation_document}, headers: cors});
  });
  await page.route('https://raw.githubusercontent.com/MaplePrivacyLabs/**', route => route.fulfill({json: {PCR0: scenarioValue(scenario, 'provider', fixture.pcr0)}, headers: cors}));
  await page.route('https://raw.githubusercontent.com/Lore-Hex/**', route => {
    const url = route.request().url();
    // The page's record of NYTE's reproduction lives on main; the gateway's pin lives at its release commit.
    if (url.includes(`/quill-cloud-proxy/${COMMIT}/`)) return record(route, {pcr0: scenarioValue(scenario, 'pin', fixture.pcr0)});
    assert.ok(url.includes('/quill-cloud-proxy/main/'), url);
    const nyte = scenarioValue(scenario, 'nyte', fixture.pcr0);
    return record(route, nyte && nyte !== 'null' ? {pcr0: nyte, source_commit: 'c0ffee', eif_sha256: 'beef', review: 'https://github.com/Lore-Hex/quill-cloud-proxy/issues/1'} : nyte);
  });
  await page.route('https://api.trustedrouter.com/**', route => {
    const token = scenarioValue(scenario, 'token', gatewayToken());
    return token === null ? route.fulfill({status: 503, headers: cors, body: 'unavailable'}) : route.fulfill({body: token, contentType: 'application/cbor', headers: cors});
  });
  await page.route('https://www.googleapis.com/**', route => route.fulfill({json: KEYS, headers: cors}));
  await page.route('https://trust.trustedrouter.com/**', route => {
    const accepted = scenarioValue(scenario, 'accepted', [`sha256:${'11'.repeat(32)}`, DIGEST]);
    return record(route, accepted && {accepted_image_digests: accepted});
  });
  await page.goto(`${base}/verified/maple.html`, {waitUntil: 'networkidle'});
  await page.waitForFunction(finished);
  return {context, page, errors, requests};
}

const panel = (page, id) => Promise.all([
  page.locator(`#${id}`).getAttribute('data-verdict'),
  page.locator(`#${id} .verdict-title`).textContent(),
  page.locator(`#${id} [data-step]`).evaluateAll(rows => rows.map(row => row.dataset.state)),
]);

(async () => {
  await verifierControls();
  await gatewayControls();
  const browser = await chromium.launch({headless: true});
  try {
    for (const scenario of CASES) {
      const {context, page, errors, requests} = await open(browser, 1440, scenario);
      assert.deepEqual(await panel(page, 'live-check'), [scenario.name, scenario.title, scenario.states], scenario.label);
      assert.deepEqual(await panel(page, 'gateway-check'), [scenario.gateway, scenario.gatewayTitle, scenario.gatewayStates], scenario.label);
      if (scenario.gatewayText) assert.ok((await page.locator('#gateway-check .verdict-text').textContent()).includes(scenario.gatewayText));
      if (scenario.name === 'verified') {
        assert.equal(await page.locator('dd[data-record="source_commit"]').textContent(), 'c0ffee');
        assert.equal(await page.locator('a[data-record="review"]').getAttribute('href'), 'https://github.com/Lore-Hex/quill-cloud-proxy/issues/1');
      }
      // The check only reads: every request is a GET with nothing attached.
      const checks = requests.filter(request => !request.url().startsWith(base) && !request.url().startsWith('https://trustedrouter.com/'));
      assert.ok(checks.length >= 3);
      for (const request of checks) assert.deepEqual([request.method(), request.postData()], ['GET', null], request.url());
      assert.equal(await page.locator('.rerun').isDisabled(), false);
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`PASS ${scenario.title} / ${scenario.gatewayTitle}${scenario.label ? ` (${scenario.label})` : ''}`);
    }
    for (const width of [1440, 390, 320]) {
      const {context, page, errors} = await open(browser, width, {...CASES[1], delay: 400});
      await page.evaluate(() => document.fonts.ready);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `evidence overflow at ${width}`);
      const rerun = page.getByRole('button', {name: 'Run both checks again'});
      await rerun.click();
      // One run at a time: a second click cannot start an overlapping check.
      assert.equal(await rerun.isDisabled(), true);
      assert.equal(await page.locator('#gateway-check').getAttribute('data-verdict'), 'running');
      await page.waitForFunction(finished);
      assert.deepEqual((await panel(page, 'live-check'))[0], 'pending');
      assert.deepEqual((await panel(page, 'gateway-check'))[0], 'pending');
      assert.equal(await rerun.isDisabled(), false);
      await page.screenshot({path: `${output}/nyte-verified-maple-${width}.png`, fullPage: true});
      assert.deepEqual(errors, []);
      await context.close();
      console.log(`PASS ${width}px: layout, rerun`);
    }
    const context = await browser.newContext({javaScriptEnabled: false});
    const page = await context.newPage();
    await page.goto(`${base}/verified/maple.html`);
    for (const id of ['live-check', 'gateway-check']) assert.ok((await page.locator(`#${id} .verdict-text`).textContent()).includes('manual check below'));
    const manual = await page.locator('#manual pre').textContent();
    assert.ok(manual.includes('enclave.trymaple.ai/attestation/') && manual.includes('api.trustedrouter.com/attestation'));
    await context.close();
    console.log('PASS without JavaScript: manual check stands on its own');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
