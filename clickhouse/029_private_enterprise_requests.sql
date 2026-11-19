-- Deliberately outside tr.* and default.* (analytics and archive readers).
CREATE DATABASE IF NOT EXISTS tr_enterprise ON CLUSTER trustedrouter;
CREATE TABLE IF NOT EXISTS tr_enterprise.requests ON CLUSTER trustedrouter
(
    request_id UUID,
    requested_at DateTime64(3, 'UTC'),
    request_type Enum8('BUYER_DECK' = 1, 'SECURITY_PACK' = 2, 'MARKET_DATA' = 3, 'SAMPLE_DATASET' = 4),
    source_site LowCardinality(String),
    email String
)
ENGINE = ReplicatedReplacingMergeTree('/trustedrouter/tables/{shard}/private-enterprise-requests-v1', '{replica}')
PARTITION BY toYYYYMM(requested_at)
ORDER BY request_id
TTL toDateTime(requested_at) + INTERVAL 180 DAY DELETE;
