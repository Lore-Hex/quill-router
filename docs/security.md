# Security Notes

## OAuth and exchange rate limits

Registered apps use `/v1/oauth/authorize` and `/v1/oauth/token`. Redirect URIs
are exact-matched before errors may redirect. PKCE S256 is mandatory and the
verifier is 43–128 RFC 7636 unreserved characters. Authorization codes and
consent records are short-lived and single-use.

Consent is a server-side JSON entity. Approval posts only its opaque id, a
constant-time-compared CSRF token, and the visible budget choice; client,
callback, scopes, state, and PKCE values come from the record. Stripe URLs
carry only the consent id and checkout result—never callback or PKCE material.

`/v1/oauth/token` and legacy `/v1/auth/keys` share a dedicated process-local
per-IP 30/minute bucket. A limited request returns 429 with `Retry-After`.
Conformant token errors use RFC 6749 envelopes and no-store headers; the legacy
endpoint preserves its historical envelope and statuses.

The four OAuth hand-off gaps are closed: scoped balance reads, conformant
OAuth 2.1/PKCE, server-bound explicit consent, and end-to-end app attribution.

## Prompt And Output Data

TrustedRouter never logs prompt or output content. Ordinary synchronous and
streaming inference does not retain it. Metadata rows are limited to generation
ID, workspace, key hash, model, provider, token counts, cost, usage type, speed,
finish reason, and status.

The opt-in Batch API is a separate retention mode. It temporarily stores
per-artifact AES-256-GCM ciphertext so work can survive restarts and results can
be polled. Per-artifact data keys are wrapped with Cloud KMS and artifacts are
deleted after 30 days. This boundary depends on the deployed GCP KMS and IAM
policy and is not the same zero-retention property as ordinary inference. See
`/docs/batch` and `quill-cloud-proxy/docs/design/batch-api.md`.

`GET /generation/content` is present for OpenRouter compatibility, but it
returns `content_not_stored`.

## BYOK Secrets

The management API accepts BYOK setup as either a raw `api_key` or a
`secret_ref`. Raw keys are treated as one-time input: the control plane derives
a short first/last display hint, creates a random data-encryption key (DEK),
encrypts the provider key with AES-256-GCM, wraps the DEK with the configured
BYOK envelope key, and stores only ciphertext + non-secret metadata. In
production the DEK wrap is a Cloud KMS Encrypt call; local/test can use an
in-process wrapper for deterministic CI. Stored BYOK config must never contain
raw provider keys.

At production scale BYOK keys are normal encrypted database rows, not one GCP
Secret Manager secret per customer key. The deploy infra provisions a single
KMS crypto key for the envelope wrap, so Secret Manager is not the per-user BYOK
object store.

The attested gateway contract returns the encrypted BYOK envelope, secret
reference, key hint, and a non-secret `byok_cache_key` needed for routing.
Prompt/output content stays in the gateway path and is not included in
authorize, settle, refund, activity, or generation metadata calls.

Gateways should decrypt BYOK envelopes only inside the attested runtime and
keep the resulting provider key in short-lived process memory. The cache key is
derived from the workspace, provider, and encrypted envelope bytes; rotating a
BYOK key produces a different cache key and forces a new KMS unwrap. Deleting a
BYOK key stops returning an envelope in `/internal/gateway/authorize`, so stale
cache entries are no longer reachable and expire by TTL.

Operators can prove the full production contract with the explicit,
destructive smoke:

```bash
set -a
source ~/.quill_cloud_keys.private
set +a
TR_BYOK_PROD_SMOKE=1 uv run python scripts/smoke_byok_prod.py
unset CEREBRAS_API_KEY TR_BYOK_PROD_SMOKE
```

The smoke creates an isolated workspace, uploads a Cerebras key, verifies two
provider-forced attested calls across an envelope rotation, checks BYOK
settlement and an unchanged prepaid balance, deletes the provider credential,
and proves subsequent BYOK authorization fails closed. It never prints the
provider key. The BYOK row and inference key are deleted and the disposable
workspace is soft-deleted in a `finally` block. Run it manually for security
releases, not on every deploy, because it creates durable signup audit metadata.

