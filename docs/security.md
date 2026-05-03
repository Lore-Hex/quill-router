# Security Notes

## Prompt And Output Data

TrustedRouter does not store prompt or output content. Metadata rows are
limited to generation ID, workspace, key hash, model, provider, token counts,
cost, usage type, speed, finish reason, and status.

`GET /generation/content` is present for OpenRouter compatibility, but it
returns `content_not_stored`.

## BYOK Secrets

The management API accepts BYOK setup as either a raw `api_key` or a
`secret_ref`. Raw keys are treated as one-time input: the control plane derives
a short first/last display hint and stores only a Secret Manager-style
reference. Stored BYOK config must never contain raw provider keys.

The attested gateway contract returns only the secret reference and key hint
needed for routing. Prompt/output content stays in the gateway path and is not
included in authorize, settle, refund, activity, or generation metadata calls.

## API Keys

API keys are verified with a per-key random salt and SHA-256 digest. The public
`hash` field is an opaque key ID used for management and gateway authorization;
it is not the secret verifier.

## Production Boundary

`api.quillrouter.com` is the attested prompt path. The FastAPI control plane
does not register chat, messages, responses, or embeddings routes in production,
so an outage cannot silently degrade prompt traffic to a non-attested handler.

Production config is fail-closed: startup requires `TR_INTERNAL_GATEWAY_TOKEN`,
`TR_STRIPE_WEBHOOK_SECRET`, `TR_STRIPE_SECRET_KEY`, `TR_SENTRY_DSN`, and a
configured Spanner/Bigtable storage backend.

## Rate Limiting

Requests are rate limited before route handlers run. Local/test uses an
in-memory counter. Production uses the configured store so counters are shared
across Cloud Run instances. The default buckets are per-IP for unauthenticated
requests, per-key for bearer-authenticated requests, and a separate higher
limit for internal gateway calls.

This is an application backstop, not the whole abuse plan. Public signup should
also use Cloud Armor, Stripe/payment risk controls, per-provider quota
isolation, and automated key suspension.

## Sentry

Sentry is control-plane-only. Do not add Sentry to `quill-cloud-proxy/enclave-go`
or any attested workload image. The FastAPI control plane initializes Sentry
with request bodies disabled and scrubbers for auth headers, API keys, BYOK
keys, prompt fields, output fields, cookies, and Stripe raw payloads.

## Cloudflare

`trustedrouter.com` can be Cloudflare proxied. `api.quillrouter.com` must be
DNS-only so TLS reaches the attested Confidential Space workload.

`trust.trustedrouter.com` should point at the control-plane/trust hosting, not
the enclave API IP.
