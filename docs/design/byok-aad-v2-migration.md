# BYOK envelope AAD v2 — migration plan

**Status:** deployed through step 3 on GCP, AWS, and Azure; step 4 is not started
**Owner:** TrustedRouter platform/security
**Blocks on:** nothing. The reachable exploit path is already closed (#544).
**Spans:** `quill-router` and `quill-cloud-proxy`. Ordering between them is the
whole difficulty — **and it repeats per cloud deployment**, see §4.0.

**Progress (2026-08-15):** the per-cloud sequence is complete through step 3.
Step 4 remains deliberately undone, so every enclave and control plane still
reads v1 envelopes during the retention window.

| Cloud | Step 1: enclave reads v1/v2 | Step 2: control plane writes v2 | Step 3: database backfill | Step-4 attestation |
|---|---|---|---|---|
| GCP | complete in every serving region | complete | 7 BYOK envelopes migrated; 7 v2, 0 v1 after an independent audit | **not recorded** |
| AWS | complete in Paris and Ireland | complete on both Fargate services and both App Runner services | read-only audit returned zero rows; nothing was rewritten | **not recorded** |
| Azure | complete in UAE North and Southeast Asia | complete | read-only audit returned zero rows; nothing was rewritten | **not recorded** |

The backfill implementation landed in quill-router#573. The standalone
operator configuration and mandatory explicit-KMS apply gate landed in
quill-router#576. The GCP migration temporarily granted the dedicated operator
identity key-scoped encrypt/decrypt access, then removed it automatically; the
post-migration IAM audit found no remaining operator binding. AWS and Azure
were read-only audits because their independent databases contained nothing to
rewrite.

**Read the AWS and Azure cells carefully, because an earlier revision of this
table did not let you.** They previously read "clean audit: no BYOK or Broadcast
secret rows existed", which renders as the same green check as GCP's migration
and is a different claim. "The audit found no rows" is also what a wrong resume
cursor, a renamed entity kind, a wrong database, or a credential scoped to the
wrong project produces — with exit code 0 and identical output. From inside the
audit those cases are indistinguishable, which means **no cell in this table,
however carefully worded, is a precondition for step 4.** The last column is,
because it is a file that a test reads: `byok-aad-v2-attestations.json`, written
only by `scripts/check_no_v1_envelopes.py`, which pairs the audit with a census
computed by different SQL — including one question that assumes neither the
entity kind nor the field name the envelope is stored under — and refuses to
call an uncorroborated empty result an attestation. It is empty today. Nobody
has run it against a real database.

And the last column is read as **one row, not three**: see §4.0 point 2. The
enclaves cross-read on control-plane failover, so a v1 envelope left in any one
database can be handed to any cloud's enclave.

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
this is not rediscovered. **That test should be deleted as part of this work** —
in step 4, with v1 itself, and not before. `tests/test_byok_v1_removal_gate.py`
holds those two together in both directions: while v1 envelopes are still
readable, that test is the only standing record of why any of this exists.

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

Three consequences:

1. **Steps 1 and 2 on GCP did not migrate AWS or Azure.** A v2 envelope written
   by the GCP control plane lands in the GCP database, so the AWS and Azure
   databases were untouched and their own rows still had to be walked.

   ⚠️ **An earlier revision of this bullet said that envelope "never reaches
   another cloud's enclave", and used that to argue merging step 2 for GCP was
   safe with only the GCP enclave upgraded. That is false, in both directions.**
   The enclaves are deployed with an *ordered, comma-separated* control-plane
   list and fail over to the next entry when the current one cannot be dialled
   (verified 2026-08-15):

   | where | what |
   |---|---|
   | `quill-cloud-proxy` `tools/deploy-aws-nitro.sh:888` | `QUILL_TR_CONTROL_PLANE_BASE_URL=https://aws.trustedrouter.com/v1,https://trustedrouter.com/v1` |
   | `quill-cloud-proxy` `tools/deploy-azure-aci.sh:269` | `TR_CONTROL_PLANE_BASE_URL=https://azure.trustedrouter.com/v1,https://trustedrouter.com/v1` |
   | `quill-cloud-proxy` `tools/deploy-gcp-mig.sh:208` | `TR_CONTROL_PLANE_BASE_URL=https://trustedrouter.com` (home only) |
   | `enclave-go/internal/trustedrouter/client.go:41-44` | "baseURLs is ordered: index 0 is this cloud's OWN control plane, later entries are fallbacks used only when an earlier one cannot be dialled." |
   | `client.go:789-826`, `:216` | `postToFirstDialable` walks that list; the `Authorization` it returns carries `byok_encrypted_secret`. |

   So an AWS or Azure enclave whose own control plane is undialable is served
   by the **home (GCP) plane**, out of the **GCP database**. Envelopes do cross
   clouds. What actually made the GCP step-2 merge survivable is the sentence
   further down this section: `byokcache/cache.go`, which holds the algorithm
   dispatch, carries no `//go:build` tag, so v2 *read* support was already
   compiled into the `cloud_aws` and `cloud_azure` variants — not isolation.

2. **Therefore step 4 is a fleet-wide decision, not a per-cloud one.** If one
   cloud drops v1 read support while any other cloud's database still holds a
   v1 envelope, that cloud's enclave breaks the next time it fails over — i.e.
   during a control-plane outage, on top of an existing incident, for exactly
   the customers who brought their own keys. The precondition for dropping v1
   **anywhere** is zero-v1 **everywhere**. That is why the ledger is read as a
   single verdict and why `--status-only` exits non-zero until every cloud is
   recorded; see `byok_v1_attestations.ENCLAVE_CONTROL_PLANE_SOURCES`, which
   transcribes the table above and is what the required set is derived from.

3. **The ordering constraint reappears on every deployment.** The moment an
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

The enclave code needs no per-cloud change: the file carrying the v1/v2
algorithm dispatch, `internal/byokcache/cache.go` (`Algorithm` at :26,
`AlgorithmV2` at :32, the `switch` at :346-348), has no `//go:build` tag, so v2
read support is already compiled into the `cloud_aws` and `cloud_azure`
variants and CI exercises all of them. The *package* is not tag-free — it also
holds `kms_http_aws.go` (`cloud_aws`), `kms_http_gcp.go` (`!cloud_aws`) and
`confidential_space_token{,_other}.go` (`cloud_gcp` / `!cloud_gcp`) — but none
of those touch the envelope format, so the conclusion holds and the earlier
package-level phrasing of it did not. What is missing is a
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
backfill has been idle for at least one full retention window. This is the one
step with no rollback row in §5, so its precondition is executable rather than
written down:

```bash
# once per standalone cloud — they are separate databases (§4.0)
uv run python scripts/check_no_v1_envelopes.py --backend spanner \
    --cloud gcp --record --operator you@lorehex.co
uv run python scripts/check_no_v1_envelopes.py --status-only   # what is still owed
```

**Exit 0 from either form means the whole fleet is clear, never that one cloud
is** (§4.0 point 2). A per-cloud run that attests its own cloud while others
still owe theirs exits 1, and `--status-only` exits 2 while the ledger blocks,
so `check_no_v1_envelopes.py --status-only && <next step>` is safe to write.

The check is read-only and needs no KMS access: it classifies envelopes by
their stored `algorithm`. It reports one of six outcomes and only two may be
recorded:

- `clean` — envelopes were seen and every one was v2.
- `empty_witnessed` — no envelope of any format was seen, a census reached the
  same table with the same credentials and found rows there, and a search for
  the literal `TR-BYOK-ENVELOPE-AES-256-GCM-V1` over whole row bodies — no kind
  filter, no assumption about which field holds an envelope — found nothing.
  That last clause is the one carrying the weight: the per-kind census and the
  walk restrict to the same two kinds (`MIGRATED_KINDS` on both counts and on
  the Postgres walk; the same two names hardcoded in SQL text on the Spanner
  walk), so a renamed entity kind or a renamed body field hides rows from both
  and they corroborate each other's silence. The literal search is a full table
  scan and is meant to be.

An empty result with nothing behind it is `zero_scan`, a loud refusal. A scan
that misses rows either census question can see is `scan_disagrees_with_census`.

**What it cannot tell you:** whether the credential pointed at the right
database. A wrong-but-populated `tr_entities` is reachable, non-empty and holds
no v1 envelope, which is what success looks like. The run records
`census_source` — which database the census was taken from — so check it
against the cloud the entry claims. **Read that field knowing which adapter
produced it.** On `--backend postgres` it is the server's own answer
(`current_database()`, `current_user`, `inet_server_addr()`). On `--backend
spanner` — i.e. the GCP invocation in the code block above — it is not: the
client builds `projects/…/instances/…/databases/…` by concatenating the
`--project`, `--spanner-instance` and `--spanner-database` you passed, with no
RPC, so it echoes your own arguments back and would look the same against a
local emulator. The recorded string says so. For GCP, corroborate the database
some other way before treating that line as evidence.

**One false positive worth knowing about:** the literal search is a text search
over whole row bodies, so any row that merely *mentions*
`TR-BYOK-ENVELOPE-AES-256-GCM-V1` — a captured provider error, a stored audit
line — counts and blocks that cloud with `scan_disagrees_with_census` until it
is removed or rewritten. That is on purpose: narrowing the pattern to a
JSON-shaped match would tie it to each adapter's serialisation, and a literal
search that stops matching fails open.

See the module docstrings in `src/trusted_router/byok_v1_attestations.py` and
`tests/test_byok_v1_precondition.py` for the rest of the scope limits.

Then, and only then:

- remove `_aad`, `ALGORITHM`, and the v1 branches from both repos
- delete `test_aad_encoding_is_not_injective_in_general`
- keep a v1-shaped envelope in a test fixture asserting it is now **rejected**,
  so the removal is deliberate rather than silent

`tests/test_byok_v1_removal_gate.py` fails CI if a v1-shaped envelope stops
opening while `docs/design/byok-aad-v2-attestations.json` does not attest zero
v1 on every cloud, and prints the commands above. Note that the third bullet
above — a fixture asserting a v1 envelope is now **rejected** — is itself an
edit that makes every stored v1 row unopenable, so it belongs after the ledger
is complete like the other two, not before. The gate decides by sealing a v1
envelope the way the stored rows were sealed and asking `decrypt_byok_secret`
and `decrypt_control_secret` to open it, rather than by reading the source for
`_aad`/`ALGORITHM`; that catches an entry-point rejection that leaves the v1
code physically in place, and stays quiet through refactors that keep v1
working. It permits the removal once the ledger is complete — it sequences the
work rather than forbidding it. It also holds that the collision record and v1
are deleted together: removing that test earlier drops the only standing
evidence that the v1 encoding is not injective.

Two things the gate deliberately does not do. It cannot see `quill-cloud-proxy`,
whose enclave carries its own v1 branch and its own removal decision, so
ordering across the two repos is still a human responsibility. And it opens a
v1 envelope wrapped by the local/test key wrapper, so a v1 regression confined
to the KMS wrapper is out of its scope.

---

## 5. Rollback

| step | rollback |
|---|---|
| 1 | revert the enclave build. Nothing writes v2 yet, so this is free. |
| 2 | revert the control plane. Rows written as v2 in the interim will **not** decrypt on a v1-only control plane — so keep the read side of v2 in place and revert only the write side. Plan the revert as "stop writing v2", not "remove v2". |
| 3 | none needed; the job is non-destructive. Stop it and leave mixed v1/v2, which both planes read. |
| 4 | do not attempt this step until you are willing not to roll it back. |

Per-cloud: rolling back step 2 on one deployment does not affect what the
others *write*, since the databases are separate. It does not follow that the
read side is per-cloud. Once **any** cloud's control plane has written even one
v2 envelope, **every** enclave that can be served by that control plane needs
v2 read support permanently — which, per §4.0 point 1, means the AWS and Azure
enclaves too, because both fail over to the home plane. (They have it: v2 read
support is compiled into every variant.)

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
   and they break at step 4. Still open — a grep cannot answer it. What is now
   handled is the *second* half of the risk: the surface list lives in one place
   (`byok_v1_attestations.MIGRATED_SURFACES`, from which the backfill's field map
   is derived) and is fingerprinted into every attestation, so adding a fourth
   surface invalidates every zero-v1 attestation taken before it existed rather
   than silently inheriting them.
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
