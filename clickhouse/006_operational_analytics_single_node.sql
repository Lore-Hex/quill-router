-- Operational metadata tables for a STANDALONE ClickHouse node.
--
-- Column-for-column identical to 004_operational_analytics_replicated.sql --
-- the drain writes the same JSONEachRow batch to every node it is configured
-- with, so a column that differs between the two files is a node that silently
-- rejects half the traffic. The ONLY difference is the engine.
--
-- Why plain ReplacingMergeTree and not the Replicated variant:
--
-- The GCP cluster replicates through a 3-node ClickHouse Keeper quorum. The
-- AWS clouds run TWO nodes, in two regions, and across exactly two members a
-- quorum is worse than useless: two Keeper nodes cannot form a majority when
-- either one dies, so losing a region would FREEZE WRITES on the survivor --
-- turning a durability improvement into an availability regression.
--
-- So these nodes do not know about each other at all. The drain
-- (clickhouse/ingest_operational_outbox_postgres.py) writes each batch to
-- every node and deletes the outbox rows only once all of them accepted it.
-- Redelivery after a partial failure is what makes that safe, and THIS ENGINE
-- is what makes redelivery safe: ReplacingMergeTree keyed on `ingest_version`
-- collapses a row that arrives twice. `ingest_version` is the outbox row's
-- enqueue timestamp, not the ingest wall clock, so the same event redelivered
-- an hour later carries the same version and collapses rather than racing.
--
-- Keep the ORDER BY, PARTITION BY and TTL clauses in step with 004 as well:
-- ReplacingMergeTree deduplicates within a partition on the sort key, so a
-- divergent ORDER BY changes what "the same row" means on one node only.

CREATE TABLE IF NOT EXISTS activity_generations
(
    generation_id               String,
    request_id                  String,
    tenant_id                   FixedString(64),
    key_id                      FixedString(64),
    model                       LowCardinality(String),
    provider                    LowCardinality(String),
    provider_name               LowCardinality(String),
    app                         String,
    tokens_prompt               UInt64,
    tokens_completion           UInt64,
    cached_input_tokens         UInt64,
    reasoning_tokens            UInt64,
    total_cost_microdollars     Int64,
    usage_type                  LowCardinality(String),
    speed_tokens_per_second     Float64,
    finish_reason               LowCardinality(String),
    status                      LowCardinality(String),
    streamed                    UInt8,
    usage_estimated             UInt8,
    elapsed_milliseconds        Nullable(UInt64),
    first_token_milliseconds    Nullable(UInt64),
    ttfb_milliseconds           Nullable(UInt64),
    region                      LowCardinality(Nullable(String)),
    user                        Nullable(String),
    session_id                  Nullable(String),
    http_referer                Nullable(String),
    app_categories              Array(String),
    tags                        Map(String, String),
    created_at                  DateTime64(3, 'UTC'),
    ingest_version              DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMM(created_at)
ORDER BY (tenant_id, created_at, generation_id)
TTL toDateTime(created_at) + INTERVAL 400 DAY;

CREATE TABLE IF NOT EXISTS synthetic_probe_samples
(
    id                              String,
    probe_type                      LowCardinality(String),
    target                          LowCardinality(String),
    target_url                      String,
    monitor_region                  LowCardinality(String),
    status                          LowCardinality(String),
    target_region                   LowCardinality(Nullable(String)),
    latency_milliseconds            Nullable(UInt64),
    ttfb_milliseconds               Nullable(UInt64),
    dns_milliseconds                Nullable(UInt64),
    tcp_connect_milliseconds        Nullable(UInt64),
    tls_handshake_milliseconds      Nullable(UInt64),
    gateway_processing_milliseconds Nullable(UInt64),
    connection_reused               Nullable(UInt8),
    protocol                        LowCardinality(Nullable(String)),
    http_status                     Nullable(UInt16),
    error_type                      LowCardinality(Nullable(String)),
    provider                        LowCardinality(Nullable(String)),
    model                           LowCardinality(Nullable(String)),
    selected_provider               LowCardinality(Nullable(String)),
    selected_model                  LowCardinality(Nullable(String)),
    generation_id                   Nullable(String),
    attestation_digest              Nullable(String),
    source_commit                   Nullable(String),
    cost_microdollars               Int64,
    output_match                    Nullable(UInt8),
    created_at                      DateTime64(3, 'UTC'),
    ingest_version                  DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMMDD(created_at)
ORDER BY (target, probe_type, monitor_region, created_at, id)
TTL toDateTime(created_at) + INTERVAL 14 DAY;

CREATE TABLE IF NOT EXISTS spend_lease_shadow
(
    event_id                    String,
    created_at                  DateTime64(3, 'UTC'),
    workspace_id                String,
    key_hash                    FixedString(64),
    boot_kid                    String,
    boot_verified               UInt8,
    lease_id                    Nullable(String),
    no_lease_reason             Nullable(String) DEFAULT NULL,
    echo_state                  LowCardinality(String),
    would_admit                 Nullable(UInt8),
    enclave_estimate_micro      Nullable(Int64),
    server_estimate_micro       Nullable(Int64),
    server_verdict              LowCardinality(String),
    catalog_version             Nullable(String),
    divergence                  LowCardinality(String),
    schema_version              UInt8,
    ingest_version              DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMMDD(created_at)
ORDER BY (workspace_id, created_at, event_id)
TTL toDateTime(created_at) + INTERVAL 30 DAY;

ALTER TABLE spend_lease_shadow
    ADD COLUMN IF NOT EXISTS no_lease_reason Nullable(String) DEFAULT NULL AFTER lease_id;

CREATE TABLE IF NOT EXISTS synthetic_status_rollups
(
    id                           String,
    period                       LowCardinality(String),
    period_start                 DateTime('UTC'),
    component                    LowCardinality(String),
    target                       LowCardinality(String),
    probe_type                   LowCardinality(String),
    monitor_region               LowCardinality(String),
    target_region                LowCardinality(String),
    sample_count                 UInt64,
    up_count                     UInt64,
    down_count                   UInt64,
    degraded_count               UInt64,
    routing_degraded_count       UInt64,
    trust_degraded_count         UInt64,
    unknown_count                UInt64,
    latency_histogram            Map(String, UInt64),
    ttfb_histogram               Map(String, UInt64),
    dns_histogram                Map(String, UInt64),
    tcp_connect_histogram        Map(String, UInt64),
    tls_handshake_histogram      Map(String, UInt64),
    gateway_processing_histogram Map(String, UInt64),
    error_counts                 Map(String, UInt64),
    last_checked_at              Nullable(DateTime64(3, 'UTC')),
    cost_microdollars            Int64,
    updated_at                   DateTime64(3, 'UTC'),
    ingest_version               DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMM(period_start)
ORDER BY (
    period, period_start, component, target, probe_type, monitor_region,
    target_region, id
)
TTL period_start + INTERVAL 24 MONTH;

CREATE TABLE IF NOT EXISTS public_analytics_snapshots
(
    name           LowCardinality(String),
    generated_at   DateTime64(3, 'UTC'),
    payload        String CODEC(ZSTD(3)),
    ingest_version DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMM(generated_at)
ORDER BY name
TTL toDateTime(generated_at) + INTERVAL 7 DAY;
