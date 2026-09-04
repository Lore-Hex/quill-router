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

-- Index sort order is deliberately ASCENDING everywhere.
--
-- Aurora DSQL rejects DESC in index keys outright: "specifying sort order
-- not supported for index keys". An ascending index still serves a
-- descending ORDER BY through a reverse scan, so dropping the explicit
-- order costs nothing on stock Postgres or Spanner PG and is the
-- difference between this schema applying on DSQL or not.
--
-- DSQL also requires CREATE INDEX ASYNC; that is handled in
-- storage_postgres.py::_execute_ddl rather than here, so this file stays
-- portable SQL.

CREATE INDEX IF NOT EXISTS tr_entities_recent
    ON tr_entities (kind, indexed_at, id);
CREATE INDEX IF NOT EXISTS tr_entities_day_recent
    ON tr_entities (kind, index_date, indexed_at, id);
CREATE INDEX IF NOT EXISTS tr_entities_day_probe_target_recent
    ON tr_entities (
        kind,
        index_date,
        index_target,
        index_probe_type,
        indexed_at,
        id
    );
CREATE INDEX IF NOT EXISTS tr_entities_target_recent
    ON tr_entities (kind, index_target, indexed_at, id);
CREATE INDEX IF NOT EXISTS tr_entities_probe_target_recent
    ON tr_entities (
        kind,
        index_probe_type,
        index_target,
        indexed_at,
        id
    );
CREATE INDEX IF NOT EXISTS tr_entities_monitor_recent
    ON tr_entities (kind, index_monitor_region, indexed_at, id);
CREATE INDEX IF NOT EXISTS tr_entities_period_recent
    ON tr_entities (kind, index_period, indexed_at, id);

CREATE TABLE IF NOT EXISTS tr_credit_balance (
    workspace_id TEXT NOT NULL,
    shard BIGINT NOT NULL DEFAULT 0,
    total_credits BIGINT NOT NULL DEFAULT 0,
    total_usage BIGINT NOT NULL DEFAULT 0,
    reserved BIGINT NOT NULL DEFAULT 0,
    trust_tier BIGINT DEFAULT 0,
    trust_computed_at TIMESTAMPTZ,
    trust_latched_at TIMESTAMPTZ,
    trust_override_tier BIGINT,
    billing_pause_causes JSONB,
    pause_epoch BIGINT DEFAULT 0,
    trust_reconciled_through TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, shard)
);

ALTER TABLE tr_credit_balance ADD COLUMN IF NOT EXISTS trust_tier BIGINT DEFAULT 0;
ALTER TABLE tr_credit_balance ADD COLUMN IF NOT EXISTS trust_computed_at TIMESTAMPTZ;
ALTER TABLE tr_credit_balance ADD COLUMN IF NOT EXISTS trust_latched_at TIMESTAMPTZ;
ALTER TABLE tr_credit_balance ADD COLUMN IF NOT EXISTS trust_override_tier BIGINT;
ALTER TABLE tr_credit_balance ADD COLUMN IF NOT EXISTS billing_pause_causes JSONB;
ALTER TABLE tr_credit_balance ADD COLUMN IF NOT EXISTS pause_epoch BIGINT DEFAULT 0;
ALTER TABLE tr_credit_balance ADD COLUMN IF NOT EXISTS trust_reconciled_through TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS tr_trust_event (
    workspace_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('payment', 'refund', 'dispute', 'abuse', 'grant')),
    provider TEXT NOT NULL CHECK (provider IN ('stripe', 'paypal', 'adyen', 'x402', 'operator', 'system')),
    amount_micro BIGINT,
    original_payment_ref TEXT,
    adverse_ref TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payment_amount_micro BIGINT,
    currency TEXT,
    credited_micro BIGINT,
    recovered_micro BIGINT,
    provider_subtype TEXT,
    lifecycle_status TEXT CHECK (lifecycle_status IS NULL OR lifecycle_status IN ('pending', 'succeeded', 'failed', 'reversed', 'won', 'lost', 'closed', 'terminal_by_horizon')),
    cumulative_refunded BIGINT,
    recovery_target BIGINT,
    debit_status TEXT CHECK (debit_status IS NULL OR debit_status IN ('debited', 'partial', 'unrecovered')),
    unrecovered_micro BIGINT,
    provider_ordering_watermark TEXT,
    PRIMARY KEY (workspace_id, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS tr_trust_event_adverse_dedup
    ON tr_trust_event (provider, adverse_ref, kind);
CREATE UNIQUE INDEX IF NOT EXISTS tr_trust_event_payment_dedup
    ON tr_trust_event (provider, original_payment_ref, kind);

CREATE TABLE IF NOT EXISTS tr_earnings_balance (
    user_id TEXT NOT NULL,
    shard BIGINT NOT NULL DEFAULT 0,
    total_earned BIGINT NOT NULL DEFAULT 0,
    total_transferred BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, shard)
);

