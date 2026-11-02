-- AWS/Azure variant of 027. Apply before deploying readers or drain bundles.
ALTER TABLE activity_generations
    ADD COLUMN IF NOT EXISTS requested_model String DEFAULT '' AFTER model;
