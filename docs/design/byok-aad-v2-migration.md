# BYOK envelope AAD v2 — migration plan

**Status:** proposed, not started
**Owner:** unassigned
**Blocks on:** nothing. The reachable exploit path is already closed (#544).
**Spans:** `quill-router` and `quill-cloud-proxy`. Ordering between them is the
whole difficulty — **and it repeats per cloud deployment**, see §4.0.

**Progress:** steps 1 and 2 are done **for the GCP deployment only**
(quill-cloud-proxy#162, quill-router#560). AWS and Azure have not been
sequenced. Steps 3 and 4 are not started anywhere.

---

## 1. The defect

AES-GCM associated data binds a ciphertext to its context: an envelope sealed
for one (workspace, provider) must fail to open in another. That guarantee holds
only if the map from context to AAD bytes is **injective**. Ours is not.

`byok_crypto.py:189`

```python
def _aad(workspace_id: str, provider: str) -> bytes:
    return f"trustedrouter:byok:{workspace_id}:{provider}".encode()
```

Colon-joined with no escaping and no length prefix, so component boundaries are
ambiguous:

```
_aad("a:b", "c") == _aad("a", "b:c") == b"trustedrouter:byok:a:b:c"
```

Worse, `encrypt_control_secret` (`byok_crypto.py:86-98`) routes its `purpose`
into the **same namespace** through the `provider` slot:

```python
def encrypt_control_secret(raw_secret, settings, *, workspace_id, purpose):
    return encrypt_byok_secret(raw_secret, settings,
                               workspace_id=workspace_id, provider=purpose)
```

So a BYOK provider key and a control secret in one workspace can share AAD, and
each will decrypt the other. Control-secret purposes are built by
`broadcast_secret_context(destination_id, "api_key" | "headers")`.

### What is already fixed, and what is not

The **reachable** path is closed. The console BYOK route used to accept any
lowercased string up to 64 characters as a provider name, so a tenant could
register `broadcast:bdst_…:api-key` and collide deliberately. It now validates
against the catalog exactly as the API route does (#544), and
`tests/test_byok_aad_namespace_property.py` holds that.

What remains is the **encoding**. Colon collisions need a colon inside a
workspace id, and all three backends mint `str(uuid.uuid4())` with no
caller-supplied id, so it is not reachable by normal issuance. It is still
wrong, and it is the kind of wrong that becomes reachable the moment someone
adds a customer-chosen identifier anywhere in the tuple.

`test_aad_encoding_is_not_injective_in_general` asserts the collision today so
this is not rediscovered. **That test should be deleted as part of this work.**

---

## 2. Why this is not a one-line fix

Three constraints, in increasing order of how much they hurt.

### 2.1 Both the ciphertext and the wrapped DEK are bound to the AAD

`byok_crypto.py:59-60`

```python
ciphertext    = AESGCM(dek).encrypt(nonce, plaintext, aad)
encrypted_dek = _wrap_dek(dek, dek_nonce, aad, settings)
```

Re-wrapping only the DEK is not enough. Any migration has to decrypt the
plaintext under the old AAD and re-encrypt both layers under the new one, which
means the migration job handles **plaintext provider keys in memory**. Treat it
with the same care as the enclave does.

### 2.2 The DEK wrap is a KMS operation, not a local one

`quill-cloud-proxy/enclave-go/internal/byokcache/kms_gcp.go:46-61` passes the
AAD to Cloud KMS as `additionalAuthenticatedData`. So the re-wrap is a network
call to KMS per envelope, not a local computation. Budget for rate limits and
partial failure — the backfill must be resumable.

### 2.3 The enclave gates on the exact algorithm string, and so do we

Both sides reject anything they do not recognise:

| | file | behaviour |
|---|---|---|
| control plane | `byok_crypto.py:78-79` | `raise ValueError("unsupported BYOK envelope algorithm")` |
| enclave | `byokcache/cache.go:214-215` | `return "", fmt.Errorf("unsupported envelope algorithm %q", …)` |

Both constants are the same literal, `TR-BYOK-ENVELOPE-AES-256-GCM-V1`.

**This is the ordering constraint.** If the control plane starts writing v2
envelopes before the enclave can read them, every affected BYOK key stops
working at the next inference request — a hard outage for exactly the customers
who took the trouble to bring their own keys.

---

## 3. The v2 format

```python
ALGORITHM_V2 = "TR-BYOK-ENVELOPE-AES-256-GCM-V2"

def _aad_v2(namespace: str, workspace_id: str, context: str) -> bytes:
    """Length-prefixed, so component boundaries are unambiguous.

    Each component is encoded as its byte length (4-byte big-endian) followed
    by its UTF-8 bytes. No choice of component values can produce the same
    byte string from a different tuple.
    """
    parts = [b"trustedrouter/byok/v2", namespace.encode(), workspace_id.encode(), context.encode()]
    return b"".join(len(p).to_bytes(4, "big") + p for p in parts)
```

Two changes, and both are load-bearing:

1. **Length prefixing** makes the encoding injective. Escaping would also work;
   length prefixes are harder to get subtly wrong and do not care about the
   delimiter character. Checked against 200 adversarial tuples (empty strings,
   embedded colons, NUL bytes, the literal prefix as a component): zero
   collisions, and both v1 collision classes separate.
2. **A `namespace` component** separates the two secret families — `"provider"`
   for BYOK keys, `"control"` for broadcast secrets — so a purpose can never
   collide with a provider even if the strings match exactly.

Keep `EncryptedSecretEnvelope` unchanged. `algorithm` already distinguishes the
formats and is already persisted, so no storage migration is needed.

> **Do not** implement this as a permanent "try v2, fall back to v1" on every
> decrypt. That keeps the substitution weakness alive forever and makes the
> migration impossible to declare finished. Dispatch on `envelope.algorithm`,
> which is exactly what it is for.

---

## 4. Sequencing

Each step ships and bakes independently. **Do not compress steps 1 and 2.**

### 4.0 — this whole sequence runs once PER CLOUD DEPLOYMENT

This was missing from the first version of this plan and it is the most
dangerous thing in it.

AWS and Azure are not regions of the GCP deployment. Per
`docs/storage-portability/multi-cloud-separation.md`, each cloud is a
**standalone TrustedRouter with its own database** — its own credits, API keys,
workspaces, and therefore **its own BYOK envelopes**. Only identity federates.

Two consequences:

1. **Steps 1 and 2 on GCP did not migrate AWS or Azure.** A v2 envelope written
   by the GCP control plane lands in the GCP database and is read by the GCP
   enclave. It never reaches another cloud's enclave. That is why merging step
   2 for GCP was safe with only the GCP enclave upgraded.

2. **The ordering constraint reappears on every deployment.** The moment an
   AWS or Azure control plane is updated to a `quill-router` build containing
   the step-2 commit, it starts writing v2 envelopes into *that* cloud's
   database. If that cloud's enclave has not already shipped step 1, every BYOK
   key there breaks.

   This is a trap, because the step-2 change arrives on those deployments as an
   ordinary version bump rather than as a deliberate migration step. **Nobody
   has to decide to run it.**

So, before any `quill-router` build containing quill-router#560 is deployed to
AWS or Azure:

- ship the enclave step-1 change for that cloud and roll it out fully
- verify the running build attests the right source commit, the same way GCP
  was verified against `trust.trustedrouter.com`
- only then let that cloud's control plane take the step-2 build

The enclave code needs no per-cloud change: `internal/byokcache` carries no
`//go:build` tag, so v2 read support is already compiled into the `cloud_aws`
and `cloud_azure` variants and CI exercises all of them. What is missing is a
**deploy and rollout** of a build containing it. As of writing,
`quill-cloud-proxy` has `deploy-enclave-gcp.yml` and a `workflow_dispatch`-only
`deploy.yml` ("Deploy AWS legacy"); there is no Azure deploy workflow in that
repo, so find the Azure enclave's actual deploy path before assuming it has
picked the change up.

Step 3's backfill is likewise per-deployment: it walks one cloud's database.

### Step 1 — enclave learns to read v2 (`quill-cloud-proxy`)

Teach `byokcache` both formats, dispatching on `envelope.Algorithm`:

- add `AlgorithmV2` alongside `Algorithm` in `cache.go:21`
- add `aadV2(namespace, workspaceID, context)` beside `aad()` at `cache.go:275`
- in `decryptEnvelope` (`cache.go:213`), select the AAD builder from the
  algorithm instead of rejecting anything that is not v1
- the control plane must tell the enclave which namespace a secret belongs to.
  It already sends provider identity in the authorization payload; confirm the
  field before writing the Go side, and do not infer it from the string shape.

Ship it. **Wait for it to be fully rolled out to every region.** A v2 envelope
reaching an old enclave build is the outage in §2.3.

Verify: an enclave build decrypts a v2 envelope produced by a scratch script,
and still decrypts existing v1 envelopes.

### Step 2 — control plane writes v2 (`quill-router`)

- add `ALGORITHM_V2` and `_aad_v2`
- `encrypt_byok_secret` writes v2 with `namespace="provider"`
- `encrypt_control_secret` stops delegating through the `provider` parameter and
  writes v2 with `namespace="control"` directly
- `decrypt_*` dispatch on `envelope.algorithm`: v2 uses `_aad_v2`, v1 uses the
  existing `_aad`, anything else raises
- new writes are v2; existing rows stay v1 and keep working

Verify: the property tests in `tests/test_byok_aad_namespace_property.py` extend
to cover cross-namespace rejection — a control secret must **not** decrypt as a
provider key with the same context string, which is the whole point.

### Step 3 — backfill

A resumable job over both surfaces:

| surface | field | context |
|---|---|---|
| `ByokProviderConfig` | `encrypted_secret` (`storage_models.py:211`) | provider slug |
| broadcast destination | `encrypted_api_key` (`storage_models.py:244`) | `broadcast_secret_context(destination_id, "api_key")` |
| broadcast destination | headers secret | `broadcast_secret_context(destination_id, "headers")` |

For each v1 row: decrypt under v1 AAD, re-encrypt under v2, write back in one
transaction, verify by decrypting the new envelope before committing.

Properties the job must have:

- **resumable** — it is a KMS round trip per row; assume it dies partway
- **idempotent** — re-running must skip rows already at v2
- **never destructive** — a row that fails to decrypt under v1 is logged and
  skipped, never deleted or overwritten. Alert on any such row: it means
  something else is already wrong.
- **rate-limited** against the KMS quota

`byok_cache_key` (`byok_crypto.py:129-156`) already includes the algorithm and
ciphertext, so re-encrypted rows get fresh cache keys automatically and stale
enclave cache entries expire on their own. No cache flush needed.

### Step 4 — drop v1

Only when a query proves zero v1 rows remain across every surface **and** the
backfill has been idle for at least one full retention window.

- remove `_aad`, `ALGORITHM`, and the v1 branches from both repos
- delete `test_aad_encoding_is_not_injective_in_general`
- keep a v1-shaped envelope in a test fixture asserting it is now **rejected**,
  so the removal is deliberate rather than silent

---

## 5. Rollback

| step | rollback |
|---|---|
| 1 | revert the enclave build. Nothing writes v2 yet, so this is free. |
| 2 | revert the control plane. Rows written as v2 in the interim will **not** decrypt on a v1-only control plane — so keep the read side of v2 in place and revert only the write side. Plan the revert as "stop writing v2", not "remove v2". |
| 3 | none needed; the job is non-destructive. Stop it and leave mixed v1/v2, which both planes read. |
| 4 | do not attempt this step until you are willing not to roll it back. |

Per-cloud: rolling back step 2 on one deployment does not affect the others,
since the databases are separate. But a cloud whose control plane has written
even one v2 envelope needs its enclave to keep v2 read support permanently.

The asymmetry in step 2 is the one to internalise: **v2 read support is
permanent from the moment it ships; v2 write support is the reversible part.**

---

## 6. Open questions for whoever picks this up

1. **Does the enclave receive the namespace, or must it infer one?** The Go side
   needs to know whether a secret is `provider` or `control`. If the
   authorization payload does not carry it, that field has to be added first,
   which makes this a three-repo change rather than two. **Check this before
   estimating.**
2. **Are there envelopes outside the three surfaces in §3?** The list came from
   grepping `encrypt_byok_secret` / `encrypt_control_secret` call sites in
   `quill-router`. If another service writes envelopes, the backfill misses them
   and they break at step 4.
3. **Is `provider` the right context for a BYOK key**, or should it be the
   storage slug from `byok_storage_provider_candidates`? They differ for aliased
   providers, and picking the wrong one produces envelopes that decrypt in
   testing and fail for exactly the aliased providers.

---

## 7. Why bother, given it is unreachable

Because the argument for its being unreachable is a fact about *today's*
identifier formats, not a property of the code. It rests on: every workspace id
is a UUID; the console validates provider names against the catalog; nothing
else feeds a caller-controlled string into that tuple. Each of those is one
feature away from being false, and none of them is stated anywhere near
`_aad`.

The encoding is also the cheap half. The expensive half — the enclave
coordination and the backfill — is what it costs, and it costs the same whenever
it is done. It gets more expensive as BYOK adoption grows, because step 3 is
linear in the number of stored envelopes.