CREATE TABLE IF NOT EXISTS tr_credit_movement (
    account_id TEXT NOT NULL,
    movement_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount_microdollars BIGINT NOT NULL,
    counterparty_account_id TEXT,
    custom_model_id TEXT,
    authorization_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, movement_id)
);

-- Postgres has no Spanner-style row deletion policy. A future sweep must
-- delete tr_credit_movement rows older than 400 days.
-- Ascending keeps this index portable to Aurora DSQL; reverse scans serve the
-- newest-first read path without an explicit DESC key.
CREATE INDEX IF NOT EXISTS tr_credit_movement_by_time
    ON tr_credit_movement (account_id, created_at);

CREATE TABLE IF NOT EXISTS tr_user_lifetime_topup (
    user_id TEXT NOT NULL PRIMARY KEY,
    total_microdollars BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    -- Lazy rolling-window counters, mirroring the Spanner shape
    -- (scripts/deploy/migrate_typed_counters.sh:216-221). "Lazy" means no
    -- scheduled reset job: a window whose *_start is NULL or older than the
    -- current floor reads as ZERO and is rewritten on the next settle. That
    -- keeps enforcement correct without a cron that could silently stop and
    -- leave every key permanently over its limit.
    day_usage BIGINT NOT NULL DEFAULT 0,
    day_start TIMESTAMPTZ,
    week_usage BIGINT NOT NULL DEFAULT 0,
    week_start TIMESTAMPTZ,
    month_usage BIGINT NOT NULL DEFAULT 0,
    month_start TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, key_hash, shard)
);

-- The key-limit call family (reserve/settle/refund_key_limit) is keyed by
-- key_hash ALONE — Spanner's PK is (key_hash, shard), but the Postgres PK
-- leads with workspace_id, so none of those statements can use it. Without
-- this index every reserve is a full scan of tr_key_limit, which on Aurora
-- DSQL means a distributed scan on the hottest path in the product.
CREATE INDEX IF NOT EXISTS tr_key_limit_by_key_hash ON tr_key_limit (key_hash, shard);

-- Upgrade path for stores created before the window counters existed.
-- CREATE TABLE IF NOT EXISTS above is a no-op on an existing table, so the
-- columns would silently never appear and every window check would read a
-- missing column.
--
-- The usage columns are added BARE and then given a default, because Aurora
-- DSQL rejects a constraint in ADD COLUMN outright:
--     FeatureNotSupported: ALTER TABLE ADD COLUMN with constraint not supported
-- The tr_entities ALTERs above are all bare nullable types, so they never hit
-- this; `NOT NULL DEFAULT 0` does. Verified against a real DSQL cluster --
-- the fresh-create path (CREATE TABLE) passes either way, so this is only
-- reachable on an EXISTING deployment, which is every deployment that matters.
--
-- NOT NULL is deliberately NOT re-added on the upgrade path: DSQL would have
-- to validate it against existing rows, and the readers already coalesce a
-- NULL window counter to zero (a NULL *_start means "window not started",
-- which is the same lazy-floor case as a stale one).
ALTER TABLE tr_key_limit ADD COLUMN IF NOT EXISTS day_usage BIGINT;
ALTER TABLE tr_key_limit ALTER COLUMN day_usage SET DEFAULT 0;
ALTER TABLE tr_key_limit ADD COLUMN IF NOT EXISTS day_start TIMESTAMPTZ;
ALTER TABLE tr_key_limit ADD COLUMN IF NOT EXISTS week_usage BIGINT;
ALTER TABLE tr_key_limit ALTER COLUMN week_usage SET DEFAULT 0;
ALTER TABLE tr_key_limit ADD COLUMN IF NOT EXISTS week_start TIMESTAMPTZ;
ALTER TABLE tr_key_limit ADD COLUMN IF NOT EXISTS month_usage BIGINT;
ALTER TABLE tr_key_limit ALTER COLUMN month_usage SET DEFAULT 0;
ALTER TABLE tr_key_limit ADD COLUMN IF NOT EXISTS month_start TIMESTAMPTZ;

