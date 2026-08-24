-- Client-observed reliability telemetry for a STANDALONE ClickHouse node.
--
-- Column-for-column identical to 008_client_events_replicated.sql; only the
-- engines differ. Sampled request events live for 90 days, exact minute
-- counters for 180 days, rollups for 24 months, and quarantine rows for 30
-- days. The raw client tables are deliberately NOT archived to Parquet.

ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS gateway_request_id String DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS synthetic UInt8 DEFAULT 0;
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_source LowCardinality(String) DEFAULT 'none';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_sdk LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_sdk_version LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_lang LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_runtime LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_os LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_arch LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_timeout_ms Nullable(UInt32) DEFAULT NULL;
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_attempt Nullable(UInt8) DEFAULT NULL;
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_prev_outcome LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_prev_error_class LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_prev_host LowCardinality(String) DEFAULT '';
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_prev_elapsed_ms Nullable(UInt32) DEFAULT NULL;
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_since_first_ms Nullable(UInt32) DEFAULT NULL;
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_stream Nullable(UInt8) DEFAULT NULL;
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS client_failover_used Nullable(UInt8) DEFAULT NULL;

CREATE TABLE IF NOT EXISTS client_request_events
(
    event_id                      FixedString(64),
    tenant_id                     FixedString(64),
    key_id                        FixedString(64),
    batch_id                      FixedString(32),
    instance_id                   FixedString(16),
    seq                           UInt32,
    received_at                   DateTime64(3, 'UTC'),
    created_at                    DateTime64(3, 'UTC'),
    clock_skew_ms                 Int64,
    synthetic                     UInt8,
    sdk                           LowCardinality(String),
    sdk_version                   LowCardinality(String),
    lang                          LowCardinality(String),
    runtime                       LowCardinality(String),
    os                            LowCardinality(String),
    arch                          LowCardinality(String),
    plane                         LowCardinality(String),
    endpoint                      LowCardinality(String),
    method                        LowCardinality(String),
    streaming                     UInt8,
    provider_pinned               UInt8,
    model                         LowCardinality(String),
    final_outcome                 LowCardinality(String),
    final_http_status             UInt16,
    final_host                    LowCardinality(String),
    first_error_class             LowCardinality(String),
    error_source                  LowCardinality(String),
    total_ms                      UInt32,
    ttft_ms                       Nullable(UInt32),
    timeout_phase                 LowCardinality(String),
    configured_timeout_ms         Nullable(UInt32),
    attempt_count                 UInt8,
    failover_used                 UInt8,
    attempt_host                  Array(LowCardinality(String)),
    attempt_outcome               Array(LowCardinality(String)),
    attempt_http_status           Array(UInt16),
    attempt_error_class           Array(LowCardinality(String)),
    attempt_error_source          Array(LowCardinality(String)),
    attempt_should_retry          Array(LowCardinality(String)),
    attempt_retry_after_ms        Array(UInt32),
    attempt_elapsed_ms            Array(UInt32),
    attempt_ttfb_ms               Array(UInt32),
    attempt_request_id            Array(String),
    attempt_moved                 Array(UInt8),
    sample_rate                   Float32,
    sample_reason                 LowCardinality(String),
    tr_fault                      UInt8,
    methodology_version           UInt8,
    ingest_version                DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMM(created_at)
ORDER BY (tenant_id, created_at, event_id)
TTL toDateTime(created_at) + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS client_minute_counters
(
    event_id                    FixedString(64),
    tenant_id                   FixedString(64),
    key_id                      FixedString(64),
    instance_id                 FixedString(16),
    bucket_start                DateTime('UTC'),
    received_at                 DateTime64(3, 'UTC'),
    synthetic                   UInt8,
    sdk                         LowCardinality(String),
    sdk_version                 LowCardinality(String),
    level                       LowCardinality(String),
    endpoint                    LowCardinality(String),
    streaming                   UInt8,
    host                        LowCardinality(String),
    outcome                     LowCardinality(String),
    error_class                 LowCardinality(String),
    http_status_class           LowCardinality(String),
    timeout_phase               LowCardinality(String),
    timeout_floor_met           UInt8,
    provider_pinned             UInt8,
    requests                    UInt64,
    attempts                    UInt64,
    failover_used               UInt64,
    first_attempt_success       UInt64,
    total_ms_hist               Map(String, UInt64),
    first_event_ms_hist         Map(String, UInt64),
    tr_fault                    UInt8,
    methodology_version         UInt8,
    ingest_version              DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMM(bucket_start)
ORDER BY (
    tenant_id, bucket_start, level, host, endpoint, outcome, error_class,
    event_id
)
TTL bucket_start + INTERVAL 180 DAY;

CREATE TABLE IF NOT EXISTS client_availability_rollups
(
    id                         String,
    period                     LowCardinality(String),
    period_start               DateTime('UTC'),
    scope                      LowCardinality(String),
    tenant_id                  FixedString(64),
    host                       LowCardinality(String),
    endpoint                   LowCardinality(String),
    sdk                        LowCardinality(String),
    requests                   UInt64,
    successes                  UInt64,
    tr_fault_failures          UInt64,
    excluded_failures          UInt64,
    aborted                    UInt64,
    attempts                   UInt64,
    attempt_tr_fault           UInt64,
    failover_used              UInt64,
    first_attempt_success      UInt64,
    distinct_tenants           UInt32,
    capped_requests            UInt64,
    coverage_requests          UInt64,
    total_ms_hist              Map(String, UInt64),
    first_event_ms_hist        Map(String, UInt64),
    methodology_version        UInt8,
    computed_at                DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY toYYYYMM(period_start)
ORDER BY (period, period_start, scope, tenant_id, host, endpoint, sdk, id)
TTL period_start + INTERVAL 24 MONTH;

CREATE TABLE IF NOT EXISTS operational_outbox_quarantine
(
    shard            UInt8,
    commit_ts        DateTime64(6, 'UTC'),
    event_kind       String,
    event_id         String,
    payload          String CODEC(ZSTD(3)),
    reason           String,
    quarantined_at   DateTime64(3, 'UTC')
)
ENGINE = MergeTree
ORDER BY (commit_ts, event_kind, event_id)
TTL toDateTime(commit_ts) + INTERVAL 30 DAY;
