# HANDOFF — multi-cloud + ClickHouse analytics

**Last updated 2026-07-31.** Written for an agent taking this over cold. Read
[`multi-cloud-separation.md`](multi-cloud-separation.md) and
[`analytics-ingestion.md`](analytics-ingestion.md) for the *why*; this document is the *where
we are and what is next*.

> **Production topology update:** the canonical operational document is now
> [`../clickhouse-reliability.md`](../clickhouse-reliability.md). ClickHouse is
> no longer a single shadow node. Provider analytics reads use a private,
> three-zone replicated cluster. Historical stage descriptions below remain
> useful design context but are not the current runbook.

---

## 1. The program in one paragraph

TrustedRouter should run as a **standalone deployment on each cloud** — its own database,
credits, API keys, analytics. Identity federates across clouds; credits do not. GCP is the
existing deployment. AWS and Azure are next, both on the **same** `PostgresStore`. Analytics
moves off Bigtable onto **ClickHouse**, one instance per cloud, because Bigtable is an
operational wide-column store and was never a columnar warehouse.

---

## 2. Status

| Piece | State |
|---|---|
| Separation decision | **Decided**, #307 |
| `PostgresStore` | **Merged**, #310 — passes conformance |
| `PostgresStore` on Spanner PG dialect | **Proven**, #322 — 14/14 |
| ClickHouse cluster on GCP | **Live**: three zones, three Keeper voters, 500 GB SSD per replica |
| Backfill (historical) | **Done** — 200k+ rows, per-day parity with Bigtable |
| Live ingestion | **Live and verified**, durable Spanner outbox plus hourly repair |
| Route-health differential proof (stage 2 gate) | **Passes**, #350 |
| Provider portal read cutover | **Live** through private regional load balancer |
| Immutable Parquet archive | **Live**, verified daily revisions in GCS, seven-year retention |
| Hour/day/month rollups | **Live**, parity-gated atomic partition replacement |
| AWS deployment | **Not started** |
| Azure deployment | **Not started** |

### The single most useful result

**One `PostgresStore` covers every target.** Aurora DSQL (AWS), Cosmos DB for PostgreSQL /
Citus (Azure), *and* Spanner's PostgreSQL dialect (GCP) are all Postgres-wire. This was
verified, not assumed — the credit path's primitives were probed directly on Spanner PG
before trusting the suite:

| Primitive | Spanner PG |
|---|---|
| `jsonb` store + read-back | works |
| `INSERT ... ON CONFLICT DO NOTHING` | rowcount **1**, then **0** on replay |
| `ON CONFLICT ... DO UPDATE` | works |
| conditional `UPDATE` rowcount | works |
| `SELECT ... FOR UPDATE` | works |

The second row is exactly-once credit. Because of this, adding a cloud does **not** multiply
the "different money code per backend" risk — there is one implementation.

---

## 3. What is running in production right now

### ClickHouse cluster

* `tr-clickhouse-1/2/3` run in `us-central1-a/b/c`, each on `e2-standard-4`
  with a 500 GB SSD and no external IP.
* One logical shard has three `ReplicatedReplacingMergeTree` replicas and a
  three-voter embedded Keeper quorum.
* Provider analytics readers use private load balancer `tr-clickhouse-ilb`.
* Exact raw queries use `FINAL`. Recomputed hourly, daily, and monthly tables
  replace verified partitions instead of using replay-unsafe additive views.
* Daily disk snapshots retain 30 days. Verified immutable Parquet retains raw
  history for seven years. See the canonical runbook linked above.

### Live ingestion

App → Spanner outbox → ingester → ClickHouse.

* **Outbox** `tr_analytics_outbox`, PK `(shard, commit_ts, event_id)`, `allow_commit_timestamp`,
  16 shards, plus `ROW DELETION POLICY (OLDER_THAN(commit_ts, INTERVAL 7 DAY))`.
* **Enqueue** in `storage_gcp_generations.py`, gated on `TR_ANALYTICS_OUTBOX_ENABLED`
  (currently `true` in `scripts/deploy/rollout.sh`). Separate transaction from settle,
  best-effort, logs `loss_tolerated` and `repairable_via`.
* **Ingester** `clickhouse/ingest_outbox.py`, systemd `tr-clickhouse-ingest.service`.
* **Reconciler** `clickhouse/reconcile_benchmark_samples.py`, systemd timer, hourly.
* Deployed by `scripts/deploy/clickhouse_live_ingestion.sh` (refuses to run if the node has an
  external IP).

