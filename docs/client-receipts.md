# Inference receipt key discovery

Inference receipt signing keys are generated inside each enclave at boot. The
receipt itself is returned only to the requesting client and is never stored;
the control plane durably stores only public keys and their key-binding
attestations.

Clients resolving a compact receipt's `kid` should fetch:

```text
GET /.well-known/inference-receipt-keys
GET /trust/receipt-keys.json
```

The two routes mirror the same bounded, append-only log. Each key contains its
Ed25519 public JWK, the attestation in wire-format form, the serving plane,
first/last observation times, revocation state, and `verified`.

`verified=true` currently means the GCP Confidential Space JWT signature,
issuer, validity window, audience, and non-debug state were checked against
Google's issuer JWKS, in addition to checking the receipt-key commitment.
AWS Nitro and Azure MAA entries are retained only after their evidence contains
the correct key commitment, but are published as `verified=false` until their
full in-package chain verifiers are implemented. Consumers must not silently
treat `verified=false` as hardware-anchored trust.

The scheduled internal collector resolves every configured gateway endpoint to
its A records, connects to each IP with the gateway hostname as TLS SNI and
`Host`, and reads `/receipt-key`. Cloud Scheduler invokes
`POST /v1/internal/gateway/receipt-keys/collect` with the internal gateway
token; successful passes record the `job:receipt-key-collector` heartbeat. A
malformed `kid`, absent key commitment, or failed GCP chain check is skipped.
Re-observing a key advances only
`last_seen` (and may refresh a key-bound attestation); a `kid` collision never
replaces the stored key.
