# Analytics ingestion: design for 1T → 100T tokens/month

Companion to `README.md`. One question: **how analytics rows get from the
control plane into ClickHouse** at 1T tokens/month within ~2 months and 100T
soon after.

> **Revision history.** v1 of this document proposed a Bigtable high-water-mark
> replay feeding an additive materialized view, justified by a
> `parts/s = N/T` argument. An adversarial review found that design cannot
> survive a crash, a late write, or a replay. Three of its load-bearing claims
> were factually wrong against this repo. v2 (below) is the corrected design;
> §2 records the errors deliberately, because each one is a trap the next
> person will otherwise re-enter.

---

## 1. Measured facts

### Token distribution per organic generation

| p25 | p50 | p75 | p99 | mean |
|---|---|---|---|---|
| 421 | 4,104 | 464,756 | 987,811 | 197,673 |

Violently bimodal — small chat-shaped requests plus a heavy long-context tail.
The mean is dominated by the tail and is useless for capacity planning.

**Row rate cannot be derived from a token total.** At 100T tokens/month:

| assumed tokens/generation | generations/month | rows/sec |
|---|---|---|
| 197,673 (mean) | 506 M | ~195 |
| 4,104 (median) | 24.4 B | ~9,400 |
| 421 (p25) | 237 B | ~91,000 |

A **450× band**, decided by future traffic mix. Design for row rate, treat it
as unknown within two orders of magnitude, and **instrument it** so the real
number is observed rather than predicted.

### Volume today

synthetic ~240 rows/hour, synthetic_throughput ~29, organic ~11. Trivial. The
problems below are structural and appear at any volume.

### Storage

**54.3 compressed bytes/row** over 20k real rows. Treat *compressed bytes per
row* as the decision-relevant metric — not a compression ratio (see §5).

### Write path

`ProviderBenchmarkSample` (32 fields) is written for **every real generation**
(`gateway.py:1211`, `inference_quota.py:137`) plus synthetic probes.
`Generation` (63 fields) is not mirrored.

### Writer topology

Cloud Run `containerConcurrency=2`, `maxScale=100`, 4 regions ⇒ up to **400
writer processes**. Concurrency is 2 deliberately — each in-flight request can
consume 50–200 MB (`scripts/deploy/_lib.sh:33`) — so "just raise concurrency"
is a mitigation, not a design.

---

## 2. Why the in-process sink (PR #291) is wrong — corrected

PR #291 is merged but **disabled**, and must stay that way. Two of the
original three arguments hold; the headline one did not.

### 2a. Sound: Cloud Run does not reliably run background work

Cloud Run allocates CPU **during request processing**. A worker thread waking
on a timer gets little or no CPU between requests, and an instance is quietest
exactly when it is about to be scaled down. In-process background buffering is
structurally wrong on this runtime.

### 2b. Sound: `wait_for_async_insert=0` moves the loss window into the server

ClickHouse acks once rows are in its async-insert buffer, and that buffer has
no WAL. A restart drops rows that already returned 200. ClickHouse's own
guidance is `wait_for_async_insert=1` for reliable ingestion.

### 2c. **Wrong: `parts/s = N/T`**

v1 claimed 400 writers × 2s flush ⇒ 200 parts/s, therefore breakage "even at 1
row/second". That is not right, and the error is worth naming:

* The formula assumes every writer emits a **non-empty** insert every `T`. One
  row/second in total cannot produce 200 non-empty insert blocks/second.
* The sink does not flush on a fixed period anyway — it blocks for the first
  row, then drains what is present (`analytics_sink.py:147`), so under sparse
  traffic it tends toward one insert per row.
* `async_insert` exists precisely to coalesce compatible inserts from hundreds
  of clients, and the sink does send identical query text and settings.
