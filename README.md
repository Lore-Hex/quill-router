# TrustedRouter

[![CI](https://github.com/Lore-Hex/quill-router/actions/workflows/ci.yml/badge.svg)](https://github.com/Lore-Hex/quill-router/actions/workflows/ci.yml)
[![Deploy](https://github.com/Lore-Hex/quill-router/actions/workflows/deploy.yml/badge.svg)](https://github.com/Lore-Hex/quill-router/actions/workflows/deploy.yml)
[![Prod smoke](https://github.com/Lore-Hex/quill-router/actions/workflows/prod-smoke.yml/badge.svg)](https://github.com/Lore-Hex/quill-router/actions/workflows/prod-smoke.yml)
[![Status](https://img.shields.io/website?url=https%3A%2F%2Fstatus.trustedrouter.com&label=status)](https://status.trustedrouter.com)
[![Verifiable trust](https://img.shields.io/website?url=https%3A%2F%2Ftrust.trustedrouter.com&label=trust)](https://trust.trustedrouter.com)
[![JavaScript SDK](https://img.shields.io/npm/v/@lore-hex/trusted-router?label=JS%20SDK&logo=npm)](https://www.npmjs.com/package/@lore-hex/trusted-router)
[![Python SDK](https://img.shields.io/pypi/v/trusted-router-py?label=Python%20SDK&logo=pypi)](https://pypi.org/project/trusted-router-py/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

TrustedRouter is an OpenRouter-compatible production LLM router with an
attested API plane and a regular SaaS control plane.

- Product: `trustedrouter.com`
- API base: `https://api.quillrouter.com/v1`
- Trust: `trust.trustedrouter.com`
- Quill app: `https://quill.lorehex.co`
- Source: `https://github.com/Lore-Hex/quill-router`

This repo intentionally implements the control-plane contract first: route
coverage, auth/key management, billing ledger semantics, usage metadata, no
prompt/output storage, Sentry scrubbers, and provider abstractions. The attested
gateway implementation lives in `quill-cloud-proxy`.

Trust boundary: `api.quillrouter.com` is the attested prompt path and must
terminate TLS inside Confidential Space. `trustedrouter.com` is the control
plane and must never serve a production inference fallback.

## Local

```bash
uv sync
uv run pytest
uv run uvicorn trusted_router.main:app --reload
```

End-to-end smoke against a running instance:

```bash
TR_SMOKE_BASE_URL=http://127.0.0.1:18080/v1 uv run python scripts/smoke_e2e.py
```

For production, set `TR_SMOKE_BASE_URL=https://api.quillrouter.com/v1` and
`TR_SMOKE_INTERNAL_TOKEN` if the internal gateway routes are token-protected.

Set local operator/provider keys in:

```text
/Users/jperla/claude/.quill_cloud_keys.private
```

That file is never committed. It is expected to be dotenv-style:

```text
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
CEREBRAS_API_KEY=...
DEEPSEEK_API_KEY=...
MISTRAL_API_KEY=...
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
SENTRY_DSN=...
```

The deploy script also accepts local aliases already used in some operator
files: `CLAUDE_API_KEY` for Anthropic, `CHATGPT_API_KEY` for OpenAI, and
`STRIPE_KEY` for `STRIPE_SECRET_KEY`.

Vertex is different from the other provider platforms: production GCP deploys
use the Cloud Run or Confidential Space service account and short-lived Google
access tokens from metadata/ADC. Do not put a long-lived Vertex key in this
file for the first-party prepaid Vertex route; grant the runtime service
account Vertex permissions instead.

## License

Apache License 2.0. This is the right default for TrustedRouter because it is
commercially permissive, familiar to infrastructure buyers, and includes an
explicit patent grant.

## Security Defaults

- Prompt and output content are never stored.
- Usage logs contain metadata only.
- API keys are stored as salted SHA-256 hashes with opaque key IDs.
- User-submitted BYOK provider keys are stored as envelope-encrypted ciphertext
  rows, not one Secret Manager object per key. In production, Cloud KMS wraps
  the per-key DEK; external `env://...` references remain supported for
  operator-managed keys.
- Gateway authorizations include a non-secret `byok_cache_key` for encrypted
  BYOK envelopes. Attested gateways use it for short TTL, memory-only decrypted
  key caching; BYOK rotation changes the key and delete stops returning the
  envelope.
- BYOK raw keys are one-time input only; public/control-plane responses expose
  a short first/last key hint and encrypted reference metadata, never plaintext.
- Production config fails closed without an internal gateway token, signed
  Stripe webhook secret, and a non-memory storage backend.
- Production control-plane apps do not register `/chat/completions`,
  `/messages`, `/responses`, or `/embeddings`; those belong on the attested API
  plane.
- Sentry is control-plane-only and scrubs request bodies, auth headers, API
  keys, BYOK keys, prompt messages, and output text. A client-side Sentry flood
  gate caps repeated issues per fingerprint and total events per process/window
  so a single noisy integration cannot consume the whole error budget again.
- No Sentry configuration belongs in the attested enclave.

## Broadcast Observability

Workspace owners can configure Broadcast destinations at
`/v1/broadcast/destinations` or in the console under Broadcast. Supported
destinations are PostHog and OTLP JSON webhooks. Broadcast is metadata-only by
default: model, provider, token counts, latency, cost, route type, region, and
custom trace metadata. Prompt/output content is exported only when a destination
explicitly enables `include_content`; those content-enabled encrypted
destinations are returned only to the attested gateway, not normal management
responses. Metadata-only deliveries are written to a persistent Broadcast
outbox first and drained asynchronously by `/internal/broadcast/drain`, so a
PostHog/webhook outage does not block inference or lose already-settled
metadata on process restart.

## Synthetic Monitoring

TrustedRouter has a separate synthetic monitoring plane for public uptime.
Synthetic workers run outside the enclave, send tiny real requests into the
public attested API, and store only metadata. The monitor model aliases are:

- `trustedrouter/free`: OpenRouter-style free pool. Useful for users, not an
  SLA signal.
- `trustedrouter/cheap`: cheapest paid pool with provider diversity.
- `trustedrouter/monitor`: internal uptime pool for PONG and fallback checks.
  It is visible in the catalog for transparency, but authorization requires
  the configured `TR_SYNTHETIC_MONITOR_API_KEY`; normal API keys receive 403.

Workers should run from `us-central1` and `europe-west4`, using a dedicated
`trustedrouter-synthetic-monitoring` workspace/key with hard spend caps and
auto-refill. Raw samples are append-only Bigtable rows; public status pages
read compact rollups exposed at `/status`, `/status.json`, and
`/status/history?window=5m|24h|daily`. Synthetic generations use the
`TrustedRouter Synthetic` app label and are excluded from public provider
benchmark/ranking samples so uptime probes do not pollute customer analytics.

Status separates three SLO classes instead of blending them:

- `router_core`: attested API reachable, key authorization works, route
  candidates/fallback are available, and settle/refund is durable.
- `provider_effective`: a full model response succeeds after provider fallback.
- `control_plane`: dashboard, billing UI, keys, credits, docs, trust, and
  status surfaces.

Deploy watchdogs and internal burn-rate alerts default to `router_core`.
Provider-only failures should degrade `provider_effective` without consuming
the router-core error budget when fallback remains available.

## Public Positioning

- Pricing: prepaid and BYOK usage is tracked as integer microdollars, not
  floating point dollars, so tiny token costs remain auditable in the ledger.
- Uptime target: `trustedrouter/auto` is a real chat model alias in local/test
  control-plane inference and rolls to the next configured provider on upstream
  provider failures. Chat requests also honor OpenRouter-style `models` and
  `provider` routing filters (`order`, `only`, `ignore`, `allow_fallbacks`,
  `data_collection`, and `sort`) so clients can request explicit fallback
  chains or provider preferences.
- Billing: prepaid credits and BYOK first; no subscription is required.
- Trust: hosted open source, with the running API's source commit, image
  reference, image digest, and attestation policy published at
  `trust.trustedrouter.com`.
- Signup: email signup creates a one-time management key for the workspace.
- Wallet/crypto: stablecoin checkout is wired through Stripe Checkout's Crypto
  payment method when requested. Card/default Checkout remains the default path.

## Scale Target

The goal is to support OpenRouter-class scale:

- 1 trillion tokens/day, or about 11.6 million tokens/second averaged over a
  day.
- 1-4 million developer accounts.
- 300+ actively routable models.
- 60+ providers.
- Global routing overhead competitive with edge-deployed routers.

The current production deployment does **not** meet that target yet. It runs
the control plane in 10 GCP regions behind a global LB with per-region
Serverless NEGs, and a single live prompt gateway until additional attested
regional pools are deployed. Capacity scales horizontally as more attested
pools come online; correctness, trust, billing, and SDK compatibility are
in steady-state.

Request volume depends heavily on average generation size. At 1 trillion
tokens/day:

| Average tokens/request | Requests/day | Average request rate |
| ---: | ---: | ---: |
| 1,000 | 1.0B | 11.6k rps |
| 2,500 | 400M | 4.6k rps |
| 10,000 | 100M | 1.2k rps |

The architecture can be evolved to this scale, but only if the hot path avoids
per-request global bottlenecks. That means regional stateless gateway fleets,
regional provider pools, sharded quota leases, append-only metadata writes, and
asynchronous aggregation.

## Current Latency

Measured from this development machine to the centralized GCP `us-central1`
attested API on May 2, 2026:

| Probe | p50 | p95 | Notes |
| --- | ---: | ---: | --- |
| Unauthenticated `/v1/chat/completions` rejection | 174 ms | 184 ms | Includes DNS, TCP, public TLS, enclave request handling. |
| TCP connect | 55 ms | 59 ms | Network path to `us-central1` from this machine. |
| TLS handshake complete | 112 ms | 124 ms | Public ACME cert terminates inside the enclave. |
| `/attestation` | 1.06 s | 1.12 s | Includes GCP attestation token generation, so not representative of normal routing overhead. |

The centralized network overhead is much higher than OpenRouter's reported
edge overhead, but model latency usually dominates interactive requests. The
first production scaling step should be multi-region rather than building a
custom global edge immediately.

## Horizontal Scale Shape

The production path is designed to scale by keeping the prompt gateway
stateless:

- `api.quillrouter.com` instances can be replicated behind TCP passthrough.
  They authorize, reserve, and settle through the control plane, but prompt
  bytes never leave the attested path.
- Spanner stores strongly consistent control-plane and billing state: users,
  workspaces, keys, BYOK metadata, Stripe event idempotency, reservations, and
  spend limits.
- Bigtable stores high-volume generation metadata rows keyed by workspace/date.
  Prompt and output content are still not stored.
- API-key verification uses a high-entropy lookup hash for point reads; it does
  not scan keys.
- Rate limits are enforced before route handlers and use the configured store,
  so production counters are shared across Cloud Run instances.

At OpenRouter-scale traffic, the next bottleneck is not the enclave binary; it
is the synchronous billing/authorization path. The architecture needs sharded
reservations, regional Bigtable clusters, Cloud Armor edge limits, and multiple
gateway replicas before public traffic is allowed to ramp.

## Multi-Region Plan

Multi-region is feasible while preserving the trust boundary, but it has to be
done carefully:

- Run independent warm attested gateway pools in at least `us-central1`,
  `us-east4`, and `europe-west4`. Add one exercised non-GCP pool next
  (initially AWS Nitro with a tiny real-traffic trickle), then Asia once the
  first three regions are boring.
- Keep TLS private keys inside each regional Confidential Space workload.
- Move ACME from TLS-ALPN-01 to DNS-01 or another challenge flow that works
  with multiple regional endpoints for the same hostname. The current
  TLS-ALPN-01 flow is fine for one region, but a global DNS record can route
  challenges to the wrong replica.
- Keep regional hostnames such as `api-us-central1.quillrouter.com`,
  `api-us-east4.quillrouter.com`, `api-europe-west4.quillrouter.com`, and the
  future `api-aws-us-west-2.quillrouter.com` for deterministic attestation,
  smoke tests, and SDK failover.
- Put `api.quillrouter.com` behind latency/geo DNS or TCP passthrough that does
  not terminate TLS. Cloudflare orange-cloud proxying remains incompatible
  with the prompt-path trust claim.
- Authorize through regional quota leases, not a synchronous global Spanner
  transaction for every request.
- Write generation metadata to regional Bigtable clusters, then aggregate into
  global activity views asynchronously.
- Keep provider routing regional, with provider-specific circuit breakers,
  fallback policy, and per-provider rate limits.

The key design rule: a regional outage can fail closed or route to another
attested region, but it must never silently degrade to a non-attested prompt
handler.

## Router-Core 5 9s Roadmap

The first target is an internal SLO, not a public contractual SLA. 99.999%
allows about 5 minutes 15 seconds of downtime per year, so the public product
copy should stay at 99.9% until at least 30-60 days of measured 99.99%+
router-core uptime exists.

Router-core availability means:

- attested TLS is reachable;
- API-key validation and gateway authorization work;
- route candidates are returned and fallback can choose a healthy provider;
- settlement/refund is durable or safely repairable;
- no prompt request ever falls back to a non-attested path.

The code paths that support this roadmap today are:

- `/status.json` exports `slo_classes.router_core`,
  `slo_classes.provider_effective`, `slo_classes.control_plane`, and burn-rate
  alerts for 5m, 1h, 6h, and 24h windows.
- The deploy watchdog reads `router_core` by default, so provider-only outages
  do not automatically roll back a control-plane deploy.
- SDKs are expected to retry connection failures and 502/503/504 across
  regional attested endpoints before surfacing failure.
- Bigtable activity writes are repairable from Spanner generation records via
  `/v1/internal/reconcile/generation-activity`.

Before making a public 5 9s claim, require three warm GCP attested regions, one
exercised non-GCP failover pool, tested paging, router-core chaos tests, staged
regional deploys with rollback gates, and 30 days of measured router-core
uptime at or above 99.99%.

## Internal Gateway Contract

The attested API plane can reserve and settle usage without sending prompt or
output content to the control plane:

- `POST /v1/internal/gateway/authorize`: validates the API key hash, reserves
  credits/key limits, and returns provider/BYOK routing metadata, route
  candidates derived from `model`, `models`, and `provider` request filters,
  and configured regional endpoints.
- `POST /v1/internal/gateway/settle`: settles successful usage and appends
  metadata-only activity rows.
- `POST /v1/internal/gateway/refund`: releases reservations after provider
  failures or client disconnects.

Set `TR_INTERNAL_GATEWAY_TOKEN` outside local development.

## Production Storage

Production uses:

```text
TR_STORAGE_BACKEND=spanner-bigtable
TR_SPANNER_INSTANCE_ID=trusted-router
TR_SPANNER_DATABASE_ID=trusted-router
TR_BIGTABLE_INSTANCE_ID=trusted-router-logs
TR_BIGTABLE_GENERATION_TABLE=trustedrouter-generations
```

`scripts/deploy-gcp.sh` enables the APIs, creates the Spanner table
`tr_entities`, creates the Bigtable generation table, deploys Cloud Run, and
wires the current GCP trust metadata into the trust page.

## Billing

`POST /v1/billing/checkout` creates a Stripe Checkout session when
`TR_STRIPE_SECRET_KEY` is configured and otherwise returns a deterministic local
mock response. Stripe webhooks credit workspaces idempotently using the
workspace ID in Checkout metadata. `POST /v1/billing/portal` follows the same
Stripe-or-mock pattern for billing management.

For stablecoin checkout, send `{"payment_method":"stablecoin"}`. When
`TR_STABLECOIN_CHECKOUT_ENABLED=true`, the Checkout session is created with
Stripe's `crypto` payment method and still credits the workspace from the signed
`checkout.session.completed` webhook.
