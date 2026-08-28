-- Column-for-column single-node counterpart to 014_reservation_overruns.sql.
-- The engine is the only difference from the replicated definition.

CREATE TABLE IF NOT EXISTS reservation_overruns
(
    hour                        DateTime('UTC'),
    hold_usage_type             LowCardinality(String),
    settled_n                   UInt64,
    overrun_n                   UInt64,
    overrun_micro               UInt64,
    max_single_overrun_micro    UInt64,
    refreshed_at                DateTime('UTC')
)
ENGINE = ReplacingMergeTree(refreshed_at)
ORDER BY (hour, hold_usage_type);
