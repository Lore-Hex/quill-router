-- Forward-only: old events remain readable with NULL regional evidence.
ALTER TABLE tr.spend_lease_shadow ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS authorization_id Nullable(String) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_predicate_reason Nullable(String) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_predicate_mask Nullable(UInt32) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_outcome Nullable(String) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_unavailable_reason Nullable(String) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_requested_region Nullable(String) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_resolved_region Nullable(String) DEFAULT NULL;
