# Storage portability: running TrustedRouter outside GCP

Handoff document. Goal: run the control plane end-to-end on AWS (then Azure)
without a risky rewrite of the billing core.

Status as of 2026-07-26:

| Phase | State |
|---|---|
| 0. Behavioural storage conformance suite | **landed** (#288) |
| 1. ClickHouse analytics — proof on real data | **done, exact match on 500 routes** |
| 4. Leak closures — cloud SDKs behind ports | **landed** (#289) |
| 2. ClickHouse in parallel on GCP (dual-write + verify) | **next** |
| 3. AWS test cluster (ClickHouse + remote Spanner) | tooling already exists — mostly a re-run |
| 5. Azure | spike done for attestation; Entra→GCP WIF outstanding |
| — Postgres/`PostgresStore` port | **deliberately NOT on the path** |

Ordering note: phase 4 landed before phase 2 because it is a pure decoupling
with no deployment risk, and it is what lets a non-GCP process start at all.

---

## The architecture decision

> **SUPERSEDED 2026-07-27 by [`multi-cloud-separation.md`](multi-cloud-separation.md).**
> Each cloud is now a **standalone deployment with its own database**: identity
> federates across clouds, credits and API keys do not. The shared-Spanner plan
> below is kept because its *analysis* is still what makes the new decision
> legible — the tradeoffs in "What this trades away" are exactly the costs that
> separation avoids, and the phased delivery still applies. Read the new
> document first; treat the bullets immediately below as history.

The naive plan is "replace Spanner and Bigtable with cloud-native equivalents
on each cloud." That is three ports of the hardest, most dangerous code we
own (reserve/settle billing), once per cloud.

**We are not doing that.** Instead:

* **Spanner stays the system of record — even when compute runs on AWS.**
  Spanner is reachable over its API from anywhere and is multi-region HA
  across zones. The control plane on AWS talks to the same Spanner instance
  that GCP does. No billing port, no dual-writing money, no migration of the
  thing that must never be wrong.
* **Only analytics becomes portable**, via ClickHouse — which runs identically
  on GCP, AWS, Azure, and self-hosted/appliance.

This buys multi-cloud *compute* now and defers multi-cloud *data* until there
is a concrete reason to pay for it.

### What this trades away (know these before phase 3)

1. **Cross-cloud latency on the hot path.** Every inference request does an
   authorize (Spanner write) and a settle (Spanner write). From AWS these
   become cross-cloud round trips. Budget for tens of ms each way and
   **measure it in the phase-3 test cluster before committing** — pair the AWS
   region with the nearest Spanner region (e.g. `us-east-1` ↔ `nam6`).
   If the added TTFB is unacceptable, that measurement — not a guess — is what
   justifies starting the Postgres port.
2. **A hard GCP dependency remains.** This is multi-cloud compute, not
   provider independence. A GCP outage or account action still takes the
   control plane down wherever it runs.
3. **Egress cost** on every Spanner round trip from AWS.

None of these are blockers; they are the things to measure rather than assume.

---

## Why Bigtable was the wrong home for analytics

Bigtable is a wide-column **operational** store — LSM-tree sorted map, built
for point reads and range scans. It is not a columnar analytics warehouse;
"column family" is not "column store". The GCP analytics warehouse is
BigQuery, and this repo contains **zero** BigQuery usage.

Worse, our schema forecloses even the partial benefit Bigtable offers. Bigtable
puts column families in separate locality groups, so a field-per-qualifier
layout would give some projection pushdown. We write **one column family
(`m`), one cell, containing a JSON blob**. So every analytic read is: scan
rows → `json.loads` each one in Python → aggregate in app memory.

Evidence in-tree:

* `storage_gcp_benchmark_index.py`, `storage_gcp_activity_index.py`,
  `storage_gcp_synthetic_rollups.py` — `json.loads` per row.
* `usage_series` implemented **five times** across storage modules.
* ~245 lines of hand-rolled pre-aggregation
  (`storage_gcp_synthetic_rollups.py` + `synthetic/backfill_rollups.py` +
  `synthetic_rollup#` row keys) — that machinery exists *because* the store
  cannot aggregate.

Bigtable remains a fine fit for the operational half (newest-N lookups for
route health, the activity feed). The split is the point: keep it for that,
stop using it as a warehouse.

---

## Phase 1 result (done): ClickHouse reproduces production exactly

`clickhouse/prove_leaderboard.py` is a differential test, not a demo:

1. Read real `ProviderBenchmarkSample` rows from production Bigtable (read-only).
2. Compute route health in Python **exactly as `synthetic/route_health.py`
   does today** — the current production answer.
3. Load the same rows into ClickHouse.
4. Compute the same thing in one SQL statement.
5. Assert agreement route by route.

Result on **40,000 real rows / 500 routes: EXACT MATCH**, including an
identical flagged set (the routes that would actually alert).

### What it proves, precisely

It proves that **given the same input rows**, the SQL translation of
`evaluate_route_health` matches the Python original — same per-route counts,
same flagged set — under the real rules: 48-hour cutoff, newest-48-per-route,
synthetic-only, `unsupported` excluded, transient failures excluded,
`min_samples=6`, `failure_rate>=0.95`. That is the risky part of the port, and
it is where the NULL bug below lived.

It does **not** prove:

* the full public leaderboard aggregator (`synthetic/leaderboard.py`), which
  also blends organic traffic, computes exact nearest-rank percentiles and
  ranks providers. ClickHouse's `quantile()` is *approximate* and has not been
  reconciled against that exact implementation. Do not swap it in blind.
* anything about the ingestion path — both sides read one slice.

Because both sides read the same slice, a biased slice would let them agree
while diverging from production (which reads per-route). The script therefore
**asserts** that the scan reaches back past the 48h cutoff before comparing;
if it doesn't, it exits rather than reporting a meaningless match.

Run it:

```bash
docker run -d --name tr-clickhouse -p 18123:8123 -p 19000:9000 \
  -e CLICKHOUSE_DB=tr -e CLICKHOUSE_USER=tr -e CLICKHOUSE_PASSWORD=tr \
  clickhouse/clickhouse-server:latest

# apply schema (strip -- comments before splitting on ';' — the prose
# contains semicolons)
# then:
export GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/tr-ops-local.json
uv run --with google-cloud-bigtable python clickhouse/prove_leaderboard.py
```

> **This is a local proof harness, not a monitor.** By default it TRUNCATEs
> the analytics table and rebuilds the materialized view. Never point it at a
> production ClickHouse. Use `--no-load` to compare against data already
> present.

### The bug this proof caught — read this before writing any porting SQL

First run mismatched on 6 of 525 routes. SQL reported **0 failures** where
Python found some, and dropped exactly as many rows as Python counted as
failures.

Cause: `error_status` / `error_type` are `Nullable`. In SQL's three-valued
logic `NULL IN (...)` is **NULL, not false**. That NULL propagates through
`OR` and `AND`, survives `NOT`, and `WHERE NULL` **drops the row**. Python's
`None in {...}` is plainly `False`.

So the naive translation silently under-counted failures on exactly the routes
whose errors carry no HTTP status (`empty_stream`, client-side aborts) — the
dead routes we most need to detect. Fix: flatten NULLs first
(`ifNull(error_status, 0)`, `ifNull(error_type, '')`).

**Generalise:** any Python→SQL port of predicate logic must be differentially
tested against the Python original on real data. Reviewing the SQL by eye
would not have caught this; the aggregate was plausible and wrong.

### What ClickHouse replaces — and what it does NOT

`route_health_hourly` (an `AggregatingMergeTree` materialized view) maintains
per-route rollups incrementally as rows arrive, with no scheduled job to fail
silently. Aggregate *states* are stored, so one view answers "last 48h" and
"last 90 days" without storing either.

Scope, precisely: it covers the **ProviderBenchmarkSample** dataset
(provider/model). It is **not** a replacement for
`synthetic/backfill_rollups.py`, which rolls up a *different* dataset —
`SyntheticProbeSample`, keyed by component/target/probe_type/region. Retiring
that job is separate work. (An earlier draft of this document claimed
otherwise; it was wrong.)

Two traps found the hard way, both now encoded in the schema comments:

1. **The view must repeat route-health's exclusions.** Counting every
   synthetic row and every error is *not* route health: on a 30.8k-row sample
   that yields 30832 samples / 1176 failures against the correct 28214 / 59 —
   a **20x overstatement of failures** that looks entirely plausible.
2. **`ReplacingMergeTree` does not make ingestion idempotent for the view.**
   Materialized views run per INSERT BLOCK, before any source-table
   replacement, so re-running a backfill permanently inflates the aggregates
   even though the source table collapses its duplicates. Measured: three
   loads of 30,832 rows left **92,500** samples in the view. Ingestion must be
   append-once, use `insert_deduplication_token`, or rebuild the view
   alongside any source reload.

It also makes questions cheap that were previously scripts. The same run
immediately surfaced that **every `google-ai-studio` Gemini route is at 100%
failure**, and confirmed `atlas-cloud/openai/gpt-5.1-codex-max` (previously
left unquarantined for lack of evidence) is dead at 11/11.

---

## Phase 0 (landed): the behavioural conformance suite

`tests/conformance/` — the acceptance test any storage backend must pass.

The pre-existing `tests/test_store_protocol_conformance.py` checks that both
backends declare the same method *names and signatures*. It cannot see
behaviour, so a backend that double-credits on retry, or lets a verification
token be redeemed twice, passes it happily.

The new suite pins semantics instead, talking **only to the `Store` protocol**:
exactly-once credit (both directions — no double-credit *and* no
over-dedupe), single-use wallet challenges / verification tokens / OAuth
codes, purpose-scoping, all three API-key lookup paths agreeing plus
revocation, session lifecycle, read-your-writes, and index newest-first /
limit / route-filter semantics.

Adding a backend = one entry in `conformance/conftest.BACKENDS`. Tests do not
change.

```bash
uv run pytest -q tests/conformance/     # 15 passed, 15 skipped
```

The 15 skips are the `spanner-emulator` backend, which is registered and skips
cleanly until emulator schema provisioning lands. **A skipped backend proves
nothing** — wiring the Spanner/Bigtable emulators is the next increment here,
and it is what turns this from an executable spec into actual cross-backend
enforcement.

### Known divergence this exposed

`InMemoryStore` deliberately does **not** implement `TypedBillingStore`, and
the Spanner store's legacy `reserve()` raises `RuntimeError`. The two backends
run **genuinely different money code paths today**, and nothing pins which
semantics are correct. Any new backend has to pick one. This is why the
typed-billing contract (leak #3) is explicitly *not* being touched yet.

---

## Phase 2 (next): ClickHouse in parallel on GCP

Goal: ClickHouse running alongside the live stack, continuously verified,
before anything depends on it.

1. **Stand up ClickHouse on GCP.** ClickHouse Cloud on GCP, or self-managed on
   GCE. Single node is fine at current volume.
2. **Dual-write.** Where `record_provider_benchmark` / `add_generation` write
   Bigtable, also enqueue to ClickHouse. Use `async_insert` or a buffer —
   **one synchronous row per generation is a ClickHouse anti-pattern.**
   Dual-write must be best-effort: a ClickHouse failure must never fail an
   inference request.
3. **Backfill** history from Bigtable. Re-running is safe for the *source
   table* (`ReplacingMergeTree` collapses duplicates on the sort key) but
   **not** for the materialized view — see the trap above. Either backfill
   once, carry an `insert_deduplication_token`, or rebuild the view after any
   reload.
4. **Verify continuously** with a comparison job — the *logic* of
   `prove_leaderboard.py`, but read-only. Do **not** schedule that script
   itself against production: it truncates by default. Extract the comparison
   into a job that only reads both stores and alerts on divergence.
5. **Flip reads** for the leaderboard only — lowest-risk surface: public,
   non-money, self-verifying against Bigtable.
6. Then flip `usage_series` / activity aggregates, and delete the rollup layer.

Verification gate for each step: the differential comparison stays exact.
Rollback: flip reads back to Bigtable; dual-write keeps both populated.

## Phase 3: AWS test cluster

Control plane on AWS + ClickHouse on AWS + **Spanner still in GCP**.

**AWS is already built — this is a re-run, not a build.** In
`quill-cloud-proxy/tools/`:

| Asset | What it does |
|---|---|
| `deploy-aws-control-plane.sh` | ECS/Fargate + ALB + ECR + secret wiring |
| `deploy-aws-nitro.sh` | the enclave on Nitro Enclaves |
| `sync-secrets-to-aws.sh` | mirrors GCP Secret Manager → AWS |
| `teardown-aws-control-plane.sh` | tears it down |
| `aws-nitro-root.pem` | pinned Nitro attestation root |

plus `attestation_aws.go` and `bootstrap_aws.go` in the enclave. Attestation
already has two backends, which is the hard part of supporting N.

**One wart to fix while you are there:** AWS reaches Spanner via
`GOOGLE_APPLICATION_CREDENTIALS` pointing at a **long-lived service-account
key JSON**. GCP Workload Identity Federation supports AWS natively, so that
key can be replaced with short-lived federated credentials. Do this rather
than copying the key pattern to a third cloud.

Must-measure before committing:

* Added p50/p95 latency on authorize and settle from AWS → Spanner. Pair the
  AWS region with the nearest Spanner region (`us-east-1` ↔ `nam6`). If the
  added TTFB is unacceptable, *that measurement* — not a guess — is what
  justifies starting a Postgres port.
* Egress cost per million requests.
* Behaviour under a cross-cloud network partition — specifically whether the
  settle-outbox drains correctly when Spanner is briefly unreachable.
  `storage_errors.is_transient_store_error` is what classifies that now.

## Phase 5: Azure — the genuinely new work

Nothing Azure exists in either repo today. The critical path is exactly two
things; everything else is configuration.

**1. `attestation_azure.go`.** Not optional — attested confidential compute is
the product, so TR on Azure without it is not TR. The good news is the shape:
Nitro returns a CBOR/COSE document the verifier parses itself, whereas both
GCP Confidential Space and Microsoft Azure Attestation return a **signed JWT**
from a cloud-operated issuer verified against its JWKS. So Azure follows
`attestation_gcp.go` closely, including the G6 session binding.

A working spike exists on the `azure-attestation` branch of quill-cloud-proxy
(vet/test/gofmt clean, **not** hardware-verified, **not** to be merged to
`main` without a go — `main` auto-deploys the enclave). Its one subtle part:
SEV-SNP gives exactly 64 bytes of caller-controlled `REPORT_DATA`, so the four
bound inputs are reduced to a single SHA-512 — exactly 64 bytes, no truncation
and no padding convention — with the pre-image also sent as `runtime_data` so
a verifier can recompute it and see *what* was bound.

**2. Entra ID → GCP Workload Identity Federation**, so Azure reaches Spanner
with no key file at all. Strictly better than what AWS does today.

Then: Container Apps (closest ECS-Fargate analog) or AKS with Confidential
Containers; AMD SEV-SNP VM families (DCasv5/ECasv5); Key Vault plus a
`sync-secrets-to-azure.sh`; ClickHouse Cloud on Azure or self-hosted on AKS —
already portable by construction from phase 1.

## Phase 4: leak closures — LANDED (#289)

Application code no longer imports a cloud SDK. Two ports own that knowledge:

* **`storage_errors`** — `StoreConflict` / `StoreUnavailable` plus
  `transient_store_error_types()` / `conflict_store_error_types()`. The
  transient set still resolves to exactly the six Google types the outbox
  parked on, asserted by test.
* **`key_management`** — a `KeyWrapper` port (`LocalAesKeyWrapper`,
  `GcpKmsKeyWrapper`) translating KMS failures to `KeyAccessDenied` /
  `KeyUnavailable`. **Adding AWS KMS or Azure Key Vault is one new class.**

Both import Google **lazily**, so a non-GCP deployment need not install the
Google libraries at all — previously `import trusted_router.main`
hard-required `google-api-core`.

`tests/test_cloud_sdk_boundary.py` keeps it true: it walks every module's AST
and fails if a cloud SDK is imported outside an allowlisted adapter. It draws
the distinction that matters — **infrastructure** (storage, secrets, key
wrapping, retry classification) must sit behind a port, while a vendor SDK
used to call that vendor's own product (`google.auth` for Vertex as an
upstream LLM provider, `boto3` for SES) is an ordinary integration that works
from any cloud and is *not* a portability blocker.

**Still outstanding here:** BYOK `unwrap` dispatches on **current settings**,
not the envelope's stored `key_ref`. Latent today, but a cloud migration or
key rotation would strand every existing envelope. Fixing it needs a re-wrap
migration of all DEKs, so it is deliberately separate.

<details>
<summary>Original phase-4 design notes (superseded by the above)</summary>

1. **GCP exceptions in app code** — `google.api_core.exceptions` is imported
   in `main.py`, `routes/byok.py`, `services/settle_outbox_apply.py` to make
   control-flow decisions. `src/trusted_router/storage_errors.py` (written,
   not yet wired) provides `StoreError`/`StoreConflict`/`StoreUnavailable` +
   `is_transient_store_error()` / `is_conflict_error()`, preserving the exact
   six-type transient set the outbox parks on, with Google imported **lazily**
   so a non-GCP deployment need not install the Google libraries at all.
2. **KMS** — `byok_crypto.py` calls `kms_v1` directly. `_wrap_dek` /
   `_unwrap_dek` already branch on `settings.byok_kms_key_name`, so a
   `KeyWrapper` port (`LocalAes` / `GcpKms` / `AwsKms` / `AzureKeyVault`)
   drops in cleanly.
   **Latent bug found:** decrypt dispatches on *current settings*, not on the
   envelope's stored `key_ref`. Switching clouds would strand every existing
   envelope. The port should dispatch on `envelope.key_ref`. This changes
   production crypto behaviour and deserves its own careful PR.
4. **`synthetic/backfill_rollups.py`** imports Bigtable directly. Largely moot
   once phase 2 lands — ClickHouse's materialized view replaces the script.

(#3, the typed-billing neutral contract, is intentionally deferred — see
"Known divergence" above.)

</details>

---

## Reference: what we actually use

**Spanner** — `run_in_transaction` ×55, `snapshot()` ×21, `execute_update`
×19, `commit_timestamp` ×8, `PENDING_COMMIT_TIMESTAMP` ×4. Multi-row ACID
transactions, serializable, SQL DML, server-side commit timestamps.
**No interleaved tables** — which is the single least-portable Spanner
feature, so a future port is more tractable than expected.

**Bigtable** — ~16 row-key families, all lexicographic prefix scans shaped
`<prefix>#<dims>#<reverse_ts>#<id>`, single column family `m`, single JSON
cell, append-only. This maps almost 1:1 onto DynamoDB (PK = `prefix#dims`,
SK = `reverse_ts#id`) or Cosmos NoSQL **if** we ever need the operational half
ported — but phase 2 removes the analytics reason to.

**KMS** — envelope encryption of customer BYOK keys.

**The seam is good.** `store_protocol.Store` is 102 methods in domain language
(`create_workspace`, `reserve_key_limit`) with no GCP vocabulary leaking, two
implementations already satisfy it, and `storage.create_store()` is a
config-driven factory (`TR_STORAGE_BACKEND`). The hard architectural work was
already done; what was missing is behavioural enforcement (phase 0) and an
analytics engine (phase 1).

## If someone later needs the Postgres port

Target the **PostgreSQL wire protocol**, not a specific product: one adapter
then covers CockroachDB (neutral + self-host), Aurora DSQL (AWS), Cosmos DB
for PostgreSQL / Azure SQL (Azure), and plain Postgres for local dev — which
would also replace `InMemoryStore` as a higher-fidelity test backend.

Caveat: wire-compatible is not behaviour-compatible (DSQL has no foreign keys
and a reduced PG surface; CockroachDB diverges on sequences and retry
semantics). Expect a thin dialect layer. **The conformance suite from phase 0
is the acceptance test** — write the backend against it.
