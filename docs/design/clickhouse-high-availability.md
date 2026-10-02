# ClickHouse high availability (GCP analytics cluster)

Status: **measured 2026-10-01, 18:47–19:10 UTC; plan proposed, nothing in
production changed.** The only code on this branch is a client-side endpoint
failover that does nothing until a second endpoint is configured (§4).

Scope: the GCP cluster `tr-clickhouse-1/2/3`. The AWS-EU and Azure deployments
run their own single ClickHouse nodes and were not measured here.

Related: [`../clickhouse-reliability.md`](../clickhouse-reliability.md) is the
operational runbook. Where this document and the runbook differ, this
document records what production looked like on the date above.

## Summary

**Verdict.** It is a replicated cluster, not one node with idle spares: one
shard, three `Replicated*MergeTree` replicas and a three-voter embedded
ClickHouse Keeper quorum; every table the application uses is replicated (17
replicated tables, all in sync), and the private load balancer spreads
control-plane reads evenly over all three nodes. The single point of failure
is the write side: both Spanner-outbox drains and all ten worker timers run
only on `tr-clickhouse-1`, so losing that node stops ingestion and, after ten
minutes, the public analytics snapshots, while losing node 2 or node 3 costs
only a short read blip.

| Lost | Reads | Writes (ingestion) | Data loss |
|---|---|---|---|
| `tr-clickhouse-2` or `-3` | ~15–25 s in which about a third of new queries go to the dead node and fail (TCP health check, no client retry); then normal | Pause of seconds while Keeper re-elects or the client reconnects; the drains retry | None |
| `tr-clickhouse-1` | Same blip; reads then served by 2 and 3. The public analytics snapshots are rebuilt only on node 1; after 600 s the control plane rejects them as stale (all but `client_reliability`) and serves an in-process stale copy or an empty snapshot | **Stop** until node 1, or a manually installed replacement worker host, is back. Rows wait in Spanner | None for 7 days (benchmark outbox TTL) / 30 days (operational outbox TTL); after that rows expire unread |
| Any two nodes | Served by the survivor (stale but consistent) | Stop: Keeper loses quorum, replicated tables go read-only | None within the outbox TTLs |
| All three (region) | Down | Down | Recover from 30-day disk snapshots or the Parquet archive (4 datasets) |

**Holes, in order:** single writer/worker host (G1); TCP-only health check and
single-URL read clients (G2); no alerting on replica lag, Keeper quorum or a
stopped writer (G4); system logs with no TTL holding 33–54 GB per node (G5);
two node-1 queries whose cost grows with volume, one already failing (G6);
rebuild hazards (G7); and three low-priority items (G3, G8, G9). Details and
fixes in §3.

**Recommendation:** keep the self-managed cluster and close the holes. It is
already replicated, it is cheaper at every modelled volume, and the holes are
days of work. Revisit ClickHouse Cloud only if the capacity test after the G6
fixes shows the single shard cannot carry 20× today's volume.

