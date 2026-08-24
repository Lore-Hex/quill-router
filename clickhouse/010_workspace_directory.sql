-- Workspace directory metadata contains workspace ids and names only.

CREATE TABLE IF NOT EXISTS tr.workspace_directory ON CLUSTER trustedrouter
(
    tenant_id           FixedString(64),
    workspace_id        LowCardinality(String),
    workspace_name      String,
    deleted             UInt8,
    workspace_created_at DateTime('UTC'),
    refreshed_at        DateTime('UTC')
)
ENGINE = ReplicatedReplacingMergeTree(
    '/trustedrouter/tables/{shard}/workspace_directory-v1',
    '{replica}',
    refreshed_at
)
ORDER BY tenant_id;