-- Durable hand-off to ClickHouse for tenant activity and synthetic status.
-- Rows are written in the SAME transaction as the settle/probe they describe,
-- so an event is delivered if and only if the write it describes committed.
--
-- The PRIMARY KEY is the event identity, NOT a timestamp. Spanner's outbox
-- orders by PENDING_COMMIT_TIMESTAMP(); Postgres has no equivalent, and a
-- now()-based cursor is unsafe here — a transaction that starts earlier can
-- commit later, so a drain checkpointing on max(enqueued_at) would silently
-- skip rows that appeared behind its cursor. The drain therefore deletes what
-- it has written rather than advancing a cursor, and this key is what makes
-- both the enqueue and that delete idempotent: an enqueue retried after an
-- OCC abort (SQLSTATE 40001) collides with itself instead of duplicating.
--
-- enqueued_at is NOT merely observability, despite only feeding the drain-lag
-- metric here. The drain forwards it as ClickHouse's `ingest_version`, which is
-- the ReplacingMergeTree version column that collapses a redelivered row onto
-- its original. That works only because the value is *stored* and therefore
-- identical on every redelivery. Never recompute it per delivery (a per-
-- statement now(), a backfill rewrite): two versions of one event stop
-- deduplicating and the duplicate becomes permanent.
CREATE TABLE IF NOT EXISTS tr_operational_analytics_outbox (
    shard BIGINT NOT NULL,
    event_kind TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (shard, event_kind, event_id)
);

-- Serves the drain's lag metric (ORDER BY enqueued_at LIMIT 1) as an index seek.
-- Without it that metric is a full table scan on every poll, which is worst
-- exactly when a backlog has made it expensive and most worth reading. The
-- batch SELECT deliberately does NOT order on this column — it orders on the
-- primary key prefix so its LIMIT is a bounded range scan.
CREATE INDEX IF NOT EXISTS tr_operational_analytics_outbox_enqueued_at_idx
    ON tr_operational_analytics_outbox (enqueued_at);

-- Deferred settlement: the peer plane's record of debt it owes the home
-- plane's ledger.
--
-- tr_deferred_outstanding is a per-workspace COUNTER, and it is the actual
-- bound on how much unsettled spend a workspace can run up while home is
-- unreachable. It is enforced by a conditional UPDATE at authorize
--   SET outstanding = outstanding + :estimate WHERE outstanding + :estimate <= :cap
-- because a read-then-check bounds nothing: the authorize-to-settle gap is a
-- whole provider stream, so every concurrent request would read the same
-- stale total and admit.
--
-- `dead_lettered` is a SEPARATE column, not a decrement to zero. A row whose
-- terms home rejects must stop consuming the workspace's serving headroom
-- (otherwise one corrupt row bricks the workspace forever) while the debt
-- stays visible for an operator to write off or replay deliberately.
CREATE TABLE IF NOT EXISTS tr_deferred_outstanding (
    workspace_id TEXT NOT NULL,
    outstanding BIGINT NOT NULL DEFAULT 0,
    dead_lettered BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id)
);

-- One row per settled deferred authorization, awaiting delivery to home.
--
-- authorization_id is the PRIMARY KEY and the cross-plane idempotency key:
-- home records a verdict per (source_plane, authorization_id), so a
-- redelivered row applies exactly once. cost_microdollars is the FROZEN
-- actual, never recomputed — the same rule the analytics outbox learned about
-- enqueued_at, for the same reason.
--
-- state: pending -> forwarded (home recorded it) | dead_letter (home returned
-- a STRUCTURED terms-conflict or unknown-workspace verdict; a bare 404 or an
-- unparseable body is an OUTAGE and stays pending, or a home rollback would
-- silently destroy the whole backlog).
CREATE TABLE IF NOT EXISTS tr_home_settlement_outbox (
    authorization_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    cost_microdollars BIGINT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts BIGINT NOT NULL DEFAULT 0,
    last_error TEXT,
    -- ISO-8601 TEXT, compared against a Python-supplied now — the same
    -- lexicographic-equals-chronological trick the reaper's expires_at uses,
    -- and for the same reason: one spelling that means the same thing on
    -- Postgres, DSQL and the SQLite test fake. Empty string = eligible now.
    -- Without this column the drain has no backoff at all: the oldest 50
    -- clamped rows would be re-presented to home every 20-45s pass, from
    -- every instance, while eligible rows behind them starve.
    next_attempt_at TEXT NOT NULL DEFAULT '',
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (authorization_id)
);

-- The drain's batch selector: pending rows oldest-first. Without it every
-- drain pass scans the whole table, which is worst exactly when a backlog has
-- made it expensive.
CREATE INDEX IF NOT EXISTS tr_home_settlement_outbox_pending_idx
    ON tr_home_settlement_outbox (state, next_attempt_at);