**The decision you need to make:** stay self-managed and close the holes
(recommended), or migrate analytics to ClickHouse Cloud (about $1.0–1.5k a month
of new cash spend at today's volume, §3.3). Everything else here is engineering
work that follows from that choice.

## 1. Measured topology

### 1.1 Method (read-only)

All production access was read-only, through `gcloud compute ssh
--tunnel-through-iap` as `tr-ops-local`, the same path the deploy scripts use.
Every query ran as `clickhouse-client --readonly=1` (`getSetting('readonly')`
returned 1 on every node), so the server would have refused any write. Queries
were `SELECT`s on `system.*` and `count()`s on `tr` tables; no `SYSTEM`,
`OPTIMIZE`, DDL or DML was sent. On the hosts, only read-only commands ran
(`df`, `du`, `free`, `ls`, `systemctl` listings, `journalctl`, and `curl` of
`/ping` and `/replicas_status`); configuration directories were listed, not
opened, and nothing was edited. GCP resources were read with `describe` and
`list`. The analytics drain lag came from the public `/status.json` rather than
from Spanner, because reading the outbox head is the expensive Spanner read the
runbook's "Drain freshness" section replaced.

### 1.2 What is running

| | tr-clickhouse-1 | tr-clickhouse-2 | tr-clickhouse-3 |
|---|---|---|---|
| Zone, IP | us-central1-a, 10.128.15.214 | us-central1-b, 10.128.0.28 | us-central1-c, 10.128.0.34 |
| Machine, disk | e2-standard-4, 500 GB pd-ssd | same | same |
| ClickHouse | 26.7.1.1315, up since 2026-08-26 | 26.7.1.1315, up since 2026-08-22 | 26.7.1.1315, up since 2026-08-22 |
| Keeper role | follower | follower | **leader**, 2 synced followers |
| Its Keeper session goes to | node 2 | node 3 | node 2 |
| Macros | shard 01, replica tr-clickhouse-1 | shard 01, replica tr-clickhouse-2 | shard 01, replica tr-clickhouse-3 |
| Worker units, `/opt/tr-clickhouse` | 12 units (2 drains + 10 timers) | none | none |

No VM has an external IP; all three have deletion protection and automatic
restart, run as `tr-clickhouse@`, and are in the daily snapshot policy (30–31
snapshots each, 559 GB of snapshot storage in total).

**Keeper.** Embedded ClickHouse Keeper on all three nodes (`keeper_server`,
`server_id` 1–3, raft port 9234, client port 9181), rendered by
`scripts/deploy/clickhouse_cluster.sh:189-230`. All three report Raft term 11
with committed log indexes advancing together (about 102.96 M), about 9,400
znodes and 3.7 MB of data, average latency 0–2 ms. There is no separate
ZooKeeper.

**Cluster.** `system.clusters` on every node lists `trustedrouter` as one shard
with three replicas (the three IPs above, port 9000, `errors_count` 0); the
rendered config sets `internal_replication` true. There are no `Distributed`
tables; the cluster definition is used for `ON CLUSTER` DDL, and reads hit each
node's local replicated tables.

**Tables.** Every table the application reads or writes is replicated. On each
node, at 18:47 UTC, all 17 replicated tables had `total_replicas` 3,
`active_replicas` 3, `queue_size` 0, `absolute_delay` 0, `is_readonly` 0, and
`system.replication_queue` had no errors.

| Engine | Tables (`tr` database) |
|---|---|
| ReplicatedReplacingMergeTree (13) | activity_generations, provider_benchmark_samples, spend_lease_shadow, synthetic_probe_samples, synthetic_status_rollups, public_analytics_snapshots, client_request_events, client_minute_counters, client_availability_rollups, workspace_directory, tenant_workspace_map, reservation_overruns, analytics_workspace_backfill |
| ReplicatedMergeTree (4) | provider_analytics_hourly, provider_analytics_daily, provider_analytics_monthly, operational_outbox_quarantine |
| View (2) | growth_billing_owners, growth_daily_usage |
| **Node 1 only, not replicated (8)** | provider_benchmark_samples_local_backup (ReplacingMergeTree, 271,490 rows), provider_analytics_{hourly,daily,monthly}_local_backup and _staging (MergeTree), ws_backfill_map (Join engine, 705,434 rows, 199 MB held in RAM) |

None of the node-1-only tables is read by the control plane (they are not in
the reader grants of `scripts/deploy/clickhouse_control_reader.sh:47-59`). The
`_staging` tables are node-local by design: `clickhouse/rollup_analytics.py:242-263`
rebuilds a partition in staging and publishes it with `REPLACE PARTITION`,
which replicates. `analytics_workspace_backfill` (replicated) and
`ws_backfill_map` have no DDL in this repository.

**Replication is live, not configured-but-idle.** Node 1 has executed 3,545,941
`INSERT`s since 2026-08-26 and has never fetched a part. Node 3 has fetched
2,940,980 parts since 2026-08-22 (21 fetches failed; none is outstanding).
Nodes 2 and 3 executed no `INSERT` in the last 7 days. Reads in the last 24 h,
by node:

| User (caller) | tr-clickhouse-1 | tr-clickhouse-2 | tr-clickhouse-3 |
|---|---:|---:|---:|
| `tr_control_read` (control plane, via the load balancer) | 8,469 queries, p50 43 ms, p99 5.0 s | 8,343, p50 33 ms, p99 5.1 s | 8,550, p50 33 ms, p99 5.1 s |
| `tr_growth_read` | 178 | 188 | 206 |
| `tr` (node-1 workers) | 12,006 queries reading 859 GB | — | — |

### 1.3 Who writes and who reads

**Writes go to node 1 only.** The control plane never writes to ClickHouse:
`TR_OPERATIONAL_ANALYTICS_SINK=outbox` (`scripts/deploy/rollout.sh:536`) and
`TR_ANALYTICS_OUTBOX_ENABLED=true` put rows into Spanner outboxes. Two drains on
node 1 move them into node 1's local server:

- `clickhouse/ingest_outbox.py:194-223,347`: `tr_analytics_outbox` →
  `provider_benchmark_samples`, `clickhouse-client` with no `--host`.
- `clickhouse/ingest_operational_outbox.py:996` constructs
  `ClickHouseOperationalWriter(password=...)`; host defaults to empty
  (`:495-516`) and is omitted from argv (`:541-548`), so it is localhost.
  `tr_operational_analytics_outbox` → activity, synthetic, client-event and
  spend-lease tables.

The ten timers on node 1 also talk to localhost:
`clickhouse/rollup_reservation_overruns.py:27` and
`clickhouse/refresh_workspace_directory.py:41` (`http://localhost:8123/`), and
`/usr/bin/clickhouse-client` without a host in
`clickhouse/build_public_snapshots.py:51`, `rollup_analytics.py:78`,
`rollup_synthetic.py:40`, `rollup_client_events.py:36`, `archive_daily.py:328`
and `local_clickhouse.py:25` (used by `verify_spanner_delivery.py`). They are
installed only on node 1: `scripts/deploy/clickhouse_live_ingestion.sh:15`
defaults `NAME=tr-clickhouse-1`, and `clickhouse_operational_analytics.sh:102-183`
installs and enables with `node_ssh 0`.

Writing to one replica is enough for durability once it replicates: a part
inserted on node 1 is fetched by 2 and 3. The problem is that nothing else can
write when node 1 is gone.

**Reads go to all three nodes through the private load balancer.**
`scripts/deploy/rollout.sh:339-347` resolves the `tr-clickhouse-ilb` address
(10.128.0.96) and sets both `TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL` and
`TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL` (`:516`, `:520`) to
`http://10.128.0.96:8123`. If that lookup fails it silently falls back to node 1
(`:346`). The readers are `OperationalAnalyticsClient`
(`src/trusted_router/storage_gcp.py:433-441`,
`routes/console/activity.py:95-104`) and `ProviderAnalyticsClient`
(`routes/provider_portal.py:75-87`). Before this branch, both took one URL,
used one timeout for every phase including connect (20 s; 2 s for public
snapshots), and never retried.

The load balancer (`scripts/deploy/clickhouse_cluster.sh:421-500`) is an internal
passthrough TCP forwarding rule with global access over three unmanaged
instance groups, `sessionAffinity: NONE`, `CONNECTION` balancing. Its health
check is **TCP on port 8123, every 10 s, unhealthy after 2 failures**
(`:442-450`). At measurement time all three backends were HEALTHY.

## 2. Measured capacity

### 2.1 Tables (one replica; the other two match to within in-flight rows)

Bytes are compressed on-disk bytes from `system.parts` (active parts) on
tr-clickhouse-3. Growth is from per-day `count()`s and node 1's
`system.asynchronous_insert_log`.

| Table | Rows | On disk | Bytes/row | TTL | Growth |
|---|---:|---:|---:|---|---|
| activity_generations | 12,904,858 | 1.62 GB | 125 (133.5 for September) | 400 d | 542,321 rows on 2026-09-30; 823,244 in the 24 h to 2026-10-01 18:50 |
| provider_benchmark_samples | 8,803,602 | 455 MB | 51.7 | 400 d | 520,275 on 2026-09-30; 801,050 in 24 h |
| spend_lease_shadow | 8,683,097 | 509 MB | 58.7 | 30 d | none since 2026-09-28; TTL empties it by about 2026-10-28 |
| synthetic_status_rollups | 373,871 | 115 MB | 308 | 24 mo | ~7,000 rows/day (rebuilt every 5 min) |
| analytics_workspace_backfill | 3,399,443 | 107 MB | 31 | none | static |
| client_minute_counters | 1,038,549 | 89 MB | 85 | 180 d | 27,673 in 24 h |
| client_request_events | 575,349 | 89 MB | 154 | 90 d | 15,003 in 24 h |
| synthetic_probe_samples | 530,874 | 30 MB | 56 | 14 d | ~39,000/day, at TTL steady state |
| public_analytics_snapshots | 45 | 22 MB | ~478 KB | 7 d | ~5.7 rows/min replaced; steady size, but 30 GB/day of insert bytes |
| client_availability_rollups | 253,784 | 16 MB | 61 | 24 mo | rebuilt every 5 min |
| provider_analytics_hourly / daily / monthly | 284,591 / 106,564 / 16,097 | 11 / 4.2 / 0.7 MB | ~40 | 3 y / none / none | ~3,000 hourly rows/day |
| workspace_directory, tenant_workspace_map, reservation_overruns, operational_outbox_quarantine | 3,176 / 3,175 / 1,206 / 0 | < 1 MB each | | | |
| **All `tr` tables** | **36.98 M** | **3.06 GB** | | | **≈181 B per generation** (133.5 activity + 0.93 × 51.3 benchmark) **+ ≈7 MB/day** of fixed-rate tables |

At the stated ~580k generations a day, `tr` data grows by about **112 MB/day per
replica** (105 MB scaling with traffic, 7 MB fixed). Nothing has reached its
400-day TTL yet, so nothing expires within any horizon below.

Daily activity is bursty: quiet hours run at 1.2–2k rows (about 40–50k a day)
and heavy days reach 1.24M (2026-09-22). The low count for 2026-09-29 (49,963)
is a quiet day, not an ingestion gap: hourly counts were steady at 1.1–4.7k all
day.

**System logs are most of the disk.** The `system` database holds 53.5 GB on
node 1 and 34.4 / 33.0 GB on nodes 2 / 3, against 3.06 GB of application data.
Nine of the twelve log tables have no TTL (`text_log`, `trace_log`,
`query_log`, `part_log`, `metric_log`, `asynchronous_metric_log`, `error_log`,
`background_schedule_pool_log`, `query_metric_log`); `text_log` alone is
27.3 GB on node 1. In September the TTL-less logs grew by 24.0 GB on node 1
(0.80 GB/day) and 12.1–12.8 GB on nodes 2 and 3 (0.40–0.43 GB/day). On node 1
the rate was 24.9 GB in August and 24.0 GB in September, while activity rows
went from 1.0 M to 8.1 M a month.

### 2.2 Disk per node

| Node | Provisioned | Filesystem | Used (`df`) | Available | Use% | `tr` | `system` |
|---|---:|---:|---:|---:|---:|---:|---:|
| tr-clickhouse-1 | 500 GB pd-ssd | 528.2 GB | 70.1 GB | 436.5 GB | 14% | 3.09 GB | 53.5 GB |
| tr-clickhouse-2 | 500 GB pd-ssd | 528.2 GB | 45.0 GB | 461.6 GB | 9% | 3.06 GB | 34.4 GB |
| tr-clickhouse-3 | 500 GB pd-ssd | 528.2 GB | 43.3 GB | 463.3 GB | 9% | 3.06 GB | 33.0 GB |

Node 1 also holds 3.1 GB of journald logs, 1.9 GB of ClickHouse server logs and
the node-local tables. Snapshot storage is 235 / 165 / 159 GB for 30 days of
daily incremental snapshots, about 3.5 times the data on the disks.

### 2.3 Ingest, reads, CPU and memory

- **Ingest (24 h):** 1.71 M event rows (823,244 activity, 801,050 benchmark,
  47,016 synthetic probes, 27,673 client minute counters, 15,003 client request
  events), about 20 rows/s, in 71,235 async-insert flushes (~24 rows each),
  plus rollup rewrites. Peak minute in 7 days: 104,351 rows (the daily rollup
  at 03:43 UTC). Drain lag in the logs: 1–5 s now, 30.1 s worst in 7 days;
  `/status.json` reported `drain_lag_seconds` 5.705 at 18:52 UTC.
- **Settings that matter:** `insert_quorum` 0 and `async_insert` 1 with
  `wait_for_async_insert` 1 (26.7 defaults): an `INSERT` returns once node 1
  has written the part, before any other replica has it. `parts_to_delay_insert`
  300 / `parts_to_throw_insert` 600; the largest partition has 14 parts.
- **CPU (7 days, 4 vCPU each):** node 1 averages 24% busy (p99 of user time
  89%, load average up to 13.8); nodes 2 and 3 average about 5%. Node 1's
  average rose from 14.5% (2026-08-01..08) to 24% (late September) while daily
  activity rows rose about 12× and node-1 worker reads rose 2.7× (≈320 →
  ≈860 GB/day). Of today's 859 GB/day of worker reads, about 700 GB is in
  queries whose cost grows with data volume; the largest are a full-table
  `FINAL` scan of `activity_generations` by `generation_id` every 30 minutes
  (`verify_spanner_delivery`, 259 GB/day, `clickhouse/operational_fingerprint.py:101-106`)
  and the leaderboard snapshot queries.
- **Memory:** 16.8 GB per node; ClickHouse resident memory averages 4.1–5.5 GB
  and peaked at 7.0 GB in 7 days; `max_server_memory_usage` is 11.6 GB. The control-plane read profile caps a
  query at 1 GiB; the heaviest control-plane read in 24 h used 102 MiB.
- **Already at a limit:** the leaderboard-evidence query in
  `clickhouse/build_public_snapshots.py:125-160` runs every minute under a
  256 MiB cap (`:156`). It has failed with `MEMORY_LIMIT_EXCEEDED` 11,042 times
  since 2026-09-21, including 867 of 1,120 runs today after the #1432 rewrite.
  The builder keeps the last good `leaderboard_evidence` snapshot when it fails,
  so that snapshot is refreshed only on the minority of successful runs.
- **Keeper:** ~9,400 znodes, 3.7 MB, average latency 0–2 ms, worst 5 s since
  start (node 2), no rejected connections. Not a near-term limit.

### 2.4 When each limit is hit

Model: generations per day = 580,000 × g^(days/30) with g = 3 or 10; each
generation adds 181 B of `tr` data per replica; fixed-rate tables add 7 MB/day;
TTL-less system logs keep growing at their measured September rate. Start:
2026-10-01. Node 1 is the binding node.

| Limit | Today | 3×/month | 10×/month | Basis |
|---|---|---|---|---|
| Node-1 disk at the 75% alert (380 GB used) | 70.1 GB | **2027-01-27** (44 M generations/day by then) | **2026-12-08** (109 M/day) | measured bytes/generation and log growth |
| Node-1 disk full (506.6 GB, writes fail) | | 2027-02-08 | 2026-12-13 | same |
| Nodes 2 and 3, 75% | 45.0 / 43.3 GB | 2027-02-03 / 02-04 | 2026-12-10 / 12-11 | same |
| Node-1 CPU averaging 70% (from 24%) | 24% | between 2026-11-12 and 2027-02-25 | between 2026-10-21 and 2026-12-10 | earlier date: volume-dependent scans grow with traffic; later date: the last two months' measured relationship (CPU ∝ traffic^0.2) |
| Benchmark drain stops sleeping between batches (5,000 rows per ~5.3 s cycle: 5 s sleep plus the measured ~0.3 s pass ≈ 81.5 M rows/day) | 0.8 M/day | 2027-02-13 | 2026-12-04 | code constants (`ingest_outbox.py:317-318`); throughput beyond this point is unmeasured |
| Operational drain, same point (5,000 rows per ~2.15 s cycle ≈ 201 M rows/day) | | 2027-03-10 | 2026-12-16 | code constants |
| Control-plane 1 GiB per-query cap, if its heaviest read grows with volume | 102 MiB | about 2026-12-03 | about 2026-10-31 | assumes memory proportional to rows in the window; unverified |
| Leaderboard-evidence 256 MiB cap | exceeded | hit 2026-09-21 | hit 2026-09-21 | measured |
| Writer outage tolerated before outbox rows expire | 7 d (benchmark), 30 d (operational) | same | same | Spanner `ROW DELETION POLICY` (`migrate_analytics_outbox.sh:53`, `migrate_operational_analytics_outbox.sh:31`) |

Two consequences. First, with no traffic growth node 1 would reach 75% on
2027-09-06, mostly from system logs; under 3×/month growth `tr` data
overtakes the system logs as the faster-growing consumer on about 2026-11-25
(about 2026-10-27 at 10×), so the log TTL (G5) moves the disk dates by only
about 12 days (3×) or 4 days (10×). Second, the earliest limits are on node 1's
CPU and in individual queries, not disk; both are made worse by node 1 doing all
worker work.

### 2.5 Not measured, and why

- **Live alert policies.** `tr-ops-local` lacks `monitoring.alertPolicies.list`;
  only the templates in `scripts/deploy/clickhouse-alerts/` were read.
- **Parquet archive size and cost.** `tr-ops-local` cannot list the archive
  bucket, by design.
- **Drain throughput above the knee, and query latency or CPU at 3–100×
  volume.** These need a load test. `scripts/deploy/clickhouse_capacity_smoke.sh
  --apply` writes to production, so it was not run.
- **Outbox depth in Spanner.** Not read: it is the expensive scan the
  heartbeat row exists to avoid, and `/status.json` reports the lag instead.
- **A Terraform plan.** `tr-ops-local` cannot read the state bucket; the HCL in
  §3.2 was checked with `terraform validate` only.
- **The Ops Agent's exact `percent_used` definition.** The 75% threshold above
  uses `df`'s `Use%` (used ÷ (used + available), excluding the 5% root
  reserve), which is the earlier of the two possible readings.