Verified 2026-07-30: rows flowed, outbox depth held at **0**, `drain_lag_seconds=0.000`,
`clickhouse_insert_errors_total=0`, and the ingester's `rows_ingested_total` matched the
ClickHouse row delta exactly.

Provider portal analytics now read ClickHouse through the private load
balancer. Spanner remains authoritative for billing, and ClickHouse remains
off the inference and settlement critical path.

### Keyless cross-cloud identity (provisioned, currently unused)

GCP has WIF pool `multicloud`, provider `aws-workloads` (AWS account `330422590279`), and SA
`tr-multicloud@quill-cloud-proxy` with **database-scoped** Spanner and **instance-scoped**
Bigtable — not project-level. `scripts/entrypoint.sh` materialises an `external_account`
config from a plain env var and **refuses key material**, so the seam cannot decay back into a
mirrored service-account key.

Under separation no *data* path needs this. It is for operational access (shared image
registry, bootstrap) only. **Do not build application data flow on it.**

---

## 4. Next tasks, in order

### 4.1 Clean up Spanner before adding clouds — issue #334

`tr_entities` holds **14.8M rows**; real business data is *185 workspaces, 251 API keys*. Three
problems:

1. **~5.45M dead rows.** `reservation`, `reservation_idemp`,
   `gateway_authorization_idempotency` stopped being written **2026-06-26**, matching the typed
   ledger cutover. The typed tables took over; the entity copies were never deleted. Tell:
   typed `tr_gateway_authorization` = 7,806 rows vs its entity twin = 3,064,299.
2. **~9.4M rows still growing with no TTL.** The DDL's 7 row-deletion policies are on the
   *typed* tables; `tr_entities` has none. `generation` + `generation_by_workspace` (5.9M) also
   duplicate Bigtable — check for readers before retiring.
3. **`tr_settle_outbox` at 549,173 rows.** Drain-then-delete is not deleting.

**Do this first.** Otherwise the multi-cloud work replicates the bloat into every new cloud.
Confirm no readers, then batched/partitioned DML — never large unbatched DML during a rolling
deploy.

### 4.2 Route-health read cutover

