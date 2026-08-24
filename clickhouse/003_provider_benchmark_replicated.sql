-- Staging table used to migrate the single local analytics table to one shard
-- with three ClickHouse replicas. The migration script creates this table on
-- every replica, verifies full fingerprints, then renames it to the canonical
-- `provider_benchmark_samples` name. The Keeper path is intentionally stable
-- across table renames.

CREATE TABLE IF NOT EXISTS provider_benchmark_samples_replicated
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
ENGINE = ReplicatedReplacingMergeTree(
    '/trustedrouter/tables/{shard}/provider_benchmark_samples-v1',
    '{replica}',
    created_at
)
PARTITION BY toYYYYMM(created_at)
ORDER BY (provider, model, created_at, id)
TTL toDateTime(created_at) + INTERVAL 400 DAY;
