-- Additive migration. The growth role sees hashes only, never directory IDs/names.
ALTER TABLE tr.workspace_directory ON CLUSTER trustedrouter
    ADD COLUMN IF NOT EXISTS billing_account_fingerprint String DEFAULT '';
CREATE VIEW IF NOT EXISTS tr.growth_billing_owners ON CLUSTER trustedrouter
DEFINER=tr SQL SECURITY DEFINER AS
SELECT lower(hex(SHA256(workspace_id))) AS workspace_fingerprint,
       billing_account_fingerprint, refreshed_at AS billing_owner_observed_at
FROM tr.workspace_directory FINAL
WHERE deleted=0 AND billing_account_fingerprint!='';
GRANT ON CLUSTER trustedrouter SELECT ON tr.growth_billing_owners TO tr_growth_read;
