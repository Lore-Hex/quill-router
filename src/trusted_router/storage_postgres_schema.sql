CREATE TABLE IF NOT EXISTS tr_entities (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    body JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, id)
);

CREATE TABLE IF NOT EXISTS tr_credit_balance (
    workspace_id TEXT NOT NULL,
    shard BIGINT NOT NULL DEFAULT 0,
    total_credits BIGINT NOT NULL DEFAULT 0,
    total_usage BIGINT NOT NULL DEFAULT 0,
    reserved BIGINT NOT NULL DEFAULT 0,
    source_updated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, shard)
);

CREATE TABLE IF NOT EXISTS tr_key_limit (
    workspace_id TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    shard BIGINT NOT NULL DEFAULT 0,
    limit_micro BIGINT,
    usage BIGINT NOT NULL DEFAULT 0,
    byok_usage BIGINT NOT NULL DEFAULT 0,
    reserved BIGINT NOT NULL DEFAULT 0,
    include_byok BOOLEAN NOT NULL DEFAULT TRUE,
    day_limit_micro BIGINT,
    week_limit_micro BIGINT,
    month_limit_micro BIGINT,
    source_updated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, key_hash, shard)
);