## API Keys

API keys are verified with a per-key random salt and SHA-256 digest. The public
`hash` field is an opaque key ID used for management and gateway authorization;
it is not the secret verifier.

## Production Boundary

`api.trustedrouter.com` is the attested prompt path. The FastAPI control plane
does not register chat, messages, responses, or embeddings routes in production,
so an outage cannot silently degrade prompt traffic to a non-attested handler.

Production config is fail-closed: startup requires `TR_INTERNAL_GATEWAY_TOKEN`,
`TR_STRIPE_WEBHOOK_SECRET`, `TR_STRIPE_SECRET_KEY`, `TR_SENTRY_DSN`, and a
configured Spanner/Bigtable storage backend.

## Rate Limiting

Deployed services default to `TR_RATE_LIMIT_CLIENT_IP_MODE=untrusted`: every
request shares one conservative `untrusted_lb` bucket, and even a syntactically
valid caller-supplied `X-TrustedRouter-Client-IP` is ignored. A service may set
the mode to `edge_header` only after its front door has been verified to
overwrite that header with one normalized source address and direct origin
access has been closed. This is the GCP external Application Load Balancer
contract. AWS App Runner and direct Azure Container Apps cannot provide the
same overwrite and must remain in `untrusted` mode unless their topology is
changed.

The application always ignores `X-Forwarded-For`, `CF-Connecting-IP`,
`X-TrustedRouter-User`, and Host when deriving limiter identity. In
`edge_header` mode, a missing, duplicated, or malformed trusted header also
collapses into `untrusted_lb`; it never falls back to caller-controlled headers
or the deployed socket peer. Only explicit `local` and `test` environments use
the direct ASGI peer, so canary and staging retain deployed trust semantics.

All HTTP limiter counters are bounded and process-local so request admission
cannot create a Spanner read-modify-write hotspot. The ingress bucket is keyed
only by trusted source and uses the IP allowance even when a caller supplies a
Bearer value or cookie. Only a correctly matched internal gateway secret earns
the higher internal allowance before route authentication. The dedicated
federation directory, settlement, and credit secrets earn that allowance only
on their exact inbound routes; they are never treated as generic internal
credentials. After ordinary API key or session authentication succeeds, a
second credential bucket is applied exactly once for that request. Invalid or
arbitrary Bearer values therefore neither mint credential buckets nor make
public routes perform authentication.

These counters are defense-in-depth, not a fleet-wide quota: each application
instance has its own allowance. Fleet-wide coarse source limiting and
volumetric protection are supplied by the front door (Cloud Armor or the cloud
equivalent). This split keeps the limiter off storage and billing hot paths
while preserving a bounded local backstop on every instance.

The ASGI boundary also enforces four independent request-body controls:
`TR_MAX_REQUEST_BODY_BYTES` (4 MiB by default),
`TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES` (64 MiB),
`TR_MAX_CONCURRENT_REQUEST_BODIES` (16), and
`TR_REQUEST_BODY_READ_TIMEOUT_SECONDS` (30 seconds). Deployments set smaller
limits for public/forms services and a deliberate 32 MiB per-request limit for
the authenticated browser chat and internal multimodal surfaces.

Oversized declared bodies are rejected before route execution; streamed bodies
are counted incrementally and an overflowing chunk is never handed to
`Request.body()` or `Request.json()`. Declared bytes reserve the shared process
budget before route work, and undeclared bytes reserve it as they arrive.
Duplicate, malformed, negative, or Content-Length-plus-Transfer-Encoding
framing is rejected as a bad request. The middleware neither prebuffers request
bodies nor buffers streaming responses. Slow reads have one total deadline.
Admission failures are retryable and body/framing rejections close the HTTP/1
connection; an outer close-only guard does the same when an early auth,
rate-limit, safe-method, or 404 response leaves a possible body unread. This
preserves authentication/error ordering for legacy clients that attach a body
to a GET while preventing their unread bytes from retaining a backend socket.

This is an application backstop, not the whole abuse plan. Public signup should
also use Cloud Armor, Stripe/payment risk controls, per-provider quota
isolation, and automated key suspension.

## Sentry

