-- Schema first, then drain bundles, then control-plane writers/readers.
-- No historical alias can be inferred from a selected model: empty is unknown.
ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS requested_model String DEFAULT '' AFTER model;
