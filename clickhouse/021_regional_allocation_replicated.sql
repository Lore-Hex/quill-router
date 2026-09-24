-- Forward-only: old events retain NULL allocation evidence.
ALTER TABLE tr.spend_lease_shadow ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS regional_selected_shard Nullable(UInt16) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS regional_sibling_served Nullable(UInt8) DEFAULT NULL;