Sentry is control-plane-only. Do not add Sentry to `quill-cloud-proxy/enclave-go`
or any attested workload image. The FastAPI control plane initializes Sentry
with request bodies disabled and scrubbers for auth headers, API keys, BYOK
keys, prompt fields, output fields, cookies, and Stripe raw payloads.

Sentry also has a client-side flood gate to bound repetitive reporting. By
default each Cloud Run process sends at most 3 events per fingerprint and 50
total error events per hour
(`TR_SENTRY_FLOODGATE_MAX_EVENTS_PER_FINGERPRINT`,
`TR_SENTRY_FLOODGATE_MAX_EVENTS_PER_WINDOW`,
`TR_SENTRY_FLOODGATE_WINDOW_SECONDS`). Logs use a separate bounded gate so they
cannot consume exception capacity. Structured router HTTP errors are grouped
by route and error details, excluding per-request timing and request IDs.
Distinct errors are admitted while the process budget remains; once that
budget is exhausted, even a new fingerprint is dropped until the next window.

These limits multiply across processes and reset on restarts. They are not a
fleet-wide monthly allowance and cannot guarantee staying within Sentry's
included volume. Monitor Sentry usage and retain an explicit project/PAYG cap.
Resolving or deleting an issue does not restore already consumed capacity.
To temporarily drop all new Sentry events during
an incident, set
`TR_SENTRY_FLOODGATE_MAX_EVENTS_PER_WINDOW=0` and redeploy.

## Cloudflare

`trustedrouter.com` can be Cloudflare proxied. `api.trustedrouter.com` must be
DNS-only so TLS reaches the attested Confidential Space workload.

`trust.trustedrouter.com` is hosted on **GitHub Pages**, independently of the
control plane and the enclave API. Its custom domain is declared in
[`trust-page/CNAME`](https://github.com/Lore-Hex/quill-cloud-proxy/blob/main/trust-page/CNAME)
and deployed by
[`publish-trust-page.yml`](https://github.com/Lore-Hex/quill-cloud-proxy/blob/main/.github/workflows/publish-trust-page.yml).
It must not point at the enclave API IP or the control-plane service.
`trust.quill.lorehex.co` is the separate S3/CloudFront mirror, published by
`publish-trust-s3.yml` after Pages. The control-plane `/trust` page links to this
verification material; it is not the hosting origin for the trust subdomain.

The cloud-specific signing workflows produce the signed release bundles.
Pages and the mirror publish those bundles unchanged; they do not sign them.
Verify signatures and live attestation against the published measurements,
not just the existence of an HTTPS page.

For GCP, compare the verified live `submods.container.image_digest` with
`accepted_image_digests` in the signed release record. The scalar `image_digest`
and `source_commit` describe the target release, not every serving region during
a rolling deployment. The deployment publishes the approved transition set
before switching regions and narrows it only after the rollout succeeds.
Reject an empty or malformed set and any live digest outside it. Keep the
Google signature, issuer, audience, fresh nonce, TLS-session binding, and
production debug-state checks; accepting a published transition set does not
replace those checks. A locally pinned single-image policy can intentionally
block during a rollout, but that is different from an unpublished image.

## Legacy delegated-key PKCE

Both `/v1/auth/keys/code` and the legacy `/auth` consent flow require a PKCE
challenge. Only `S256` is accepted; omitting the method with a challenge defaults
to `S256`. `/v1/auth/keys` rejects plain, missing, and unknown methods on stored
authorization codes as well. Old insecure codes must be replaced by a fresh
authorization. The legacy JSON response envelope is unchanged. New integrations
should use the registered OAuth 2.1 flow.

## Enclave telemetry checks

CI explicitly checks out `quill-cloud-proxy` and records its revision. The
Sentry guard fails on missing source and checks Go source, Dockerfiles, and Go
dependency manifests, including the attestation sidecar. Local full-suite runs
need a sibling checkout or `TR_ENCLAVE_SOURCE_ROOT` pointing to `enclave-go`.

This Sentry source/dependency check is not a whole-image SBOM audit and must
not be described as proof that every third-party runtime binary is free of
every telemetry SDK.
