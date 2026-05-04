# TrustedRouter

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
- BYOK raw keys are one-time input only; public/control-plane responses expose
  a short first/last key hint and encrypted reference metadata, never plaintext.
- Production config fails closed without an internal gateway token, signed
  Stripe webhook secret, and a non-memory storage backend.
- Production control-plane apps do not register `/chat/completions`,
  `/messages`, `/responses`, or `/embeddings`; those belong on the attested API
  plane.
- Sentry is control-plane-only and scrubs request bodies, auth headers, API
  keys, BYOK keys, prompt messages, and output text.
- No Sentry configuration belongs in the attested enclave.

## Public Positioning

- Pricing target: prepaid routes are priced at `$0.01` less per 1 million
  tokens than the published provider price, tracked as integer microdollars per
  million tokens so the ledger can represent one-cent-per-million discounts
  without floating point math.
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

- Run independent attested gateway pools in at least `us-central1`,
  `europe-west4`, `us-east4`, and one Asia region. `europe-west4` is the first
  EU region in the default control-plane routing metadata.
- Keep TLS private keys inside each regional Confidential Space workload.
- Move ACME from TLS-ALPN-01 to DNS-01 or another challenge flow that works
  with multiple regional endpoints for the same hostname. The current
  TLS-ALPN-01 flow is fine for one region, but a global DNS record can route
  challenges to the wrong replica.
- Use regional hostnames such as `us.api.quillrouter.com` and
  `eu.api.quillrouter.com` for deterministic attestation and debugging.
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
