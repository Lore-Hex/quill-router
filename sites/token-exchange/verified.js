// NYTE evidence check for a provider's AWS Nitro enclave.
//
// Runs entirely in the visitor's browser. It asks the enclave for a fresh
// attestation document, verifies AWS's signature on it, and compares the
// measured image (PCR0) with two public records: the provider's own approval
// and the build NYTE reproduced independently. Nothing is sent to NYTE.
//
// This file is deliberately short and unminified so it can be read in full.
(() => {
  'use strict';

  // SHA-256 of the AWS Nitro Enclaves root certificate (DER), published at
  // https://docs.aws.amazon.com/enclaves/latest/user/verify-root.html
  const AWS_NITRO_ROOT_SHA256 = '641a0321a3e244efe456463195d606317ed7cdcc3c1756e09893f3c68f79bb5b';
  const P384 = {name: 'ECDSA', namedCurve: 'P-384'};
  const ES384 = {name: 'ECDSA', hash: 'SHA-384'};

  const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  const sameBytes = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
  const fromBase64 = text => Uint8Array.from(atob(text), c => c.charCodeAt(0));

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
    const doc = cbor(payload);
    if (doc.get('digest') !== 'SHA384') throw new Error('unexpected digest algorithm');

    // 1. Certificate chain: root, intermediates, then the enclave's certificate.
    const chain = [...doc.get('cabundle'), doc.get('certificate')].map(certificate);
    const rootDigest = hex(new Uint8Array(await crypto.subtle.digest('SHA-256', chain[0].der)));
    if (rootDigest !== AWS_NITRO_ROOT_SHA256) throw new Error('chain does not start at the AWS Nitro root certificate');
    for (let i = 0; i < chain.length; i += 1) {
      const cert = chain[i];
      const issuer = chain[Math.max(i - 1, 0)]; // the root signs itself
      if (now < cert.notBefore || now > cert.notAfter) throw new Error('a certificate in the chain is outside its validity period');
      if (!sameBytes(cert.issuer, issuer.subject)) throw new Error('certificate chain is out of order');
      if (!await crypto.subtle.verify(ES384, await publicKey(issuer), cert.signature, cert.signed)) {
        throw new Error('a certificate signature in the chain is invalid');
      }
    }

    // 2. AWS's signature over the document, made with the enclave certificate.
    const context = new TextEncoder().encode('Signature1');
    const parts = [Uint8Array.of(0x84, 0x60 | context.length), context,
      cborByteString(protectedHeader), cborByteString(new Uint8Array(0)), cborByteString(payload)];
    const signed = new Uint8Array(parts.reduce((sum, p) => sum + p.length, 0));
    parts.reduce((offset, p) => (signed.set(p, offset), offset + p.length), 0);
    if (!await crypto.subtle.verify(ES384, await publicKey(chain[chain.length - 1]), signature, signed)) {
      throw new Error('the attestation signature is invalid');
    }

    // 3. Freshness: the document must carry the nonce this browser chose.
    const nonce = doc.get('nonce');
    if (!nonce || new TextDecoder().decode(nonce) !== expectedNonce) throw new Error('the attestation is not fresh (nonce mismatch)');

    return {
      pcr0: hex(doc.get('pcrs').get(0)),
      pcr1: hex(doc.get('pcrs').get(1)),
      pcr2: hex(doc.get('pcrs').get(2)),
      moduleId: doc.get('module_id'),
      timestamp: doc.get('timestamp'),
      certificates: chain.length,
    };
  }

  // Which of the two public records the live measurement matches.
  function verdict(livePcr0, providerPcr0, nytePcr0) {
    if (nytePcr0 && livePcr0 === nytePcr0.toLowerCase()) return 'verified';
    if (!nytePcr0 || !providerPcr0) return 'incomplete';
    if (livePcr0 === providerPcr0.toLowerCase()) return 'pending';
    return 'mismatch';
  }

  const api = {cbor, verifyAttestation, verdict, fromBase64, AWS_NITRO_ROOT_SHA256};
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof document === 'undefined') return;

  // --- Page wiring. ---------------------------------------------------------
  const root = document.getElementById('live-check');
  if (!root) return;
  const sources = {
    attestation: root.dataset.attestation,
    provider: root.dataset.providerRecord,
    nyte: root.dataset.nyteRecord,
  };
  const name = root.dataset.provider;
  const step = id => root.querySelector(`[data-step="${id}"]`);
  function mark(id, state, text, value) {
    const row = step(id);
    row.dataset.state = state;
    row.querySelector('.step-result').textContent = text;
    const code = row.querySelector(':scope > code');
    if (code) { code.textContent = value || ''; code.hidden = !value; }
  }
  const HEADLINES = {
    verified: ['Verified just now', `The ${name} enclave answering right now runs the exact build NYTE reproduced from ${name}’s public source code.`],
    pending: ['Newer release, review in progress', `${name} has shipped a newer release. It matches ${name}’s own signed approval, and NYTE has not finished reproducing it yet. NYTE’s route stays pinned to the build it reproduced and sends nothing to this one until the review is done.`],
    mismatch: ['Not verified', `The enclave answering right now matches neither ${name}’s published approval nor the build NYTE reproduced.`],
    incomplete: ['Check incomplete', 'The enclave’s attestation is genuine, but one of the public records could not be loaded, so the comparison is unfinished. Try again, or use the manual check below.'],
    failed: ['Check could not finish', 'This browser could not complete the check. Use the manual check below.'],
  };
  function conclude(state, detail) {
    root.dataset.verdict = state;
    root.querySelector('.verdict-title').textContent = HEADLINES[state][0];
    root.querySelector('.verdict-text').textContent = detail || HEADLINES[state][1];
    root.querySelector('.verdict-time').textContent = `Checked in your browser at ${new Date().toLocaleTimeString()}.`;
  }
  const json = async url => {
    const response = await fetch(url, {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };

  async function run() {
    root.dataset.verdict = 'running';
    root.querySelector('.verdict-title').textContent = 'Checking…';
    root.querySelector('.verdict-text').textContent = `Asking the ${name} enclave to prove what it is running.`;
    root.querySelector('.verdict-time').textContent = '';
    ['fetch', 'signature', 'provider', 'nyte'].forEach(id => mark(id, 'waiting', 'Waiting'));
    if (!globalThis.crypto || !crypto.subtle || !crypto.randomUUID) {
      conclude('failed', 'This browser does not offer the cryptography needed for the check. Use the manual check below.');
      return;
    }
    const nonce = crypto.randomUUID();
    const records = Promise.allSettled([json(sources.provider), json(sources.nyte)]);
    let live;
    try {
      const body = await json(`${sources.attestation}/${nonce}`);
      mark('fetch', 'ok', 'Received a fresh attestation document', `nonce ${nonce}`);
      live = await verifyAttestation(fromBase64(body.attestation_document), nonce, Date.now());
      mark('signature', 'ok', `Signed by AWS Nitro hardware (${live.certificates} certificates, back to the AWS root)`, `PCR0 ${live.pcr0}`);
    } catch (error) {
      const id = live === undefined && step('fetch').dataset.state !== 'ok' ? 'fetch' : 'signature';
      mark(id, 'bad', id === 'fetch' ? `Could not reach the enclave (${error.message})` : `Rejected: ${error.message}`);
      conclude(id === 'fetch' ? 'failed' : 'mismatch', id === 'fetch' ? undefined : `The attestation document was rejected: ${error.message}.`);
      return;
    }
    const [provider, nyte] = await records;
    const providerPcr0 = provider.status === 'fulfilled' ? provider.value.PCR0 : null;
    const nytePcr0 = nyte.status === 'fulfilled' ? nyte.value.pcr0 : null;
    const compare = (id, pcr0, label) => {
      if (!pcr0) return mark(id, 'bad', `Could not load ${label}`);
      const same = pcr0.toLowerCase() === live.pcr0;
      mark(id, same ? 'ok' : id === 'nyte' ? 'wait' : 'bad', same ? `Matches ${label}` : `Differs from ${label}`, `PCR0 ${pcr0.toLowerCase()}`);
    };
    compare('provider', providerPcr0, `${name}’s signed approval`);
    compare('nyte', nytePcr0, 'the build NYTE reproduced');
    if (nyte.status === 'fulfilled') {
      const record = nyte.value;
      document.querySelectorAll('[data-record]').forEach(el => {
        const value = record[el.dataset.record];
        if (typeof value !== 'string') return;
        if (el.tagName !== 'A') el.textContent = value;
        else if (value.startsWith('https://github.com/')) { el.href = value; el.textContent = value.replace('https://github.com/', ''); }
      });
    }
    conclude(verdict(live.pcr0, providerPcr0, nytePcr0));
  }
  root.querySelector('.rerun').addEventListener('click', run);
  run();
})();
