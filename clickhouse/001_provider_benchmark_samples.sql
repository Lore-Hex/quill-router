-- TrustedRouter analytics schema, increment 1: provider benchmark samples.
--
-- This is the leaderboard / route-health dataset that currently lives in
-- Bigtable as `benchmark_*#...` rows, one JSON blob per row in column family
-- `m`. Every aggregate over it today is computed by scanning rows and
-- json.loads-ing each one in Python (storage_gcp_benchmark_index.py,
-- synthetic/backfill_rollups.py). That is the workload this schema removes.
--
-- Design notes:
--
-- * ReplacingMergeTree keyed on the full sort key makes ingestion IDEMPOTENT.
--   Backfill from Bigtable can be re-run, and the dual-write path can retry,
--   without double-counting. This is the property that makes the migration
--   safe to attempt more than once.
-- * The sort key leads with (provider, model) because every operational query
--   is scoped to one route; `created_at` after it gives the newest-first range
--   scan that the Bigtable reverse-timestamp row key provides today.
-- * LowCardinality on the dimension columns: provider/model/status are a few
--   hundred distinct values over millions of rows, so this is a large win on
--   both storage and GROUP BY speed.
-- * TTL replaces the Bigtable GC policy. Set deliberately long here; the
--   rollup views below retain aggregates after raw rows expire.

CREATE TABLE IF NOT EXISTS provider_benchmark_samples
(
    id                       String,
    created_at               DateTime64(3, 'UTC'),
    provider                 LowCardinality(String),
    model                    LowCardinality(String),
    provider_name            LowCardinality(String),
    status                   LowCardinality(String),
    usage_type               LowCardinality(String),
    source                   LowCardinality(String),
    streamed                 UInt8,

    input_tokens             UInt32,
    output_tokens            UInt32,
    total_cost_microdollars  Int64,

    speed_tokens_per_second  Nullable(Float32),
    elapsed_milliseconds     Nullable(UInt32),
    first_token_milliseconds Nullable(UInt32),
    ttfb_milliseconds        Nullable(UInt32),

    finish_reason            LowCardinality(Nullable(String)),
    error_type               LowCardinality(Nullable(String)),
    error_status             Nullable(UInt16),
    error_message            Nullable(String),
    region                   LowCardinality(Nullable(String)),
    app                      String
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (provider, model, created_at, id)
TTL toDateTime(created_at) + INTERVAL 400 DAY;


-- Hourly route-health aggregates, maintained incrementally.
--
-- Scope: this covers the ProviderBenchmarkSample dataset (provider/model)
-- only. It is NOT a replacement for synthetic/backfill_rollups.py, which
-- rolls up a DIFFERENT dataset — SyntheticProbeSample, keyed by
-- component/target/probe_type/region. Retiring that job is separate work.
--
-- The WHERE clause deliberately mirrors synthetic/route_health.py: synthetic
-- source only, only `error`/`success` statuses (so `unsupported` is excluded
-- from BOTH numerator and denominator), and transient failures dropped
-- entirely rather than counted. Without those exclusions this view is not
-- route health at all — on a 30.8k-row sample the unfiltered numbers are
-- 30832 samples / 1176 failures, against the correct 28214 / 59. A 20x
-- overstatement of failures is precisely the kind of plausible-looking wrong
-- answer that survives code review.
--
-- ifNull() on the transient predicate is load-bearing for the same reason it
-- is in the query path: `NULL IN (...)` is NULL, not false, and a NULL here
-- would silently drop rows from the aggregate.
--
-- Aggregate STATES are stored (not finalised numbers) so they can be merged
-- across any window at read time: the same view answers "last 48h" and
-- "last 90 days" without storing either.
--
-- !! IDEMPOTENCY WARNING !!
-- The source table's ReplacingMergeTree does NOT protect this view.
-- Materialized views run per INSERT BLOCK, before any replacement happens,
-- so re-inserting the same rows adds duplicate aggregate states here even
-- though the source table later collapses them. Re-running a backfill
-- therefore inflates this view permanently. Ingestion must either be
-- append-once, or use insert_deduplication_token, or the view must be
-- rebuilt (DROP + re-populate) alongside any source reload.
CREATE MATERIALIZED VIEW IF NOT EXISTS route_health_hourly
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(hour)
ORDER BY (provider, model, hour)
AS
SELECT
    provider,
    model,
    toStartOfHour(created_at)                        AS hour,
    countState()                                     AS samples_state,
    countIfState(status = 'error')                   AS failures_state,
    countIfState(status = 'success')                 AS successes_state,
    quantileState(0.95)(elapsed_milliseconds)        AS p95_elapsed_state,
    sumState(toInt64(input_tokens + output_tokens))  AS tokens_state
FROM provider_benchmark_samples
WHERE source = 'synthetic'
  AND status IN ('error', 'success')
  AND NOT (status = 'error' AND (
        ifNull(error_status, 0) IN (429,500,502,503,504,529)
     OR ifNull(error_type, '') IN ('ReadTimeout','ConnectTimeout','WriteTimeout','PoolTimeout',
                                   'ConnectError','ReadError','WriteError','RemoteProtocolError')))
GROUP BY provider, model, hour;
