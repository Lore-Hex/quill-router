-- Preserve throughput measurement accounting independently from billed
-- completion tokens. Safe to apply repeatedly and safe after a fresh install
-- whose 001 schema already includes these columns.
ALTER TABLE provider_benchmark_samples
    ADD COLUMN IF NOT EXISTS visible_output_tokens UInt32 DEFAULT 0
    AFTER output_tokens;

ALTER TABLE provider_benchmark_samples
    ADD COLUMN IF NOT EXISTS reasoning_tokens UInt32 DEFAULT 0
    AFTER visible_output_tokens;

ALTER TABLE provider_benchmark_samples
    ADD COLUMN IF NOT EXISTS requested_output_tokens UInt32 DEFAULT 0
    AFTER reasoning_tokens;

ALTER TABLE provider_benchmark_samples
    ADD COLUMN IF NOT EXISTS synthetic_slot Nullable(UInt64)
    AFTER requested_output_tokens;
