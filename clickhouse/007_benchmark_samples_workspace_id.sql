-- Add `workspace_id` to the benchmark/usage sample table.
--
-- WHY: Spanner's `tr_generation` carries a
-- `ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))`, so
-- per-workspace usage history there is a rolling 30-day window — a customer's
-- lifetime totals silently shrink as their traffic ages out. ClickHouse keeps
-- these rows for 400 days but had no tenant column, so it could not answer
-- "how much has this customer used" at all. This column is what makes durable
-- per-customer usage history possible.
--
-- PRIVACY NOTE: this table also feeds PUBLIC surfaces (leaderboard, provider
-- and model rankings, the /apps directory). Those consumers aggregate samples
-- into their own explicit dicts and must never project `workspace_id` into a
-- response; `tests/test_analytics_workspace_id.py` pins that boundary. The
-- node itself is VPC-internal with no public IP, which is why carrying a
-- tenant id here is acceptable where it would not be in a public export.
--
-- Backfill is deliberately NOT attempted: rows written before this change
-- have no tenant attribution anywhere recoverable (Spanner has already
-- deleted anything older than 30 days), so historical rows keep the DEFAULT
-- ''. Treat `workspace_id = ''` as "unattributed", not "no workspace".
--
-- Idempotent: `IF NOT EXISTS` makes re-running safe.

ALTER TABLE provider_benchmark_samples
    ADD COLUMN IF NOT EXISTS workspace_id LowCardinality(String) DEFAULT ''
    AFTER source;
