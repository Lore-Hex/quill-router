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

The two routes mirror the same view. Without a query parameter they return every
observed attestation version ordered by immutable `(kid, att_sha256)`, in pages
of at most 250 versions and at most 1 MiB. Follow `next_cursor` by passing it as
`?cursor=<next_cursor>` until it is null. Each row is identified by
`(kid, att_sha256)` and contains its
Ed25519 public JWK, the attestation in wire-format form, the serving plane,
first/last observation times, revocation state, and `verified`.

Pass `?kid=<kid>` to fetch every retained attestation version for exactly one
signing key. The same byte and page ceilings apply; follow its `next_cursor` as
`?kid=<kid>&cursor=<next_cursor>` until it is null.

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
Re-observing the same document advances only `last_seen`; a newly re-minted
document for the same `kid` appends another row, and a `kid` collision never
replaces stored key material.

## Storage migration

Legacy receipt-key rows have no `att_sha256`. Readers compute the b64url-unpadded
SHA-256 from the raw decoded attestation document, and the next observation
rewrites that legacy row under its `(kid, att_sha256)` identity. The additive
database migration must keep `att_sha256` nullable while those rows remain and
add a non-unique index on `(kid, att_sha256)`. For Postgres-family deployments,
apply the equivalent nullable column and index in the deployment-owned schema.
GCP uses `scripts/deploy/migrate_receipt_key_versions.sh`. Apply that DDL before
starting this router revision: its receipt-key writer names the physical `kid`
and `att_sha256` columns, so collection fails until they exist. The migration is
safe to re-run, and readers remain compatible with legacy rows whose projections
are null.

## 2026-09-10 version-retention defect and rollout order

Production exposed why `kid` alone cannot identify a log row. Two receipts from
one enclave run pinned different documents: receipt `iat=1789054681` pinned
`ooBW14zh…`, while the only document retained for that `kid` had
`iat=1789054983` and hash `am4rIk6A…`. The old kid-keyed upsert had overwritten
the first document, so the older compact receipt no longer had public evidence
available “forever.”

Land and deploy this router change before enabling enclave `att_history`
responses. The router accepts both legacy bodies without `att_history` and new
bodies with it; that ordering prevents a new enclave from serving history that
an older collector would silently ignore.
