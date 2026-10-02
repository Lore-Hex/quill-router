-- G6 in docs/design/clickhouse-high-availability.md: verify_spanner_delivery
-- looks activity rows up by generation_id, which is not a prefix of the sort key
-- (tenant_id, created_at, generation_id), so every lookup read the whole table
-- with FINAL. This projection, ordered by generation_id, finds every stored copy
-- of an ID through its own primary index; the full rows are then read by sort
-- key.
--
-- ReplacingMergeTree accepts a projection only when deduplicate_merge_projection_mode
-- says what a deduplicating merge does to it: 'rebuild' recomputes it for the
-- merged part. MATERIALIZE builds it for existing parts, as a background
-- mutation on every replica.
ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter
    MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild';
ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter
    ADD PROJECTION IF NOT EXISTS by_generation_id
    (SELECT generation_id, tenant_id, created_at ORDER BY generation_id);
ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter
    MATERIALIZE PROJECTION by_generation_id;