- **ClickHouse Cloud Scale-tier price on GCP.** The pricing page fetched on
  2026-10-01 publishes only Enterprise-plan rates for GCP regions.

## 3. Gaps and fixes

The cluster does not need converting: replication, Keeper, ON CLUSTER DDL and
the load balancer exist (`clickhouse_cluster.sh` built them in July–August
2026). What remains is closing these holes.

### 3.1 The holes

**G1. One host does all writing and all worker jobs (high).**
Evidence: §1.2–1.3. Effect: §Summary. The outbox drains tolerate a second
copy: rows are deduplicated by `ReplacingMergeTree` and read with `FINAL`,
outbox deletes are by key, and archive objects are write-once by precondition
(the diagnostic `operational_outbox_quarantine` table, plain
`ReplicatedMergeTree`, could record a poison row twice). The publishers do
not: a rollup or snapshot job that replaces a whole partition can finish after
a newer one and overwrite fresher results with older ones, so two hosts must
never publish at once. That is why takeover below is fenced and manual.

1. Now: install the same units on tr-clickhouse-2, **disabled**, with a
   runbook step to `systemctl enable --now` them, and the node-local
   `_staging` tables there. `clickhouse_live_ingestion.sh` already takes
   `NAME`/`ZONE`; `clickhouse_operational_analytics.sh` needs a worker-index
   parameter in place of `node_ssh 0` at `:102-183` and `:239-240`.
   Recovery then takes minutes once someone is paged, well inside the 7-day
   outbox TTL.
