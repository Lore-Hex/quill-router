# ClickHouse reliability and long-term analytics

This is the canonical production runbook for TrustedRouter provider analytics.
It supersedes the single-node and shadow-read descriptions in
`docs/storage-portability/`.

## Scope and data policy

ClickHouse stores bounded request metadata used by provider dashboards,
leaderboards, and route-health analysis. It does not store prompts, model
outputs, API keys, BYOK secrets, authorization headers, or workspace IDs.
Spanner remains the source of truth for billing and the durable analytics
outbox. A ClickHouse failure must not fail inference or settlement.

Long-term analytics uses tiers instead of retaining every hot row forever:

| Tier | Retention | Purpose |
|---|---:|---|
| Provider and activity raw rows | 400 days | Recent investigation and exact re-aggregation |
| Raw synthetic probe rows | 14 days | Fine-grained status diagnosis before compaction |
| Synthetic status rollups | 24 months | Public reliability history |
| Hourly provider rollups | 3 years | Detailed trends and provider operations |
| Daily and monthly provider rollups | No TTL | Long-term product and provider analytics |
| Verified Parquet archive in GCS | 7 years | Immutable, portable raw history and disaster recovery |
| Persistent-disk snapshots | 30 daily snapshots | Fast node recovery |

The 400-day raw TTL has not been shortened. Changing it requires a separate
retention decision after archive restore drills and rollup consumers are
verified.

## Production topology

The GCP deployment is one logical shard with three synchronous ClickHouse
replicas and a three-voter embedded Keeper quorum:

| Node | Zone | Disk |
|---|---|---:|
| `tr-clickhouse-1` | `us-central1-a` | 500 GB SSD |
| `tr-clickhouse-2` | `us-central1-b` | 500 GB SSD |
| `tr-clickhouse-3` | `us-central1-c` | 500 GB SSD |

All nodes have no external IP, VM deletion protection, non-auto-deleting boot
disks, the Google Ops Agent, and the daily snapshot policy. The canonical raw
table uses `ReplicatedReplacingMergeTree`; exact readers use `FINAL` because
the Spanner outbox is at-least-once.

Customer-facing readers use the regional internal passthrough load balancer:

```text
tr-clickhouse-ilb (10.128.0.96:8123)
  -> tr-clickhouse-1
  -> tr-clickhouse-2
  -> tr-clickhouse-3
```

The forwarding rule allows global VPC access so all control-plane regions can
reach it. The read-only `tr_provider_read` account can select only the raw and
rollup tables. Ingestion, reconciliation, archive, and rollup workers run on
node 1. Their durable input remains in Spanner if node 1 is temporarily down.

The pre-migration local node-1 table is retained as
`provider_benchmark_samples_local_backup`. Do not delete it until a later,
explicitly reviewed cleanup.

## Durable archive

`clickhouse/archive_daily.py` exports each closed UTC day with `FINAL` for
provider benchmarks, tenant activity, raw synthetic probes, and synthetic
status rollups. It computes a full row-set fingerprint, writes Parquet, reads
the Parquet back with `clickhouse-local`, and compares row count, hash sum, and
hash XOR before publishing a manifest.

Objects are immutable and revisioned:

```text
gs://quill-cloud-proxy-tr-clickhouse-archive/
  raw/<dataset>/day=YYYY-MM-DD/
    _latest.json
    revisions/<fingerprint>/manifest.json
    revisions/<fingerprint>/part-*.parquet
```

A rerun with unchanged data is a no-op. Late reconciled rows produce a new
immutable revision and atomically advance `_latest.json`. Bucket versioning,
uniform access, and public-access prevention are enabled. The daily service
rechecks seven closed days so late rows are captured. A separate daily restore
drill downloads every part for the previous closed day, validates object
SHA256 values, parses each Parquet file, and must reproduce the source
fingerprint before updating `archive-restore.json`.

## Aggregate tiers

`clickhouse/rollup_analytics.py` builds hourly, daily, and monthly tables from
raw `FINAL` rows. It never uses an additive materialized view because replayed
outbox events would double-count.

For each partition, the worker:

1. Recomputes into a staging table.
2. Verifies `sum(attempts)` equals the exact source row count.
3. Atomically replaces the live partition.
4. Verifies the published partition again.

The hourly timer rebuilds recent closed hours. The daily timer rebuilds daily
partitions and publishes monthly rows only after a complete calendar month
closes. Backfills are idempotent.

## Deployment

Run each script without `--apply` first. The mutation order is:

```bash
scripts/deploy/clickhouse_reliability.sh --apply
scripts/deploy/clickhouse_resize_disk.sh --apply
scripts/deploy/clickhouse_cluster.sh --apply
scripts/deploy/clickhouse_live_ingestion.sh
scripts/deploy/rollout.sh
scripts/deploy/prepare_bigtable_retirement.sh --apply
scripts/deploy/clickhouse_analytics_cutover.sh --apply
# Wait for the second clean seven-day soak.
scripts/deploy/retire_bigtable_runtime.sh --apply
```

The final script deploys one region at a time, switches to
`spanner-clickhouse` plus `clickhouse-only`, and disables Bigtable mirror
writes. It never deletes the Bigtable instance or its data. The retained copy
is rollback evidence until a separate, explicit deletion review.

`clickhouse_cluster.sh` stages all Keeper configs before restarts, starts the
two new voters together, migrates only after full-fingerprint parity, pauses
the ingester for the final delta, and exposes the load balancer only after the
canonical replicated table is healthy. Routine control-plane rollouts discover
the internal load-balancer address dynamically.

Never restart or deploy all three ClickHouse nodes together. Change one zone,
wait for replica queue and load-balancer health to recover, then continue.

## Health checks

Check load-balancer backends:

```bash
gcloud compute backend-services get-health tr-clickhouse-http \
  --project quill-cloud-proxy --region us-central1
```

On every replica, require no queue, no delay, and no read-only replica:

```sql
SYSTEM SYNC REPLICA provider_benchmark_samples;
SELECT queue_size, absolute_delay, is_readonly
FROM system.replicas
WHERE database = 'tr' AND table = 'provider_benchmark_samples';
```

Use a fixed cutoff at least five minutes in the past when comparing live
fingerprints, otherwise concurrent ingestion can make healthy replicas appear
different. Compare this on all three nodes:

```sql
SELECT
  count(),
  sum(cityHash64(tuple(*))),
  groupBitXor(cityHash64(tuple(*)))
FROM provider_benchmark_samples FINAL
WHERE created_at < toDateTime64('<fixed UTC cutoff>', 3, 'UTC');
```

The controlled zone-failure test is:

```bash
scripts/deploy/clickhouse_failover_smoke.sh
scripts/deploy/clickhouse_failover_smoke.sh --apply
```

The apply mode arms a remote automatic restart before stopping one node, runs
20 SQL reads through the load balancer with two zones healthy, restores the
node, waits for 3/3 health, and synchronizes the replica.

## Restore procedures

### Replace one failed replica

1. Remove or drain only the failed backend. Keep two healthy nodes serving.
2. Preserve the failed disk for investigation.
3. Provision a replacement VM in the same zone with the same private identity.
4. Install the cluster config and canonical replicated table.
5. Let ClickHouse fetch parts from a healthy replica.
6. Require queue zero, fixed-cutoff fingerprint parity, and healthy load-balancer state.
7. Re-enable the backend.

### Restore from a disk snapshot

1. Create a new disk from the newest valid `tr-clickhouse-daily-snapshots` snapshot.
2. Attach it to an isolated replacement VM with no external IP.
3. Start ClickHouse without registering it in the load balancer.
4. Verify table metadata and a fixed-cutoff fingerprint against a healthy replica.
5. Rejoin it as a replica, synchronize, then add it to the backend group.

### Restore from Parquet

1. Read each day's `_latest.json` and its immutable manifest.
2. Verify every object's SHA256 and the manifest's row-set fingerprint.
3. Load parts into a new staging table with the current schema.
4. Compare per-day fingerprints with the manifests.
5. Replicate the staged data, verify all replicas, then rename it into service.
6. Replay the remaining Spanner outbox and run reconciliation.

Parquet is the cross-version and cross-cloud recovery source. Disk snapshots
are the faster same-platform recovery source.

## Drain freshness: the out-of-band signal

Every in-band signal for the outbox drain — the metrics line, `degraded_targets=`,
and the `backlog_alarm` that is the only bound on outbox growth — is emitted by
the drain process itself. A drain that was never installed cannot alarm about
not existing. That is not hypothetical: on AWS-EU no unit had ever been
installed, 470,370 rows accumulated in `tr_operational_analytics_outbox` between
2026-08-02 and 2026-08-17, and nothing reported it. GCP was healthy throughout,
so the fleet looked healthy.

So every control plane publishes the signal itself, in its already-public
`/status.json`:

```json
"analytics": {
  "available": true,
  "backend": "postgres",
  "drain_lag_seconds": 12.5,
  "outbox_depth": null,
  "generated_at": "2026-08-17T12:00:00Z"
}
```

