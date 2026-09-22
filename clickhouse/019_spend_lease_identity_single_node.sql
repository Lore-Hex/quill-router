-- Forward-only: lease_id is the router identity; preserve the enclave claim separately.
-- Historical NULLs remain missing evidence; do not backfill from the echo.
ALTER TABLE spend_lease_shadow
    ADD COLUMN IF NOT EXISTS echo_lease_id Nullable(String);