2. Not now: unattended takeover. A lease row cannot fence a publication that
   is already inside ClickHouse. An old holder can pass any lease or epoch
   check, submit `REPLACE PARTITION`, and stall in the server; killing the
   worker process does not stop the server-side statement, cancellation is not
   guaranteed for every query stage, and the statement can complete after a
   standby has published fresher results, replacing them with older ones.
   `RuntimeMaxSec=` cannot bound these jobs either: they are `Type=oneshot`,
   for which systemd ignores it. Automatic takeover therefore needs a
   publication-side fence (for example writing each publication into a
   staging partition named by an epoch that a single atomic swap promotes,
   with the swap refusing stale epochs) and an authoritative lease clock.
   Design that before building it. Until then takeover stays manual (item 1)
   with an explicit fence, in order:
   1. Stop the old publisher: stop node 1's worker units and timers, or stop
      the VM if node 1 is unreachable.
   2. Drain its writes from the server side: on every replica,
      `KILL QUERY WHERE user = 'tr' SYNC` for queries from the worker user, then
      confirm `system.processes` and `system.mutations` (`is_done = 0`) show
      none from it, and that `system.replication_queue` has no pending
      `REPLACE_RANGE` from node 1.
   3. Only then enable the standby units on node 2 (`systemctl enable --now`).
   Recovery takes minutes once someone is paged, well inside the 7-day outbox
   TTL.
