-- Hourly settled reservation overruns, recomputed from authoritative Spanner rows.

CREATE TABLE IF NOT EXISTS tr.reservation_overruns ON CLUSTER trustedrouter
(
    hour                        DateTime('UTC'),
    hold_usage_type             LowCardinality(String),
    settled_n                   UInt64,
    overrun_n                   UInt64,
    overrun_micro               UInt64,
    max_single_overrun_micro    UInt64,
    refreshed_at                DateTime('UTC')
)
ENGINE = ReplicatedReplacingMergeTree(
    '/trustedrouter/tables/{shard}/reservation_overruns-v1',
    '{replica}',
    refreshed_at
)
ORDER BY (hour, hold_usage_type);
