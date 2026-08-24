-- New activity rows carry their workspace_id directly (2026-08-19 decision:
-- pseudonymous ids belong in the private table; emails never do). Historical
-- rows keep only tenant_id and resolve through tr.tenant_workspace_map, which
-- is a CLOSED set once this lands: a workspace created after the writer change
-- has every row carrying workspace_id, so the map never needs refreshing.
ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS workspace_id LowCardinality(String) DEFAULT '' AFTER tenant_id;
