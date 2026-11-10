// NYTE evidence check for a provider's AWS Nitro enclave, and for NYTE's own
// gateway in front of it.
//
// Runs entirely in the visitor's browser. It asks the enclave for a fresh
// attestation document, verifies AWS's signature on it, and compares the
// measured image (PCR0) with two public records: the provider's own approval
// and the build NYTE reproduced independently. It then asks NYTE's gateway for
// its Google-signed attestation and reads the fingerprint that gateway release
// is built to accept. Every request is a plain GET that carries no data about
// the visitor.
//
// This file is deliberately short and unminified so it can be read in full.
(() => {
  'use strict';

  // SHA-256 of the AWS Nitro Enclaves root certificate (DER), published at
  // https://docs.aws.amazon.com/enclaves/latest/user/verify-root.html
  const AWS_NITRO_ROOT_SHA256 = '641a0321a3e244efe456463195d606317ed7cdcc3c1756e09893f3c68f79bb5b';
  // Google publishes the Confidential Space token signing keys here.
  const GOOGLE_ISSUER = 'https://confidentialcomputing.googleapis.com';
  const GOOGLE_KEYS = 'https://www.googleapis.com/service_accounts/v1/metadata/jwk/signer@confidentialspace-sign.iam.gserviceaccount.com';
  const RS256 = {name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256'};
  const P384 = {name: 'ECDSA', namedCurve: 'P-384'};
  const ES384 = {name: 'ECDSA', hash: 'SHA-384'};

  const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  const sameBytes = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
  const fromBase64 = text => Uint8Array.from(atob(text), c => c.charCodeAt(0));
  const fromBase64Url = text => fromBase64(text.replace(/-/g, '+').replace(/_/g, '/'));
  // A failed cryptographic check, as opposed to something this browser could not do.
  const rejected = message => Object.assign(new Error(message), {rejected: true});
  const outOfDate = message => Object.assign(new Error(message), {clock: true});

  // --- CBOR (RFC 8949): only what an attestation document uses. -------------
  function cbor(bytes) {
    let at = 0;
    const take = n => {
      if (at + n > bytes.length) throw new Error('attestation document is truncated');
      at += n;
      return bytes.subarray(at - n, at);
    };
    const uint = n => take(n).reduce((sum, b) => sum * 256 + b, 0);
    function item() {
      const first = take(1)[0];
      const major = first >> 5;
      const info = first & 31;
      let n;
      if (info < 24) n = info;
      else if (info === 24) n = uint(1);
      else if (info === 25) n = uint(2);
      else if (info === 26) n = uint(4);
      else if (info === 27) n = uint(8);
      else if (info === 31 && major >= 2 && major <= 5) n = -1; // indefinite length
      else throw new Error('unsupported CBOR encoding');
      const ended = () => bytes[at] === 0xff && (at += 1);
      if (major === 0) return n;
      if (major === 1) return -1 - n;
      if (major === 2 || major === 3) {
        let data;
        if (n >= 0) data = take(n);
        else {
          const parts = [];
          while (!ended()) { const part = item(); parts.push(typeof part === 'string' ? new TextEncoder().encode(part) : part); }
          data = new Uint8Array(parts.reduce((sum, p) => sum + p.length, 0));
          parts.reduce((offset, p) => (data.set(p, offset), offset + p.length), 0);
        }
        return major === 2 ? data : new TextDecoder().decode(data);
      }
      if (major === 4) {
        const list = [];
        if (n >= 0) for (let i = 0; i < n; i += 1) list.push(item());
        else while (!ended()) list.push(item());
        return list;
      }
      if (major === 5) {
        const map = new Map();
        if (n >= 0) for (let i = 0; i < n; i += 1) map.set(item(), item());
        else while (!ended()) map.set(item(), item());
        return map;
      }
      if (major === 6) return item(); // tag: use the tagged value
      if (info === 20) return false;
      if (info === 21) return true;
      if (info === 22 || info === 23) return null;
      throw new Error('unsupported CBOR value');
    }
    return item();
  }

  function cborByteString(data) {
    const n = data.length;
    const head = n < 24 ? [0x40 | n] : n < 256 ? [0x58, n] : n < 65536 ? [0x59, n >> 8, n & 255]
      : [0x5a, n >>> 24, (n >> 16) & 255, (n >> 8) & 255, n & 255];
    const out = new Uint8Array(head.length + n);
    out.set(head);
    out.set(data, head.length);
    return out;
  }

  // --- X.509 (DER): only the fields needed to walk a P-384 chain. -----------
  function der(bytes, start) {
    let at = start + 1;
    let length = bytes[at];
    at += 1;
    if (length & 0x80) {
      const count = length & 0x7f;
      length = 0;
      for (let i = 0; i < count; i += 1) { length = length * 256 + bytes[at]; at += 1; }
    }
    if (at + length > bytes.length) throw new Error('certificate is truncated');
    return {tag: bytes[start], start, body: at, end: at + length};
  }
  function children(bytes, node) {
    const list = [];
    for (let at = node.body; at < node.end;) { const child = der(bytes, at); list.push(child); at = child.end; }
    return list;
  }
  function derTime(bytes, node) {
    const text = new TextDecoder().decode(bytes.subarray(node.body, node.end));
    const full = node.tag === 0x17 ? (Number(text.slice(0, 2)) < 50 ? '20' : '19') + text : text;
    return Date.UTC(+full.slice(0, 4), +full.slice(4, 6) - 1, +full.slice(6, 8), +full.slice(8, 10), +full.slice(10, 12), +full.slice(12, 14));
  }
  function fixed48(bytes, node) { // DER INTEGER -> 48-byte big-endian
    let value = bytes.subarray(node.body, node.end);
    while (value.length > 48 && value[0] === 0) value = value.subarray(1);
    if (value.length > 48) throw new Error('unexpected signature size');
    const out = new Uint8Array(48);
    out.set(value, 48 - value.length);
    return out;
  }
  function certificate(bytes) {
    const [tbs, , signature] = children(bytes, der(bytes, 0));
    const fields = children(bytes, tbs);
    const first = fields[0].tag === 0xa0 ? 1 : 0; // optional [0] version
    const [, , issuer, validity, subject, spki] = fields.slice(first);
    const [notBefore, notAfter] = children(bytes, validity);
    const [r, s] = children(bytes, der(bytes, signature.body + 1)); // skip BIT STRING pad byte
    const raw = new Uint8Array(96);
    raw.set(fixed48(bytes, r));
    raw.set(fixed48(bytes, s), 48);
    return {
      der: bytes,
      signed: bytes.subarray(tbs.start, tbs.end),
      signature: raw,
      issuer: bytes.subarray(issuer.start, issuer.end),
      subject: bytes.subarray(subject.start, subject.end),
      spki: bytes.subarray(spki.start, spki.end),
      notBefore: derTime(bytes, notBefore),
      notAfter: derTime(bytes, notAfter),
    };
  }
  const publicKey = cert => crypto.subtle.importKey('spki', cert.spki, P384, false, ['verify']);

  // --- The attestation check itself. ----------------------------------------
  // Returns the signed fields, or throws with the reason the document is not
  // acceptable. `expectedNonce` is the fresh value this browser just sent.
  async function verifyAttestation(documentBytes, expectedNonce, now) {
    const cose = cbor(documentBytes);
    if (!Array.isArray(cose) || cose.length !== 4) throw new Error('not a COSE_Sign1 attestation document');
    const [protectedHeader, , payload, signature] = cose;
    if (![protectedHeader, payload, signature].every(part => part instanceof Uint8Array)) throw new Error('not a COSE_Sign1 attestation document');
    const doc = cbor(payload);
    if (!(doc instanceof Map) || !Array.isArray(doc.get('cabundle')) || !(doc.get('pcrs') instanceof Map)) throw new Error('unexpected attestation document layout');
    if (doc.get('digest') !== 'SHA384') throw new Error('unexpected digest algorithm');

    // 1. Certificate chain: root, intermediates, then the enclave's certificate.
    const chain = [...doc.get('cabundle'), doc.get('certificate')].map(certificate);
    const rootDigest = hex(new Uint8Array(await crypto.subtle.digest('SHA-256', chain[0].der)));
    if (rootDigest !== AWS_NITRO_ROOT_SHA256) throw rejected('chain does not start at the AWS Nitro root certificate');
    for (let i = 0; i < chain.length; i += 1) {
      const cert = chain[i];
      const issuer = chain[Math.max(i - 1, 0)]; // the root signs itself
      if (now < cert.notBefore || now > cert.notAfter) throw outOfDate('a certificate in the chain is outside its validity period');
      if (!sameBytes(cert.issuer, issuer.subject)) throw rejected('certificate chain is out of order');
      if (!await crypto.subtle.verify(ES384, await publicKey(issuer), cert.signature, cert.signed)) {
        throw rejected('a certificate signature in the chain is invalid');
      }
    }

    // 2. AWS's signature over the document, made with the enclave certificate.
    const context = new TextEncoder().encode('Signature1');
    const parts = [Uint8Array.of(0x84, 0x60 | context.length), context,
      cborByteString(protectedHeader), cborByteString(new Uint8Array(0)), cborByteString(payload)];
    const signed = new Uint8Array(parts.reduce((sum, p) => sum + p.length, 0));
    parts.reduce((offset, p) => (signed.set(p, offset), offset + p.length), 0);
    if (!await crypto.subtle.verify(ES384, await publicKey(chain[chain.length - 1]), signature, signed)) {
      throw rejected('the attestation signature is invalid');
    }

    // 3. Freshness: the document must carry the nonce this browser chose.
    const nonce = doc.get('nonce');
    if (!nonce || new TextDecoder().decode(nonce) !== expectedNonce) throw rejected('the attestation is not fresh (nonce mismatch)');

    if (![0, 1, 2].every(i => doc.get('pcrs').get(i) instanceof Uint8Array)) throw new Error('unexpected attestation document layout');
    return {
      pcr0: hex(doc.get('pcrs').get(0)),
      pcr1: hex(doc.get('pcrs').get(1)),
      pcr2: hex(doc.get('pcrs').get(2)),
      moduleId: doc.get('module_id'),
      timestamp: doc.get('timestamp'),
      certificates: chain.length,
    };
  }

  // Which of the two public records the live measurement matches. Both records
  // have to load before the page concludes anything from the comparison.
  function verdict(livePcr0, providerPcr0, nytePcr0) {
    if (!nytePcr0 || !providerPcr0) return 'incomplete';
    if (livePcr0 === nytePcr0.toLowerCase()) return 'verified';
    if (livePcr0 === providerPcr0.toLowerCase()) return 'pending';
    return 'mismatch';
  }

  // --- NYTE's own gateway (Google Confidential Space). -----------------------
  // The gateway returns a token signed by Google that names the exact container
  // image it is running. Returns the signed fields, or throws with the reason.
  async function verifyGatewayToken(token, keys, audience, now) {
    const parts = String(token).trim().split('.');
    if (parts.length !== 3) throw new Error('the reply is not an attestation token');
    const decode = part => JSON.parse(new TextDecoder().decode(fromBase64Url(part)));
    const header = decode(parts[0]);
    const jwk = header.alg === 'RS256' && Array.isArray(keys) && keys.find(k => k.kty === 'RSA' && k.kid === header.kid);
    if (!jwk) throw rejected('the token is not signed with one of Google’s published Confidential Space keys');
    const key = await crypto.subtle.importKey('jwk', {kty: 'RSA', n: jwk.n, e: jwk.e}, RS256, false, ['verify']);
    const signed = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
    if (!await crypto.subtle.verify(RS256, key, fromBase64Url(parts[2]), signed)) throw rejected('the token signature is invalid');
    const claims = decode(parts[1]);
    if (claims.iss !== GOOGLE_ISSUER) throw rejected('the token was not issued by Google Confidential Space');
    if (![].concat(claims.aud).includes(audience)) throw rejected('the token was issued for a different service');
    if (!(now >= (claims.nbf - 60) * 1000 && now <= (claims.exp + 60) * 1000)) throw outOfDate('the token is outside its validity period');
    // A debug image would let its operator log in to the machine.
    if (claims.swname !== 'CONFIDENTIAL_SPACE' || claims.dbgstat !== 'disabled-since-boot') {
      throw rejected('the gateway is not a production Confidential Space workload with debugging off');
    }
    const container = (claims.submods || {}).container || {};
    if (typeof container.image_digest !== 'string' || typeof container.image_reference !== 'string') {
      throw new Error('the token does not name a container image');
    }
    return {imageDigest: container.image_digest, imageReference: container.image_reference, hardware: String(claims.hwmodel), issuedAt: claims.iat * 1000};
  }

  const api = {cbor, verifyAttestation, verifyGatewayToken, verdict, fromBase64, AWS_NITRO_ROOT_SHA256, GOOGLE_KEYS};
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof document === 'undefined') return;

  // --- Page wiring. ---------------------------------------------------------
  const root = document.getElementById('live-check');
  if (!root) return;
  const gate = document.getElementById('gateway-check');
  const name = root.dataset.provider;
  const button = root.querySelector('.rerun');

  function panel(element, headlines) {
    const mark = (id, state, text, value) => {
      const row = element.querySelector(`[data-step="${id}"]`);
      row.dataset.state = state;
      row.querySelector('.step-result').textContent = text;
      const code = row.querySelector(':scope > code');
      if (code) { code.textContent = value || ''; code.hidden = !value; }
    };
    const show = (state, title, text, time) => {
      element.dataset.verdict = state;
      element.querySelector('.verdict-title').textContent = title;
      element.querySelector('.verdict-text').textContent = text;
      element.querySelector('.verdict-time').textContent = time;
    };
    return {
      mark,
      reset(text) {
        show('running', 'Checking…', text, '');
        element.querySelectorAll('[data-step]').forEach(row => mark(row.dataset.step, 'waiting', 'Waiting'));
      },
      conclude(state, detail) {
        show(state, headlines[state][0], detail || headlines[state][1], `Checked in your browser at ${new Date().toLocaleTimeString()}.`);
      },
      // A check that stopped at `stage`. Only a failed cryptographic check is
      // reported as "not verified"; anything else is this browser's problem.
      stop(stage, error, unreachable) {
        const why = error.clock ? `${error.message}; check that this device’s clock is right` : error.message;
        if (error.rejected) {
          mark(stage, 'bad', `Rejected: ${error.message}`);
          this.conclude('mismatch', `The check was rejected: ${error.message}.`);
        } else {
          mark(stage, 'bad', unreachable ? `Could not reach it (${why})` : `Could not check (${why})`);
          this.conclude('failed', unreachable ? undefined : `This browser could not finish the check: ${why}. Use the manual check below.`);
        }
      },
    };
  }
  const provider = panel(root, {
    verified: ['Verified just now', `The ${name} enclave answering right now runs the exact build NYTE reproduced from ${name}’s public source code.`],
    pending: ['Newer release, review in progress', `${name} has shipped a newer release. It matches the fingerprint ${name} published for production, and NYTE has not finished reproducing it yet. NYTE’s gateway stays pinned to the build it reproduced and sends nothing to this one until the review is done.`],
    mismatch: ['Not verified', `The enclave answering right now matches neither ${name}’s published approval nor the build NYTE reproduced.`],
    incomplete: ['Check incomplete', 'The enclave’s attestation is genuine, but one of the public records could not be loaded, so the comparison is unfinished. Try again, or use the manual check below.'],
    failed: ['Check could not finish', 'This browser could not complete the check. Use the manual check below.'],
  });
  const gateway = gate && panel(gate, {
    verified: ['Gateway attested, and it accepts this enclave', `NYTE’s gateway is running a published release inside sealed hardware. The one ${name} fingerprint built into that release is the one ${name}’s enclave reports right now.`],
    pending: ['Gateway attested, and holding back', `NYTE’s gateway is running a published release inside sealed hardware. The one ${name} fingerprint built into that release is not what ${name} is running now, so the gateway sends ${name} nothing until NYTE has reviewed the newer release.`],
    mismatch: ['Gateway not verified', 'NYTE’s gateway did not pass the check.'],
    incomplete: ['Gateway attested', `NYTE’s gateway is running a published release inside sealed hardware. The ${name} check above did not finish, so the two could not be compared.`],
    failed: ['Gateway check could not finish', 'This browser could not complete the gateway check. Use the manual check below.'],
  });
  const request = async url => {
    const response = await fetch(url, {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response;
  };
  const json = async url => (await request(url)).json();
  const fingerprint = (record, key) => (record && typeof record[key] === 'string' ? record[key].toLowerCase() : null);

  // Returns the measurement the enclave proved, or null if it proved nothing.
  async function checkProvider() {
    const sources = root.dataset;
    provider.reset(`Asking the ${name} enclave to prove what it is running.`);
    const nonce = crypto.randomUUID();
    const records = Promise.allSettled([json(sources.providerRecord), json(sources.nyteRecord)]);
    let live;
    let stage = 'fetch';
    try {
      const body = await json(`${sources.attestation}/${nonce}`);
      if (!body || typeof body.attestation_document !== 'string') throw new Error('the reply has no attestation document');
      provider.mark('fetch', 'ok', 'Received a fresh attestation document', `nonce ${nonce}`);
      stage = 'signature';
      live = await verifyAttestation(fromBase64(body.attestation_document), nonce, Date.now());
      provider.mark('signature', 'ok', `Signed by AWS Nitro hardware (${live.certificates} certificates, back to the AWS root)`, `PCR0 ${live.pcr0}`);
    } catch (error) {
      provider.stop(stage, error, stage === 'fetch');
      return null;
    }
    const [published, reproduced] = (await records).map(result => (result.status === 'fulfilled' ? result.value : null));
    const providerPcr0 = fingerprint(published, 'PCR0');
    const nytePcr0 = fingerprint(reproduced, 'pcr0');
    const result = verdict(live.pcr0, providerPcr0, nytePcr0);
    const compare = (id, pcr0, label) => {
      if (!pcr0) return provider.mark(id, 'bad', `Could not load ${label}`);
      const same = pcr0 === live.pcr0;
      // Amber when the other record explains the difference; red when neither matches.
      provider.mark(id, same ? 'ok' : result === 'mismatch' ? 'bad' : 'wait', same ? `Matches ${label}` : `Differs from ${label}`, `PCR0 ${pcr0}`);
    };
    compare('provider', providerPcr0, `${name}’s published approval`);
    compare('nyte', nytePcr0, 'the build NYTE reproduced');
    if (reproduced) {
      document.querySelectorAll('[data-record]').forEach(el => {
        const value = reproduced[el.dataset.record];
        if (typeof value !== 'string') return;
        if (el.tagName !== 'A') el.textContent = value;
        else if (value.startsWith('https://github.com/')) { el.href = value; el.textContent = value.replace('https://github.com/', ''); }
      });
    }
    provider.conclude(result);
    return live.pcr0;
  }

  // What NYTE's gateway is running, and which enclave it is built to accept.
  async function checkGateway(livePcr0) {
    const sources = gate.dataset;
    gateway.reset('Asking NYTE’s gateway to prove what it is running.');
    let stage = 'fetch';
    try {
      const [token, keys] = await Promise.all([request(sources.attestation).then(response => response.text()), json(GOOGLE_KEYS)]);
      gateway.mark('fetch', 'ok', 'Received the gateway’s attestation token');
      stage = 'signature';
      const claims = await verifyGatewayToken(token, keys && keys.keys, sources.audience, Date.now());
      const age = Math.max(0, Math.round((Date.now() - claims.issuedAt) / 60000));
      gateway.mark('signature', 'ok', `Signed by Google Confidential Space (${claims.hardware}, debugging off, issued ${age} min ago)`, `image ${claims.imageDigest}`);
      stage = 'release';
      const release = await json(sources.release);
      if (!release || !Array.isArray(release.accepted_image_digests)) throw new Error('NYTE’s release record could not be read');
      if (!release.accepted_image_digests.includes(claims.imageDigest)) throw rejected('the running image is not one NYTE has published');
      const commit = (/:gcp-release-([0-9a-f]{7,40})$/.exec(claims.imageReference) || [])[1];
      if (!commit) throw new Error('the image does not name its source release');
      gateway.mark('release', 'ok', 'One of the gateway images NYTE has published', `source ${commit}`);
      stage = 'pin';
      const pin = fingerprint(await json(sources.pin.replace('{commit}', commit)), 'pcr0');
      if (!pin) throw new Error('the release has no fingerprint record');
      if (!livePcr0) {
        gateway.mark('pin', 'ok', `Read the ${name} fingerprint this release accepts`, `PCR0 ${pin}`);
        gateway.conclude('incomplete');
      } else if (pin === livePcr0) {
        gateway.mark('pin', 'ok', `Accepts the ${name} enclave running now`, `PCR0 ${pin}`);
        gateway.conclude('verified');
      } else {
        gateway.mark('pin', 'wait', `Refuses the ${name} enclave running now`, `PCR0 ${pin}`);
        gateway.conclude('pending');
      }
    } catch (error) {
      gateway.stop(stage, error, stage === 'fetch');
    }
  }

  async function run() {
    button.disabled = true; // one check at a time, so results cannot interleave
    try {
      if (!globalThis.crypto || !crypto.subtle || !crypto.randomUUID) {
        const missing = new Error('this browser does not offer the cryptography needed');
        provider.reset(''); provider.stop('fetch', missing);
        if (gateway) { gateway.reset(''); gateway.stop('fetch', missing); }
        return;
      }
      if (gateway) gateway.reset('Waiting for the check above.');
      const live = await checkProvider().catch(error => (provider.stop('signature', error), null));
      if (gateway) await checkGateway(live);
    } finally {
      button.disabled = false;
    }
  }
  button.addEventListener('click', run);
  run();
})();
