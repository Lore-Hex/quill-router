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


-- Hourly route health, maintained incrementally.
--
-- This is the direct replacement for the hand-rolled rollup machinery
-- (storage_gcp_synthetic_rollups.py + synthetic/backfill_rollups.py +
-- `synthetic_rollup#` row keys). AggregatingMergeTree keeps the partial
-- aggregate states up to date as rows arrive; nothing has to be backfilled
-- on a schedule, and there is no rollup job to fail silently.
--
-- Aggregate STATES are stored (not finalised numbers) so they can be merged
-- across any window at read time: the same view answers "last 48h" and
-- "last 90 days" without storing either.
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
GROUP BY provider, model, hour;
