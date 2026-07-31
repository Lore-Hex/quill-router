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
| Replicated raw ClickHouse rows | 400 days | Recent investigation and exact re-aggregation |
| Hourly ClickHouse rollups | 3 years | Detailed trends and provider operations |
| Daily and monthly ClickHouse rollups | No TTL | Long-term product and provider analytics |
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

`clickhouse/archive_daily.py` exports each closed UTC day from the canonical
raw table with `FINAL`. It computes a full row-set fingerprint, writes Parquet,
reads the Parquet back with `clickhouse-local`, and compares row count, hash
sum, and hash XOR before publishing a manifest.

Objects are immutable and revisioned:

```text
gs://quill-cloud-proxy-tr-clickhouse-archive/
  raw/provider_benchmark_samples/day=YYYY-MM-DD/
    _latest.json
    revisions/<fingerprint>/manifest.json
    revisions/<fingerprint>/part-*.parquet
```

A rerun with unchanged data is a no-op. Late reconciled rows produce a new
immutable revision and atomically advance `_latest.json`. Bucket versioning,
uniform access, and public-access prevention are enabled. The daily service
rechecks seven closed days so late rows are captured.

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
```

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

## Alerts and capacity

Cloud Monitoring pages on node unavailability and disk use at 75 percent.
The ClickHouse server, archive timer, rollup timers, and ingester must also be
checked during incident response. Alert delivery uses the verified
TrustedRouter infrastructure notification channel.

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
