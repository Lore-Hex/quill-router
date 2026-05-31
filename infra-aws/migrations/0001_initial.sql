-- trustedrouter.eu Aurora PostgreSQL initial schema.
--
-- Same (kind, id, body, updated_at) shape as Spanner's tr_entities so
-- the Store protocol port from storage_gcp.py → storage_aws.py is a
-- mechanical translation: identical entity-kind taxonomy, identical
-- JSON body shapes, identical idempotency contracts (the audit in
-- tests/test_credit_ledger_idempotency_audit.py applies unchanged).
--
-- JSONB instead of TEXT for `body` so Postgres can index into JSON
-- fields if any future query needs it (Spanner uses STRING(MAX) with
-- application-side JSON encode/decode). The application code stays
-- JSON-string-shaped at the boundary; psycopg auto-encodes Python
-- dicts to JSONB on insert and decodes back on select.
--
-- updated_at TRIGGER mirrors Spanner's commit_timestamp PSEUDO_COLUMN
-- behavior: every UPDATE bumps the timestamp to NOW(). INSERT can
-- pass an explicit value (matches `updated_at=iso_now()` calls in
-- storage_gcp.py) or default to NOW().

CREATE TABLE IF NOT EXISTS tr_entities (
    kind       VARCHAR(64)  NOT NULL,
    id         VARCHAR(512) NOT NULL,
    body       JSONB        NOT NULL,
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, id)
);

-- For range scans by recency (e.g., the find_gabriella-shaped queries
-- against recent workspaces, the recent-stripe-events queries the
-- console pages issue).
CREATE INDEX IF NOT EXISTS idx_tr_entities_kind_updated_at
    ON tr_entities (kind, updated_at DESC);

-- Touch trigger: bump updated_at on every UPDATE so the app can rely
-- on it for change-stream-style queries the same way it relies on
-- Spanner's allow_commit_timestamp=true semantics.
CREATE OR REPLACE FUNCTION tr_entities_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_entities_touch_updated_at_trg ON tr_entities;
CREATE TRIGGER tr_entities_touch_updated_at_trg
    BEFORE UPDATE ON tr_entities
    FOR EACH ROW EXECUTE FUNCTION tr_entities_touch_updated_at();

-- Generations: append-only LLM-call records. On GCP these live in
-- Bigtable for cheap append semantics. On AWS we use a regular
-- Postgres table with a BRIN index on created_at — Aurora's read
-- replicas + auto-storage handle the volume fine at our current
-- scale. If it ever grows past Aurora's comfort zone, the migration
-- target is Aurora I/O-Optimized or a partitioned-by-month table.
CREATE TABLE IF NOT EXISTS tr_generations (
    id              VARCHAR(64)  NOT NULL,
    workspace_id    VARCHAR(64)  NOT NULL,
    body            JSONB        NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_tr_generations_workspace_created
    ON tr_generations (workspace_id, created_at DESC);

-- BRIN is the right index type for append-only time-series — much
-- smaller than a btree and the query pattern (range scan by recency)
-- exploits BRIN's strengths.
CREATE INDEX IF NOT EXISTS idx_tr_generations_created_at_brin
    ON tr_generations USING BRIN (created_at);
