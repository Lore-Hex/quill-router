-- Standalone (AWS/Azure) variant of 025; apply before shipping the writers/readers.
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS app_id Nullable(String) DEFAULT NULL AFTER app;
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS app_markup_microdollars Int64 DEFAULT 0 AFTER total_cost_microdollars;
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS custom_model_markup_microdollars Int64 DEFAULT 0 AFTER app_markup_microdollars;