3. Drill: `clickhouse_failover_smoke.sh:16` defaults to node 3 and checks reads
   only. Add a node-1 mode that requires the standby to drain (drain lag on
   `/status.json` recovers) while node 1 is stopped.

**G2. Reads: a TCP-only health check and clients with one URL and no retry (high).**
A TCP check passes whenever port 8123 accepts connections, so a replica that
is up but minutes behind keeps serving stale reads, and a dead one keeps
receiving about a third of new connections for ~15–25 s. Each query opens a new
connection with a 20 s connect timeout (2 s for public snapshots), so those
reads hang and then fail.

1. Health check: an HTTP check on 8123, every 5 s, unhealthy after 2.
   `GET /replicas_status` alone is not enough. It answers 503 when a writable
   replica lags its freshest peer by `min_relative_delay_to_close` (300 s, the
   default; verified on the cluster), but it skips read-only replicas
   ([`ReplicasStatusHandler.cpp`](https://raw.githubusercontent.com/ClickHouse/ClickHouse/master/src/Server/ReplicasStatusHandler.cpp)).
   A minority replica that loses Keeper while node 1 and the other voter keep
   quorum turns read-only and falls further behind while ingestion continues,
   yet still answers 200, so it keeps receiving reads and successful responses
   never trigger client failover. Use a predefined HTTP handler instead
   (`http_handlers` with a `predefined_query_handler` at, for example,
   `/tr_health`) whose query fails when this replica is read-only, too far
   behind, or cannot see the full replicated inventory:
   `SELECT throwIf(countIf(is_readonly OR absolute_delay > 300) > 0 OR count() < 17)
   FROM system.replicas WHERE database = 'tr'` (17 = the replicated tables
   today; keep that number in the same change that adds or drops a replicated
   table). ClickHouse returns an error status for a thrown exception, so the
   check marks the replica unhealthy. The freshest writable replicas still
   pass, so lag alone cannot empty the backend set. The handler runs as a
   dedicated read-only user granted `SELECT ON system.replicas` and
   `SHOW TABLES ON tr.*`: `system.replicas` filters rows by the user's table
   visibility, so without the second grant an unhealthy replica's rows are
   invisible and the check answers 200 (reproduced on ClickHouse 26.9 during
   review). The `count() < 17` guard catches that and any other incomplete
   inventory. The firewall already allows the health-check ranges to 8123.
   Terraform in §3.2 points the check at this path.
2. Client failover: committed on this branch (§4); turn it on by setting the
   two URLs to the load balancer followed by the three replicas.
3. `scripts/deploy/rollout.sh:341-347`: fail the rollout when the
   load-balancer address cannot be resolved, instead of pinning every reader
   to node 1.

**G3. An `INSERT` is acknowledged before a second replica has it (low).**
With `insert_quorum` 0 the drains delete outbox rows as soon as node 1 has
written a part. If node 1's disk were lost within the replication delay (0 s
observed), those rows would be gone. Fix: the drains pass
`--insert_quorum=2 --async_insert=0` to `clickhouse-client`
(`clickhouse/ingest_outbox.py:214-223`,
`clickhouse/ingest_operational_outbox.py:541-556`). Try it on a scratch
replicated table first.

**G4. Nothing alerts on replica health, Keeper quorum or a stopped writer (medium).**
`scripts/deploy/clickhouse-alerts/node-availability.yaml` alerts on missing VM
uptime only; a crashed `clickhouse-server`, a read-only replica or a dead drain
on a running VM does not page. `/status.json` shows `poller_stale` within
180 s, but `check-analytics-freshness.yml` reads it only every six hours. Fix:
enable ClickHouse's Prometheus endpoint (`<prometheus>` on port 9363, kept to a
handful of metrics), scrape it with the Ops Agent's `prometheus` receiver, and
alert on `ReplicasMaxAbsoluteDelay` > 300 s for 10 min, `ReadonlyReplica` > 0,
`KeeperIsLeader` summed across nodes ≠ 1, and no `InsertQuery` growth on the
writer (later, the lease holder) for 10 min. The policies belong in Terraform
next to the health check.

