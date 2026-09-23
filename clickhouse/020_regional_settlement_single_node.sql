-- Regional settlement observations share the R1 coverage stream.
-- Filter regional_outcome IN ('settled', 'refunded') for the denominator;
-- countIf(regional_overrun_microdollars > 0) and sum it for frequency/size.
ALTER TABLE spend_lease_shadow
    ADD COLUMN IF NOT EXISTS regional_actual_microdollars Nullable(Int64) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_local_microdollars Nullable(Int64) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_global_microdollars Nullable(Int64) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_overrun_microdollars Nullable(Int64) DEFAULT NULL;
