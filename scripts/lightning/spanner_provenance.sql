-- Explicit upgrade for existing native Spanner databases before Lightning
-- activation. Add the wider constraint before removing the old constraint, so
-- the table is never unconstrained. Run this once, outside a rolling deploy.
-- If interrupted after ADD succeeds, inspect INFORMATION_SCHEMA and complete
-- only the DROP. New databases already include Lightning in the original check.
ALTER TABLE tr_trust_event ADD CONSTRAINT tr_trust_event_provider_lightning
  CHECK (provider IN ('stripe','paypal','adyen','x402','lightning','operator','system'));
ALTER TABLE tr_trust_event DROP CONSTRAINT tr_trust_event_provider;
