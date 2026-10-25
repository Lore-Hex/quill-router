-- Apply with scripts/deploy/clickhouse_replicated_migrate.sh BEFORE the readers
-- and drain that name these columns ship: the reader's SELECT and the drain's
-- INSERT both fail on a cluster without them. 024 is the single-node variant of
-- the 023 projection; 026 is this file's single-node variant.
-- Apply before shipping the activity writers/readers. Historical rows have
-- no registered app attribution and zero markup, matching the public shape.
ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS app_id Nullable(String) DEFAULT NULL AFTER app;
ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS app_markup_microdollars Int64 DEFAULT 0 AFTER total_cost_microdollars;
ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS custom_model_markup_microdollars Int64 DEFAULT 0 AFTER app_markup_microdollars;
