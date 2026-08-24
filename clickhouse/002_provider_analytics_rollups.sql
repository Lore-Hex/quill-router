-- TrustedRouter analytics schema, increment 2: durable aggregate tiers.
--
-- These are rebuilt from `provider_benchmark_samples FINAL`; they are not
-- additive materialized views because the ingestion contract is at-least-once.
-- The rollup worker builds one partition in a staging table, verifies that
-- sum(attempts) equals the source row count, and atomically replaces the live
-- partition. Hourly detail is retained for three years. Daily and monthly
-- aggregates are intentionally retained without a TTL.

CREATE TABLE IF NOT EXISTS provider_analytics_hourly
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
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
)
TTL period_start + INTERVAL 3 YEAR;

CREATE TABLE IF NOT EXISTS provider_analytics_daily
AS provider_analytics_hourly
ENGINE = MergeTree
PARTITION BY toYYYYMM(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
);

CREATE TABLE IF NOT EXISTS provider_analytics_monthly
AS provider_analytics_hourly
ENGINE = MergeTree
PARTITION BY toYYYYMM(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
);

CREATE TABLE IF NOT EXISTS provider_analytics_hourly_staging
AS provider_analytics_hourly
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
);

CREATE TABLE IF NOT EXISTS provider_analytics_daily_staging
AS provider_analytics_daily
ENGINE = MergeTree
PARTITION BY toYYYYMM(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
);

CREATE TABLE IF NOT EXISTS provider_analytics_monthly_staging
AS provider_analytics_monthly
ENGINE = MergeTree
PARTITION BY toYYYYMM(period_start)
ORDER BY (
    period_start, provider, model, source, region, usage_type, status,
    error_type, error_status, streamed
);
