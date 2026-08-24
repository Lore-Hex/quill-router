-- Three-replica provider analytics rollups. Temporary staging tables remain
-- local to the rollup worker; only published aggregates need replication.

CREATE TABLE IF NOT EXISTS provider_analytics_hourly_replicated
(
    period_start                 DateTime('UTC'),
    provider                     LowCardinality(String),
    model                        LowCardinality(String),
    source                       LowCardinality(String),
    region                       LowCardinality(String),
    usage_type                   LowCardinality(String),
    status                       LowCardinality(String),
    error_type                   LowCardinality(String),
    error_status                 UInt16,
    streamed                     UInt8,
    attempts                     UInt64,
    completed                    UInt64,
    failed                       UInt64,
    input_tokens                 UInt64,
    output_tokens                UInt64,
    total_cost_microdollars      Int64,
    p50_elapsed_milliseconds     Nullable(Float64),
    p95_elapsed_milliseconds     Nullable(Float64),
    p50_first_token_milliseconds Nullable(Float64),
    p95_first_token_milliseconds Nullable(Float64),
    p50_ttfb_milliseconds        Nullable(Float64),
    p95_ttfb_milliseconds        Nullable(Float64),
    p50_tokens_per_second        Nullable(Float64),
    p95_tokens_per_second        Nullable(Float64)
)
ENGINE = ReplicatedMergeTree(
    '/trustedrouter/tables/{shard}/provider-analytics-hourly-v1',
    '{replica}'
)
PARTITION BY toYYYYMMDD(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
)
TTL period_start + INTERVAL 3 YEAR;

CREATE TABLE IF NOT EXISTS provider_analytics_daily_replicated
AS provider_analytics_hourly_replicated
ENGINE = ReplicatedMergeTree(
    '/trustedrouter/tables/{shard}/provider-analytics-daily-v1',
    '{replica}'
)
PARTITION BY toYYYYMM(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
);

CREATE TABLE IF NOT EXISTS provider_analytics_monthly_replicated
AS provider_analytics_hourly_replicated
ENGINE = ReplicatedMergeTree(
    '/trustedrouter/tables/{shard}/provider-analytics-monthly-v1',
    '{replica}'
)
PARTITION BY toYYYYMM(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
);
