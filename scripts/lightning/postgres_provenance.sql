-- Explicit migration for existing Postgres deployments; run before enabling
-- Lightning. This only widens the provider enum, never modifies money rows.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE tr_trust_event DROP CONSTRAINT tr_trust_event_provider_check;
ALTER TABLE tr_trust_event ADD CONSTRAINT tr_trust_event_provider_check
    CHECK (provider IN ('stripe', 'paypal', 'adyen', 'x402', 'lightning', 'operator', 'system')) NOT VALID;
ALTER TABLE tr_trust_event VALIDATE CONSTRAINT tr_trust_event_provider_check;
COMMIT;