Those five keys are the whole contract, pinned by a test. The oldest row's own
timestamp is deliberately not among them: nothing read it, and it is
`generated_at` minus the lag in any case.

`drain_lag_seconds` is the age of the oldest **undelivered** outbox row. Rows
are deleted only after every configured ClickHouse target has accepted them, so
this is an end-to-end statement about the whole pipeline, and it is observable
from outside the VPC — which the private ClickHouse nodes are not. A read
failure publishes `{"available": false, "reason": ...}`; the key is never
omitted and a stale number is never re-served.

Cost: one index seek per status-cache miss. Postgres/DSQL uses
`tr_operational_analytics_outbox_enqueued_at_idx`; Spanner reads the head of
each of the 32 shards on the key prefix. `outbox_depth` is deliberately
optional — `count(*)` over a large backlog is the expensive question and the
lag already answers the important one.

`.github/workflows/check-analytics-freshness.yml` reads it with no credentials
for **every** cloud in
`src/trusted_router/operational_analytics_fleet.py:ANALYTICS_FRESHNESS_FLEET`.
`tests/test_analytics_freshness_registry.py` fails if that registry disagrees,
in either direction, with the union of every table in this repo that declares a
deployment (`deployment_sources()`: the BYOK attestation tables,
`regions.MULTICLOUD_REGION_GEO`, the `external_live_regions` /
`marketing_regions` settings, and the `synthetic_fleet_peers` list every cloud
already polls), so a fourth deployment cannot exist without a drain-freshness
signal or a written reason it has none — whichever of those tables it lands in
first. Each source must also be non-empty, since a union over an empty source
is satisfied by anything. Missing section, unavailable, stale, unreachable,
over-lag, a plane answering with the wrong storage backend, and a run that
measured no cloud at all are all failures — never skips.

Values read back off a remote page (`reason`, `backend`) are narrowed through
the publisher's own vocabulary before they are printed. The problems file is
pasted verbatim into a public GitHub issue, so an unnarrowed value would let
whatever answered choose text in an issue in this repository.

**It ships with `workflow_dispatch` as its only trigger, on purpose.**
Publishing the field in this repo is not the same as serving it: merging main
auto-deploys the GCP control plane only, while AWS-EU and Azure are hand-run
scripts. A cron enabled before those deploys land files an issue every morning
about clouds nobody redeployed, and a check that cries wolf is a check people
learn to ignore (the same failure the client-telemetry check's
`CANARY_COUNT_GATE_FROM` ramp-up guard exists to avoid). A `push:` trigger is
the same hazard with a shorter fuse — it fires on the merge that lands the
publisher, when no plane serves the section yet, and opens that issue an hour
after merge instead of a morning after. Deploy all three, confirm each
`/status.json` returns an `analytics` object, run the job once by
`workflow_dispatch`, then enable both triggers in the one commit the workflow
header spells out.

Two states are neither pass nor fail, and are printed as `(unchecked)` on every
run rather than skipped: a cloud with no public status page (`reason=`), and a
cloud that legitimately runs no outbox (`expects_outbox=False`). The second one
becomes a **failure** the day that cloud publishes a real lag, which is the day
it needs watching. Azure was its only user until 2026-08-18 and is not any more:
it now has two ClickHouse nodes, a drain, and a control-plane script that states
the flag, so all three clouds are measured and none is excused.

To ask about one cloud during an incident:

```bash
PYTHONPATH=src python3 -m clickhouse.check_fleet_analytics_freshness --cloud aws
```

## Alerts and capacity

Cloud Monitoring pages on node unavailability and disk use at 75 percent.
The ClickHouse server, archive and restore timers, Spanner delivery verifier,
rollup timers, and ingester must also be checked during incident response.
Alert delivery uses the verified TrustedRouter infrastructure notification
channel.

At 75 percent disk use, do not merely expand forever. Measure rows per second,
compressed bytes per row, parts, merge backlog, query latency, Keeper latency,
and archive lag. The current topology scales reads across three replicas but
has one logical write shard. Before sustained load approaches one shard's
tested limit:

1. Add time-first projections or purpose-built rollups for expensive scans.
2. Benchmark the expected row rate with the real schema and query mix.
3. Add shards with three replicas per shard and route inserts by stable event hash.
4. Keep provider/model queries distributed so a hot model cannot own one shard.
5. Validate rebalancing and restore procedures before raising production traffic.

The GCS Parquet archive separates long-term history from hot disk capacity.
It can later be queried with ClickHouse, BigQuery external tables, or DuckDB
without expanding the production cluster just to retain old rows.