**G5. System logs have no TTL (medium).**
33–54 GB per node today, growing 0.4–0.8 GB/day per node. Fix:
`config.d/tr-system-logs.xml` with
`<ttl>event_date + INTERVAL 30 DAY DELETE</ttl>` for the nine TTL-less logs,
7 days and level `information` for `text_log`. Roll it one node at a time. On
restart ClickHouse renames a log table whose definition changed to `<name>_0`;
drop those after checking. This frees roughly 26–36 GB on node 1 (depending on
whether `text_log` keeps 30 or 7 days) and removes the fixed growth.

**G6. Two node-1 queries grow with volume; one already fails (medium, capacity).**
- `verify_spanner_delivery` looks rows up by `generation_id`, which is not a
  prefix of the sort key `(tenant_id, created_at, generation_id)`, so each
  half-hourly run is a full `FINAL` scan (5.4 GB now).
  Bound it by the source rows' `created_at` range or add a `bloom_filter` skip
  index on `generation_id` (`clickhouse/operational_fingerprint.py:101-106`).
- Leaderboard evidence (§2.3): precompute the per-route ranks in the hourly
  rollup, or raise the cap from 256 MiB to 1 GiB (node 1 has 9–12 GB
  available).

**G7. Rebuilding a node can silently break replication (medium).**
- `clickhouse_live_ingestion.sh:90-93` applies `001_…` and `002_…`, which create
  **non-replicated** `provider_benchmark_samples` and rollup tables when absent.
  On a freshly rebuilt node, run before the cluster bootstrap, the drain would
  write to a table that never replicates. Apply the replicated DDL (003, 005)
  or `ON CLUSTER` instead, and refuse when the canonical engine is not
  `Replicated*`.
- `clickhouse_startup.sh:35` installs whatever the `stable` channel has. A
  replacement node today would not be 26.7.1.1315. Pin the version.
- `tr_ops_ingest` exists only on node 1 (`clickhouse_operational_writer.sh:16`
  targets one node). Re-enabling the retired direct sink through the load
  balancer would fail authentication on two of three connections. Loop over
  all nodes first.
- `analytics_workspace_backfill` and `ws_backfill_map` exist only in
  production. Drop them or add their DDL.

**G8. The archive covers four datasets (low).** `clickhouse/archive_daily.py:90-164`
archives provider benchmarks, activity, synthetic probes and synthetic rollups.
Client telemetry (`client_request_events` 90 d, `client_minute_counters` 180 d,
`client_availability_rollups` 24 mo) is protected only by replication and
snapshots, and cannot be rebuilt from Spanner. Add them if that history
matters.

**G9. Node-1 leftovers (low).** The `_local_backup` tables, the 199 MB in-RAM
`ws_backfill_map`, and a second `tr_provider_read` definition in
`local_directory` (shadowed by `users_xml`) make node 1 differ from its
replicas. Remove them in a reviewed cleanup.

