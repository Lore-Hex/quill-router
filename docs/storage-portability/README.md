# Storage portability: running TrustedRouter outside GCP

Handoff document. Goal: run the control plane end-to-end on AWS (then Azure)
without a risky rewrite of the billing core.

Status as of 2026-07-26:

| Phase | State |
|---|---|
| 0. Behavioural storage conformance suite | **landed (this branch)** |
| 1. ClickHouse analytics — local proof on real data | **done, exact match** |
| 2. ClickHouse in parallel on GCP (dual-write + verify) | next |
| 3. AWS test cluster (ClickHouse + remote Spanner) | after phase 2 |
| 4. Leak closures (#1 exceptions, #2 KMS, #4 backfill script) | designed, not landed |
| 5. Azure | later |
| — Postgres/`PostgresStore` port | **deliberately NOT on the path** |

---

## The architecture decision

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

Result on **40,000 real rows / 525 routes: EXACT MATCH.**

Run it:

```bash
docker run -d --name tr-clickhouse -p 18123:8123 -p 19000:9000 \
  -e CLICKHOUSE_DB=tr -e CLICKHOUSE_USER=tr -e CLICKHOUSE_PASSWORD=tr \
  clickhouse/clickhouse-server:latest

# apply schema (strip -- comments before splitting on ';' — the prose
# contains semicolons)
# then:
export GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/tr-ops-local.json
SCAN_LIMIT=40000 uv run --with google-cloud-bigtable \
  python clickhouse/prove_leaderboard.py
```

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

### What ClickHouse deletes

`route_health_hourly` (an `AggregatingMergeTree` materialized view) maintains
rollups incrementally as rows arrive. It replaces the entire hand-rolled
rollup layer — `storage_gcp_synthetic_rollups.py` and
`synthetic/backfill_rollups.py` — including the scheduled backfill job that
can fail silently. Aggregate *states* are stored, so one view answers "last
48h" and "last 90 days" without storing either.

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
3. **Backfill** history from Bigtable (idempotent — `ReplacingMergeTree` keyed
   on the sort key means re-running is safe).
4. **Verify continuously.** Run `prove_leaderboard.py`'s comparison as a
   scheduled job against live data. Divergence is a page.
5. **Flip reads** for the leaderboard only — lowest-risk surface: public,
   non-money, self-verifying against Bigtable.
6. Then flip `usage_series` / activity aggregates, and delete the rollup layer.

Verification gate for each step: the differential comparison stays exact.
Rollback: flip reads back to Bigtable; dual-write keeps both populated.

## Phase 3: AWS test cluster

Control plane on AWS + ClickHouse on AWS + **Spanner still in GCP**.

Must-measure before committing:

* Added p50/p95 latency on authorize and settle from AWS → Spanner.
* Egress cost per million requests.
* Behaviour under a cross-cloud network partition — specifically whether the
  settle-outbox drains correctly when Spanner is briefly unreachable
  (`storage_errors.is_transient_store_error` is what should be classifying
  that; see leak #1).

## Phase 4: leak closures (designed, not landed)

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
