-- Single-node (AWS) variant of 012. MUST be applied to a node before the
-- clickhouse/ tree is next shipped there: the updated drain inserts the
-- workspace_id column, and an un-migrated node rejects the insert.
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS workspace_id LowCardinality(String) DEFAULT '' AFTER tenant_id;