The differential proof passes (#350, `clickhouse/prove_route_health.py`). The cutover itself is
a separate change: point `evaluate_route_health` at ClickHouse behind a setting, keep the
Bigtable path as fallback, and require a restore story before ClickHouse becomes load-bearing.

### 4.3 Leaderboard proof, then cutover

Materially harder than route health: provider-balanced capped sampling
(`benchmark_samples.py`), organic/synthetic exclusions, **exact nearest-rank percentiles**,
sustained-throughput fallback (`leaderboard.py`), top errors, last-seen, weighted provider
aggregation. `clickhouse/prove_leaderboard.py` exists as a starting point.

### 4.4 Fix the reconciler: it detects but does not repair

It reports drift and **exits 1**, so the systemd unit reports failure hourly with no repair
path. Wire the backfill in as remediation, or make the exit code meaningful and alert on it.

### 4.5 AWS deployment

`PostgresStore` on **Aurora DSQL**, its own database, its own credits. Prior AWS tooling exists
in `quill-cloud-proxy/tools/` (`deploy-aws-control-plane.sh`, `deploy-aws-nitro.sh`,
`sync-secrets-to-aws.sh`) but **the AWS account is currently empty** — everything was torn
down. Also note `scripts/entrypoint.sh` had its cross-cloud unwrap path deleted at some point,
so the old task definition's `GCP_SA_KEY_KMS_WRAPPED` was consumed by nothing: "AWS worked
before" is not true as-built.

First real milestone: run `tests/conformance/` against a live DSQL cluster. Watch for DSQL's
optimistic concurrency (every abort is `40001`) and its DDL restrictions.

### 4.6 Azure deployment

`PostgresStore` on **Cosmos DB for PostgreSQL (Citus)**, distributed on `workspace_id` — the
schema is already keyed for it. Subscription is live: `2fc83893-ca6c-48e4-b090-8860fba33d33`,
tenant `2abe2fae-5c28-491d-af5a-6255b33e534e`. First milestone is again the conformance suite
against the real cluster; watch whether Citus accepts `INSERT ... ON CONFLICT DO NOTHING` with
the conflict target on a distributed table (ours puts the distribution column in the conflict
target, which is the supported case, but confirm rather than assume).

`attestation_azure.go` is an unmerged spike on branch `azure-attestation` in
`quill-cloud-proxy`, build-tag gated `cloud_azure`, never hardware-verified.

### 4.7 Per-cloud status pages

**No refactor needed** — the seam already exists at the CLI boundary:

* **Uptime** = `run_synthetic_once` (called from `routes/internal/synthetic.py` and
  `synthetic/cli.py`) → `SyntheticProbeSample` → status page. **Runs on every cloud**, because
  uptime is a property of *that deployment*; a global number would hide an AWS outage behind
  GCP's health.
* **Throughput** = `provider_rotation_probe` / `provider_throughput_probe` (called **only**
  from `synthetic/cli.py`) → `ProviderBenchmarkSample` → leaderboard. **GCP only**, because it
  measures the *providers*, who are identical from anywhere; splitting it would give three
  noisy small samples instead of one good one.

So AWS and Azure simply never schedule the throughput commands. No cross-cloud pipe.

---

## 5. Traps that have already cost real debugging time

**`created_at` is not commit order.** The Bigtable row key is derived from *event* time. A row
committing late sorts behind an already-consumed range and is **missed forever**. This is why
live ingestion uses a commit-timestamp outbox. Never build a live cursor on `created_at`.

**`gen#` in Bigtable is keyed by generation ID, not time.** "Newest N by scan order" is a
random sample across all history. This produced a confident, wrong "data loss" conclusion.

**SQL NULL is three-valued.** `NULL IN (...)` is NULL, survives `NOT`, and `WHERE` then
**drops the row** — silently under-counting failures on rows with no HTTP status. Use
`ifNull()`. Cost a 6/525 mismatch on the leaderboard proof. Any Python→SQL predicate port needs
a differential test on real data; review-by-eye will not catch it.

**A ClickHouse MV is not covered by `ReplacingMergeTree`.** MVs run per INSERT block before
source replacement, so re-ingestion permanently inflates aggregates. Measured: 3 loads of
30,832 rows → 92,500 view samples.

**Route health takes newest-48 per route BEFORE filtering.** SQL that filters first and limits
second considers rows production never saw. Use `row_number() OVER (PARTITION BY provider,
model ORDER BY created_at DESC)` then filter in an outer query.

**A `--limit`ed backfill reports false MISMATCHes** for days the scan never reached —
`bigtable=0` there means "not scanned", not "missing".

**ClickHouse correctly refuses a plain IAP TCP tunnel.** Traffic arrives from Google's IAP
range while the `tr` user allows only `10.0.0.0/8` plus loopback, so it 403s even with the
right password. Use an SSH local forward so the connection originates from the node's own
loopback:

```bash
gcloud compute ssh tr-clickhouse-1 --zone us-central1-a --tunnel-through-iap -- -N -L 18123:localhost:8123
```

**`STORE` is monkeypatch-hostile.** It forwards via `__getattr__`, so `monkeypatch.setattr(STORE, ...)`
installs an instance attribute that teardown "restores" as a method bound to a dead store —
poisoning every later test. Patch the **class**. Issue #333.

**Starlette answers 405 from the router without raising**, so Sentry's 5xx-only default never
saw it. A console form POSTed to a GET-only route and failed silently for users indefinitely.
`failed_request_status_codes` now includes 405.

**`.notice` is styled green.** `.notice.bad` had no rule, so every console error message
rendered as a success. Fixed, but check computed styles rather than assuming a class exists.

---

## 6. Credentials and access

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/tr-ops-local.json"
export CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="$HOME/.config/gcloud/tr-ops-local.json"
```

**You usually need both.** The first is for the client libraries; the second is for the
`gcloud` CLI itself — without it, gcloud uses the operator's reauth-prone user credentials and
fails non-interactively. Do **not** export these in a shell profile; that hijacks the
operator's own interactive gcloud.

SA `tr-ops-local@quill-cloud-proxy` holds instance-scoped Spanner `databaseAdmin` +
`databaseUser`, instance-scoped Bigtable `user`, and project-level `compute.instanceAdmin.v1`,
`iap.tunnelResourceAccessor`, `secretmanager.secretAccessor`. It does **not** hold Cloud Run
read or project-level IAM admin — ask a human for those.

Node access is IAP only:

```bash
gcloud compute ssh tr-clickhouse-1 --zone us-central1-a --project quill-cloud-proxy --tunnel-through-iap
```