**Accepted risk: one region.** All three zones are in us-central1. Losing the
region stops analytics, never inference or billing. Recovery is from the
multi-region snapshots or the US multi-region Parquet archive. Cross-region
replicas are not proposed.

### 3.2 Terraform for the load balancer health check

Not committed under `infra/`: `.github/workflows/infra-apply.yml` applies the
whole root on any merge that touches `infra/**`, with no plan gate, and this
environment cannot run `terraform plan` against production state. The HCL
below passes `terraform fmt -check` and `terraform validate`. Land it as two
pull requests:

1. Adopt the existing check and backend service. The plan must say
   **No changes**.
2. Add the HTTP check and switch the backend service to it.

```hcl
locals {
  clickhouse_region = "us-central1"
  clickhouse_nodes = {
    "tr-clickhouse-1" = "us-central1-a"
    "tr-clickhouse-2" = "us-central1-b"
    "tr-clickhouse-3" = "us-central1-c"
  }
}

import {
  to = google_compute_region_health_check.clickhouse_tcp
  id = "projects/quill-cloud-proxy/regions/us-central1/healthChecks/tr-clickhouse-http"
}

import {
  to = google_compute_region_backend_service.clickhouse_http
  id = "projects/quill-cloud-proxy/regions/us-central1/backendServices/tr-clickhouse-http"
}

resource "google_compute_region_health_check" "clickhouse_tcp" {
  name                = "tr-clickhouse-http"
  region              = local.clickhouse_region
  check_interval_sec  = 10
  timeout_sec         = 5
  healthy_threshold   = 2
  unhealthy_threshold = 2

  tcp_health_check {
    port = 8123
  }
}

# Second pull request.
resource "google_compute_region_health_check" "clickhouse_replica_health" {
  name                = "tr-clickhouse-replicas-status"
  region              = local.clickhouse_region
  description         = "ClickHouse /tr_health: error when this replica is read-only or more than 300 s behind"
  check_interval_sec  = 5
  timeout_sec         = 3
  healthy_threshold   = 2
  unhealthy_threshold = 2

  http_health_check {
    port         = 8123
    request_path = "/tr_health"
  }
}

resource "google_compute_region_backend_service" "clickhouse_http" {
  name                            = "tr-clickhouse-http"
  region                          = local.clickhouse_region
  load_balancing_scheme           = "INTERNAL"
  protocol                        = "TCP"
  session_affinity                = "NONE"
  connection_draining_timeout_sec = 0
  # First pull request: google_compute_region_health_check.clickhouse_tcp.id
  health_checks = [google_compute_region_health_check.clickhouse_replica_health.id]

  dynamic "backend" {
    for_each = local.clickhouse_nodes
    content {
      group          = "projects/quill-cloud-proxy/zones/${backend.value}/instanceGroups/${backend.key}"
      balancing_mode = "CONNECTION"
    }
  }
}
```

The VMs, disks, snapshot policy, firewall rules and instance groups should be
adopted the same way, one reviewed import at a time. Importing a VM with a
wrong boot-disk or metadata attribute can plan a replacement, so each import's
plan must be read line by line.

### 3.3 Self-managed versus ClickHouse Cloud

List prices, us-central1, monthly, before credits or commitments.
Self-managed: e2-standard-4 $97.83 a month, pd-ssd $0.17 per GB-month,
snapshots about $0.05 per GB-month. ClickHouse Cloud, GCP us-central1
Enterprise-plan rates published on 2026-10-01: $0.32727 per compute unit-hour
(one unit is 8 GiB RAM and 2 vCPU, $238.91 a month), $22 per TB-month of
storage, backups at the storage rate. Per vCPU that is $119.45 a month against
$24.46 for e2.

Retained data is the steady state at the current 400-day TTL. Compute is the
range from §2.4, low (CPU ∝ traffic^0.2) to high (volume-dependent scans grow
linearly). Neither has been load-tested.

