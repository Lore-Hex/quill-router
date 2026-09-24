# Spanner CPU alert from the hourly reservation-overrun rollup

## Evidence

All timestamps are UTC on 2026-09-24, the first full day with regional quota
lease issuance enabled for the pilot cohort.

- The `TR Spanner: high-priority CPU` policy (`utilization_by_priority`,
  `priority=high`, five-minute maximum, threshold 45%) breached twice: 52.8%
  in the bucket ending 06:05 and 46.2% in the bucket ending 19:05.
- Replaying the policy filter per series with a one-minute maximum shows 21
  points above 30% in 30 hours, all in the user (not system) high-priority
  series, clustered at two to five minutes past the hour. The one-minute mean
  of the same series never exceeded 3.4%: the spikes last about ten seconds.
- `SPANNER_SYS.QUERY_STATS_TOP_10MINUTE` for the buckets ending 06:10 and
  19:10 each contain one execution of the rollup's read:
  `SELECT terminal_at, hold_usage_type, actual_micro, credit_reserved_micro,
  settled FROM tr_reservation WHERE settled = true AND terminal_at >=
  @window_start AND terminal_at < @window_end`. It scanned 6,751,551 and
  6,872,333 rows to return 19,402 and 16,330, costing 7.8 and 8.1
  CPU-seconds and 11.9 and 11.6 seconds of latency. Every other statement in
  those buckets cost under 0.01 CPU-seconds per execution.
- `tr_reservation` had indexes on `authorization_id`, `(settled, expires_at)`
  and `idempotency_scope`, none of which covers a `terminal_at` range.
- The `tr-clickhouse-overrun-rollup.timer` unit on `tr-clickhouse-1` runs
  `clickhouse.rollup_reservation_overruns` hourly with a five-minute random
  delay, which matches the breach minutes.
- The same read is the 11-14 second outlier behind the `TR Spanner: request
  latency` p99 spikes (13.7 s in the bucket ending 15:58).
- Lease traffic is not the cause: during the 19:00 burst (about nine requests
  per second from one workspace) the user high-priority series averaged 2-3%
  of a 400 processing-unit instance, and the lock-wait, aborted-commit and
  commit-latency policies stayed quiet all day.

## Change

- `scripts/deploy/migrate_typed_counters.sh` creates the covering
  `NULL_FILTERED` index `tr_reservation_by_terminal` on
  `(settled, terminal_at)` storing the three columns the rollup reads, and
  waits for its backfill like the other reservation indexes.
- `clickhouse/rollup_reservation_overruns.py` forces that index so the read
  can never regress to a base-table scan.
- The ClickHouse node's copy of the rollup is refreshed after the deploy that
  creates the index, not before: the forced index must exist first.

## Verification

After the next hourly run, the rollup's row in
`SPANNER_SYS.QUERY_STATS_TOP_10MINUTE` should scan roughly as many rows as it
returns, and the policy replay should show no user high-priority point above
30% at the top of the hour.
