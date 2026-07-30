CREATE TABLE IF NOT EXISTS tr_entities (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    body JSONB NOT NULL,
    indexed_at TIMESTAMPTZ,
    index_date TEXT,
    index_target TEXT,
    index_probe_type TEXT,
    index_monitor_region TEXT,
    index_period TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, id)
);

ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ;
ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS index_date TEXT;
ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS index_target TEXT;
ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS index_probe_type TEXT;
ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS index_monitor_region TEXT;
ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS index_period TEXT;

CREATE INDEX IF NOT EXISTS tr_entities_recent
    ON tr_entities (kind, indexed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS tr_entities_day_recent
    ON tr_entities (kind, index_date, indexed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS tr_entities_day_probe_target_recent
    ON tr_entities (
        kind,
        index_date,
        index_target,
        index_probe_type,
        indexed_at DESC,
        id DESC
    );
CREATE INDEX IF NOT EXISTS tr_entities_target_recent
    ON tr_entities (kind, index_target, indexed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS tr_entities_probe_target_recent
    ON tr_entities (
        kind,
        index_probe_type,
        index_target,
        indexed_at DESC,
        id DESC
    );
CREATE INDEX IF NOT EXISTS tr_entities_monitor_recent
    ON tr_entities (kind, index_monitor_region, indexed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS tr_entities_period_recent
    ON tr_entities (kind, index_period, indexed_at DESC, id DESC);

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