* The positive-feedback story ("inserts delay → threads pile up → autoscale →
  more writers") is wrong for this code specifically: the request path does a
  non-blocking `put_nowait` and drops on pressure (`analytics_sink.py:121`).
  Only the sink's own worker ever blocks.

A better model is `parts/s ≈ flushes per node × insert shapes × partitions
touched × shards`, with size/time/queued thresholds making row and byte rate
matter too.

**Durable staging is still the answer — but justified by durability, crash
recovery, and request-path isolation, not by that formula.** I repeated a
striking claim without checking it against the code in front of me; the
correction is the reason this section exists.

---

## 3. The decision that comes first: what loss is acceptable?

**This is a product decision and it selects the architecture.** It cannot be
deferred.

The current code already answers it one way: `record_benchmark` catches every
exception and logs (`storage_gcp_generations.py:128`) — Bigtable analytics
writes are **best-effort, and lost rows are not repairable**. So:

> **Bigtable is not a durable log of every generation.** It is an index of the
> rows that happened to land. v1 called Source A "no data loss by
> construction". That was false.

Two coherent positions:

* **(A) Bounded loss is fine** (analytics, not billing). Then a best-effort
  source is acceptable, the SLO is stated explicitly (e.g. "<0.1% of rows,
  measured"), and reconciliation detects drift.
* **(B) Every generation must appear.** Then a **real durable outbox** is
  required at the point of settle — the same pattern already used for
  settlement (`storage_gcp_settle_outbox.py`) — and Bigtable's best-effort
  index cannot be the source.

**Recommendation: (A) now with a measured SLO, (B) if analytics ever becomes
customer-facing billing evidence.** Do not build (B)'s machinery while the
requirement is (A) — but do not claim (B)'s guarantees while running (A),
which is what v1 did.

---

## 4. Ingestion architecture (v2)

Principle retained from v1: **a small number of logical writers** feed
ClickHouse, not 400 app processes. Everything else changes.

### 4a. Ingestion is AT-LEAST-ONCE, and the schema must survive that

v1's idempotency argument was false in two ways:

* **IDs are random, not deterministic**: `id=f"bench-{uuid.uuid4().hex}"`
  (`storage_models.py:637`, `:673`). A replayed row gets a *new* id, so
  `ReplacingMergeTree` cannot collapse it at all.
* **`ReplacingMergeTree` dedups only on merge.** Queries before merge see
  duplicates unless they use `FINAL`.
* **`insert_deduplication_token` is a finite window**, not historical replay
  protection — and non-replicated tables need
  `non_replicated_deduplication_window`, which the schema does not set.

Required:

1. A **stable `event_id`** derived from the generation (not a fresh uuid4),
   plus `source_commit_version` for ordering repeats. This needs a small
   change at the write site or a deterministic derivation from
   (generation_id, provider, model, created_at).
2. **Checkpoint only after durable ClickHouse acknowledgement** — and accept
   that a crash between ack and checkpoint replays, which is why (1) matters.
3. Large synchronous inserts, or `async_insert=1` with
   **`wait_for_async_insert=1`**.

### 4b. Remove the additive materialized view (for now)

`route_health_hourly` is an `AggregatingMergeTree` MV. **MVs run per inserted
block and do not inherit `ReplacingMergeTree` collapsing** — the schema file
already says so (`001_provider_benchmark_samples.sql:82`) and the proof
measured it (three loads of 30,832 rows → 92,500 aggregate samples). v1
nonetheless asserted the MV "survives unchanged". It does not: **any replay
permanently inflates it.**

So: serve shadow queries from canonical raw (with `FINAL` at today's
volumes), and build rollups later by **recomputing closed hourly partitions
from raw**, with a lateness window — replacing, never accumulating.

### 4c. Sources: split historical from live

**Historical / reconciliation — Bigtable range scan.** Keep this; it is the
right tool for backfill and for periodic drift detection, and the work is not
wasted.

**Live — NOT a `created_at` high-water mark.** v1's cursor is broken: the row
key is `reverse_time_key(created_at)` (`storage_gcp_codec.py:45`), derived
from **event time, not Bigtable commit time**. A row whose `created_at`
precedes the checkpoint but which commits after it sorts *behind* the
already-consumed range and is **missed forever**. Long generations, retries,
clock skew and the planned eventually-consistent multi-cluster `route-any`
profile (`config.py:184`) all make this real.

Options, in order of preference:

1. **Durable outbox at settle** (position B) — correct by construction, reuses
   an established pattern in this codebase.
2. **Bigtable Change Streams** — carries server commit timestamps,
   continuation tokens, updates and partition watermarks; designed for exactly
   this.
3. **Range scan with a large explicit overlap window** — acceptable only as a
   temporary low-volume shadow, only with deduplication on canonical raw, and
   only with the maximum-lateness assumption written down.

**Object-storage staging remains the portable target**, but v1 hand-waved it.
Object stores have no shared append: it needs unique immutable object names,
schema version, checksum, atomic finalize, a manifest or notification, a
processed-object ledger, and retention. `gcs()`/`s3()` do not track what was
processed. And objects must be produced by a durable regional collector — not
from Cloud Run process memory, which reintroduces §2a.

---

## 5. Schema — measure, do not prescribe

v1 asserted several changes as conclusions. They are hypotheses.

* **Daily partitioning is not a parts fix.** Today's traffic lands in one
  current partition either way. Daily may be right for *retention
  granularity*, but a mixed-date backfill creates a part per partition per
  insert — so it needs partition-aligned backfill batches and measured part
  counts.
* **`id → UUID` is not a drop-in.** Production ids are `bench-` + 32 hex
  (`storage_models.py:637`), not 36-char UUIDs. Specify the transformation,
  validate historically, and decide whether the external id is retained.
* **"10× compression" is the wrong target.** Converting strings to native
  types lowers the *uncompressed* denominator and can make the ratio look
  worse while improving storage. Target **compressed bytes/row**, bytes
  scanned per query, and CPU.
* **Do not move `error_message` out yet.** Route-health displays the newest
  error message (`route_health.py:95`); splitting it breaks that, and absent
  nullable values already compress cheaply. Keep it with truncation + ZSTD
  until column-level measurement justifies otherwise.
* **Codecs are candidates, not conclusions.** Benchmark `Delta` vs
  `DoubleDelta` vs `T64` vs plain ZSTD on real data.
* **Two access paths, both explicit.** `(provider, model, created_at, id)` is
  right for newest-per-route; a global recent-time scan needs a time-first
  projection or a separately rebuilt rollup.
* **Sharding/replication is unspecified** and must not stay that way past
  stage 3: hashing by route helps locality but skews hot routes; hashing by
  event id balances writes but fans route queries across shards.

---

## 6. Prove the real consumers before any cutover

v1's stage 2 ("flip the leaderboard, then usage_series") is unsupported.

* **`route_health_hourly` does not reproduce route health.** Production takes
  the newest 48 rows per route *across all sources* and filters after
  (`route_health.py:73`); the MV filters first and has no newest-N operation.
  It also cannot supply the newest error message. The differential proof
  validates the *raw-table SQL* (which does use `row_number()` correctly) and
  **never validates the MV**.
* **The public leaderboard is materially unproven** — provider-balanced capped
  sampling (`benchmark_samples.py:51`), organic/synthetic exclusions, exact
  nearest-rank percentiles, sustained-throughput fallback
  (`leaderboard.py:251`), top errors, last-seen, weighted provider
  aggregation.
* **`usage_series`/activity cannot be served from this dataset at all**:
  `ProviderBenchmarkSample` deliberately omits workspace identity
  (`storage_models.py:588`). That needs a separate `Generation` pipeline.

Each consumer gets its own differential proof before its own cutover.

---

## 7. Staged plan

| Stage | Do | Move on when |
|---|---|---|
| **0** | Decide the loss SLO (§3). ClickHouse node up (**done** — `tr-clickhouse-1`, internal-only). Backfill history via Bigtable range scan. Canonical raw only, **no additive MV**. | backfill matches source counts per time bucket |
| **1** | Live shadow via the chosen source (§4c). Continuous **row-set** verification. No reads from ClickHouse. | verification stable 7 days, lag within SLO |
| **2** | Per-consumer: build the query, differentially prove it, then flip that one consumer. Route-health first (smallest surface), leaderboard second. | each proof passes on live data |
| **3** | Instrument real rows/s. Revisit sizing, replication, sharding, and self-host-vs-managed **with numbers**. | rows/s > 5,000 sustained or storage > 1 TB/month |
| **4** | Swap the source to object storage as part of the AWS move. | AWS cluster work begins |

**Replication/backup must not lag a read cutover** — v1 had stage 2 flipping
reads before stage 3 even considered replication. If a consumer reads from
ClickHouse, ClickHouse needs a restore story and a Bigtable read fallback.

---

## 8. Verification

The failure mode of an analytics migration is *plausible wrong numbers*, not
an outage.

* **Row-set comparison**, not just aggregate agreement: counts plus id/checksum
  sets per time bucket. Aggregates can agree while rows are wrong.
* **Wall-clock cutoffs.** The proof currently derives its window from
  `newest_ingested_row - 48h` (`prove_leaderboard.py:359`), which silently
  shifts the window backward and can *hide* ingestion lag. Use wall clock.
* The comparison runs as a **read-only** job. `prove_leaderboard.py` truncates
  by default — it is a local harness; extract the comparison, never schedule
  the script.
* Dashboard `system.part_log` parts/s and `system.asynchronous_insert_log`
  shape counts from day one.
* Alert on ingester lag, checkpoint age, and DLQ depth.

Operational gaps to close before stage 2: durable checkpoint store, leader
lease (what happens when two runs overlap), batch ids, retry caps, poison-row
isolation and a DLQ (ClickHouse rejects an **entire** async insert if one row
fails to parse), and backfill/live handoff.

---

## 9. What survives from PR #291

* **Delete**: `ClickHouseAnalyticsSink` and the `_StoreProxy` fan-out.
* **Keep**: `_row_from_sample` encoding and `_optional_int` normalisation — the
  latter prevents a `"429"`-vs-`429` divergence between the Python and SQL
  paths. They move to the ingester.
* **Keep**: the ClickHouse schema (minus the additive MV), the differential
  proof harness, and the route-health exclusion predicate.
