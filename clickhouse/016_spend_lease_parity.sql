-- Forward-only P1/P2 evidence. NULL distinguishes pre-migration/missing evidence.
ALTER TABLE tr.spend_lease_shadow ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS binding_outcome Nullable(String),
    ADD COLUMN IF NOT EXISTS frozen_server_estimate_micro Nullable(Int64),
    ADD COLUMN IF NOT EXISTS comparison_catalog_version Nullable(String),
    ADD COLUMN IF NOT EXISTS applicability_drift Nullable(String);