| Volume (generations/day) | Retained `tr` data | Self-managed | ClickHouse Cloud |
|---|---:|---|---|
| 1× (580k), today | 3.1 GB | **$595–603**: 3 × e2-standard-4, 3 × 500 GB pd-ssd, 559 GB of snapshots, load-balancer rule | **$956** (2 replicas × 16 GiB) to **$1,433** (3 × 16 GiB), storage under $1, plus a small VM for the drains and timers |
| 20× (11.6 M) | 0.86 TB | low $1,058 (today's 12 vCPU, 3 × 1.5 TB pd-ssd); high $1,939 (3 × e2-standard-16) | low $1,471 (12 vCPU); high $5,772 (48 vCPU) |
| 100× (58 M) | 4.2 TB | low $1,055–3,353 (12 vCPU; tiered GCS or all-SSD); high ≈ $8,060 (3 shards × 3 replicas of e2-standard-32, tiered storage) | low $1,619 (12 vCPU); high ≈ $34,600 (288 vCPU) |

Not included: engineering time; private networking and egress for ClickHouse
Cloud; inter-zone replication traffic; the archive bucket; snapshot growth at
20× and 100×. If GCP spend is paid from credits, the self-managed cash cost is
lower still, while ClickHouse Cloud is billed separately unless bought through
a channel those credits cover (not checked).

**Recommendation: stay self-managed and close G1–G7.**

1. Replication already works. The holes are in workers, health checks and
   clients, and each fix is small. A migration would take weeks (drains,
   timers, network path, users and grants, archive, snapshot pipeline) and
   leave the same client and worker work to redo against a new endpoint.
2. Self-managed is cheaper at every modelled volume, because compute dominates
   and costs about a fifth as much per vCPU. ClickHouse Cloud is cheaper only
   for storage. At 100× the self-managed answer is tiered storage (parts older
   than about 30 days on GCS), not more SSD.
3. The point to revisit is sharding. If, after the G6 fixes,
   `clickhouse_capacity_smoke.sh` at 20× the current row rate shows one shard
   needs more than about 48 vCPU, self-managed operations get much harder
   (sharding, rebalancing, distributed queries). ClickHouse Cloud's single
   logical table on object storage is then worth its compute premium.

**Decision for the human:** stay self-managed and close the holes
(recommended), or migrate to ClickHouse Cloud. If self-managed, the next work
is G1 step 1, G2 and G5, which change only node configuration and the load
balancer, not the application.

## 4. Preparation committed on this branch

**Read-client endpoint failover, off until configured.**

- `src/trusted_router/clickhouse_endpoints.py` (new). A ClickHouse URL setting
  may be one URL or an ordered, comma-separated list. With one URL, which is
  every deployment today, a client makes exactly one attempt with the caller's
  timeout for every phase and raises every error, as before. With several, a
  read moves to the next endpoint only when the connection could not be opened
  (`ConnectError`, `ConnectTimeout`) or the endpoint answered 502/503/504. A
  read timeout, a 500 or any 4xx is raised without retry: those fail the same
  way on every replica, or would run an expensive query twice. With a fallback
  configured, connect is bounded at 1 s so a dead endpoint costs about a
  second, not the 20 s query budget.
- `operational_analytics.py` (`OperationalAnalyticsClient._query`) and
  `provider_analytics.py` (`ProviderAnalyticsClient`, JSON queries and the
  streamed CSV export, which fails over only before its body starts) use it.
- `config.py`: `operational_analytics_sink_problems` rejects an endpoint list
  when `TR_OPERATIONAL_ANALYTICS_SINK=direct`, because the direct sink posts to
  one URL.
- `tests/test_clickhouse_endpoints.py`: 30 tests. They pin the single-URL
  behaviour (one attempt, 20 s for every timeout phase, errors raised), each
  failover trigger, each non-trigger (read timeout, protocol error, 400, 401,
  403, 404, 500), ordering, raising the last error, the streamed export and the
  direct-sink guard. Four mutations of the module were run against them and
  each was caught: no status-code failover (5 failures), a read timeout treated
  as a connect failure (1), no connect bound (2), and the connect bound applied
  to a single endpoint too (2).

To enable it, a separate reviewed change to `scripts/deploy/rollout.sh:339-347`
(not on this branch) sets both URLs to the load balancer followed by the three
replicas, discovered rather than hard-coded:

```bash
clickhouse_node_urls="$(gc compute instances list \
  --filter='name~^tr-clickhouse-[0-9]+$' \
  --format='value(networkInterfaces[0].networkIP)' \
  | sort | sed 's|^|http://|; s|$|:8123|' | paste -sd, -)"
PROVIDER_ANALYTICS_CLICKHOUSE_URL="http://${clickhouse_ilb_ip}:8123,${clickhouse_node_urls}"
```

`tr_control_read` and `tr_provider_read` already exist on all three nodes, and
the firewall already admits the VPC ranges, so no node change is needed.

**Remaining code changes, by hole:**

| Hole | Change |
|---|---|
| G1 | Worker host as a parameter in `scripts/deploy/clickhouse_operational_analytics.sh:102-183,239-240`; standby units and `_staging` tables installed disabled on node 2; a runbook with the three fence steps (stop node 1's publishers, server-side `KILL QUERY` and verification of `system.processes` / `system.mutations` / `system.replication_queue`, then enable node 2). Node-1 mode in `scripts/deploy/clickhouse_failover_smoke.sh:16`. Unattended takeover waits for a publication-side fence design (item 2). |
| G2 | `scripts/deploy/rollout.sh:339-347`: endpoint list, and fail instead of falling back to `10.128.15.214`. The Terraform in §3.2. |
| G3 | `--insert_quorum=2 --async_insert=0` in `clickhouse/ingest_outbox.py:214-223` and `clickhouse/ingest_operational_outbox.py:541-556`. |
| G4 | `<prometheus>` block added by `scripts/deploy/clickhouse_cluster.sh:189-230`; Ops Agent receiver installed at `:277-291`; alert policies in Terraform. |
| G5 | New `config.d/tr-system-logs.xml` written by `scripts/deploy/clickhouse_cluster.sh:232-250`, restarted one node at a time. |
| G6 | `clickhouse/operational_fingerprint.py:101-106` (time-bounded lookup); `clickhouse/build_public_snapshots.py:125-160` (rank in the rollup, or a 1 GiB cap). |
| G7 | `scripts/deploy/clickhouse_live_ingestion.sh:90-93` (replicated DDL, refuse non-replicated engines); `scripts/deploy/clickhouse_startup.sh:35` (pin the version); `scripts/deploy/clickhouse_operational_writer.sh:16` (all nodes). |
| G8 | `clickhouse/archive_daily.py:90-164` (add the client telemetry datasets). |
