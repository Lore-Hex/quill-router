# Encryption And Key Management Policy

Status: draft for management approval.

Owner: Infrastructure Owner.

## Purpose

Protect customer data, secrets, and credentials with encryption and controlled key access.

## Policy

- Production traffic uses TLS.
- Production prompt traffic is designed to terminate inside the attested gateway.
- Cloud data stores use provider-managed or customer-managed encryption at rest.
- BYOK secrets are envelope encrypted before durable storage.
- KMS keys and envelope keys require least-privilege access.
- API keys are salted/hashed and raw values are not stored.
- Payment method secrets are handled by payment processors; TrustedRouter stores references only.
- Secret rotation is performed when compromise is suspected, when personnel changes require it, or on planned rotation cadence.

## BYOK Handling

- Raw BYOK key is accepted only over authenticated TLS.
- Key hint is derived for display.
- Secret is envelope encrypted.
- Gateway authorization releases provider key material only to the attested runtime path.
- Gateway-side key caches must be memory-only, short TTL, and invalidated on rotation/delete.

## Evidence

- KMS inventory.
- Secret Manager inventory.
- BYOK crypto tests.
- API key hashing tests.
- TLS/attestation evidence.
- Rotation records.
