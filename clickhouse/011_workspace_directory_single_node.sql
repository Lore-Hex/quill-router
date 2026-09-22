-- Column-for-column single-node counterpart to 010_workspace_directory.sql.
-- The engine is the only difference from the replicated definition.

CREATE TABLE IF NOT EXISTS workspace_directory
(
    tenant_id           FixedString(64),
    workspace_id        LowCardinality(String),
    workspace_name      String,
    billing_account_fingerprint String DEFAULT '',
    deleted             UInt8,
    workspace_created_at DateTime('UTC'),
    refreshed_at        DateTime('UTC')
)
ENGINE = ReplacingMergeTree(refreshed_at)
ORDER BY tenant_id;
ALTER TABLE workspace_directory
    ADD COLUMN IF NOT EXISTS billing_account_fingerprint String DEFAULT '';
