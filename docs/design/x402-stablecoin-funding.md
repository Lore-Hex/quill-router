# TrustedRouter x402 Stablecoin Funding Design

Status: draft for review
Owner: Lore Hex Corp
Last updated: 2026-06-22
Review status: reviewed with `claude --print` on 2026-06-22; P0/P1 feedback incorporated.

## Summary

TrustedRouter should support x402 stablecoin payments as a control-plane funding rail for existing workspaces. x402 should not run inside the attested enclave and should not change the prompt path. Agents still use a normal TrustedRouter API key. When credits are low, the agent can call a separate billing API to receive a Stripe x402 payment challenge, pay in USDC, and have the workspace credited after Stripe verifies settlement.

The attested gateway remains unchanged: it receives the API key, asks the control plane for metadata-only authorization, serves the prompt only when credits are available, and returns insufficient-credit errors when the ledger cannot reserve funds.

## Goals

- Let agents automatically add prepaid credits using stablecoin.
- Require a TrustedRouter API key for all x402 flows.
- Keep the existing integer credit ledger as the source of truth.
- Keep all x402 logic in the control plane, outside the enclave.
- Keep prompt and output content out of Stripe, x402 payment metadata, Sentry, logs, and billing records.
- Make all settlement paths idempotent.
- Credit only from Stripe-confirmed settled amounts, never from client input.

## Non-Goals

- No accountless anonymous paid inference in v1.
- No per-token on-chain refund path in v1.
- No x402 code inside the enclave.
- No prompt-path fallback to a non-attested control-plane route.
- No Coinbase facilitator in v1. Stripe x402 is first.
- No sub-cent x402 top-ups in v1, because Stripe PaymentIntent amounts are cent-denominated.

## Research Notes

- x402 uses HTTP `402 Payment Required` with machine-readable payment requirements so a client or agent can pay and retry. Coinbase describes this as a buyer requesting a resource, the server returning `PAYMENT-REQUIRED`, then the buyer sending payment authorization via `PAYMENT-SIGNATURE`.
- Stripe's x402 docs describe a `payment-required` response header containing a base64-encoded payment requirements payload. Their quickstart creates a crypto `PaymentIntent` in deposit mode and extracts a USDC deposit address from `next_action.crypto_display_details.deposit_addresses`.
- Stripe Machine Payments currently documents Base USDC support for x402. Stripe settlement should land in the Stripe account/balance according to account eligibility, so TrustedRouter should not require a special stablecoin bank account for the Stripe-first implementation.
- Coinbase's facilitator model remains useful later if TrustedRouter wants Coinbase Business settlement, direct USDC treasury, or a non-Stripe route.

References:

- https://docs.stripe.com/payments/machine/x402
- https://docs.stripe.com/payments/machine/x402/quickstart
- https://docs.cdp.coinbase.com/x402/welcome
- https://docs.cdp.coinbase.com/x402/core-concepts/facilitator
- https://www.x402.org/

## Public API

### `POST /v1/billing/x402/fund`

Authenticated by `Authorization: Bearer sk-tr-...`.

Request:

```json
{
  "amount": "10.00"
}
```

Behavior:

- Requires an inference or management API key.
- Resolves the key's workspace.
- Rejects request bodies containing `workspace_id`; x402 funding always derives workspace from the API key.
- Rejects amounts below the minimum, above `TR_X402_MAX_FUND_DOLLARS`, or not exactly representable in cents.
- Applies per-key and per-workspace rate limits before calling Stripe.
- Creates a Stripe crypto `PaymentIntent` in deposit mode.
- Stamps metadata:
  - `workspace_id`
  - `amount_microdollars`
  - `payment_method=x402`
  - `purpose=trustedrouter_credits`
  - `asset=USDC`
  - `network=base`
- Returns HTTP `402 Payment Required`.
- Includes `payment-required` response header with a base64 x402 payload.
- Includes a JSON body for debugging and non-x402 clients.

Response shape:

```json
{
  "error": {
    "code": 402,
    "message": "Stablecoin payment required to add TrustedRouter credits",
    "type": "insufficient_credits"
  },
  "data": {
    "payment_protocol": "x402",
    "provider": "stripe",
    "payment_intent_id": "pi_...",
    "network": "eip155:8453",
    "asset": "USDC",
    "amount_decimal": "10.00",
    "amount_microdollars": 10000000,
    "payment_required_header": "base64..."
  }
}
```

### `POST /v1/billing/x402/settle`

Authenticated by `Authorization: Bearer sk-tr-...`.

Request:

```json
{
  "payment_intent_id": "pi_..."
}
```

Behavior:

- Retrieves the Stripe `PaymentIntent`.
- Requires `metadata.payment_method=x402`.
- Requires the PaymentIntent workspace to match the API key workspace.
- Requires `currency=usd`.
- Requires metadata `asset=USDC` and configured network.
- Also verifies crypto asset/network from Stripe crypto display details when Stripe exposes those details.
- If status is not terminal-success, returns the current payment status and does not credit.
- If status is `succeeded`, credits the workspace exactly once using event id `x402:{payment_intent_id}`.
- Credits from Stripe's settled amount (`amount_received` where available, otherwise Stripe `amount` only for already-succeeded PaymentIntents), capped by the amount TrustedRouter originally requested in metadata. Client-supplied settle bodies never control the credited amount.
- Returns whether this call newly credited the workspace or was an idempotent replay.

### Stripe Webhook

The existing `/v1/internal/stripe/webhook` should handle `payment_intent.succeeded` where `metadata.payment_method=x402`.

Webhook and `POST /v1/billing/x402/settle` use the same idempotency key, so either can arrive first:

- webhook first, settle later: settle returns `credited=false`
- settle first, webhook later: webhook returns `credited=false`
- duplicate webhooks: only the first credits
- distinct Stripe webhook events for the same PaymentIntent: only the first successful PaymentIntent credit lands

Webhook signature verification remains mandatory in production. Tests must cover missing signatures, invalid signatures, and replay outside Stripe tolerance.

Additional terminal or near-terminal events:

- `payment_intent.processing`, `payment_intent.requires_action`, partial funding states: record status only, no credit.
- `payment_intent.canceled`, `payment_intent.payment_failed`: record status only, no credit.
- `charge.refunded` / refund events: v1 does not automatically debit already-granted credits. It must create an operator-visible alert and runbook entry. Automated debit support is a follow-up because debiting after credits may have been spent needs an explicit negative-balance policy.

## Data Model

Use existing credit ledger primitives in v1:

- `CreditAccount.total_credits_microdollars`
- `credit_workspace_once(workspace_id, amount_microdollars, event_id)`
- existing `stripe_event` idempotency records

No new Spanner table is required for v1 correctness. A future observability enhancement can add a first-class `payment` record for richer payment history, but the credit ledger and Stripe remain sufficient source of truth for v1.

Money units:

- TrustedRouter ledger: integer microdollars.
- USDC: 6 decimal atomic units.
- Stripe `PaymentIntent.amount`: integer cents.
- x402 funding v1 accepts only cent-exact amounts: `amount_microdollars % 10_000 == 0`.
- For USD-denominated credits, `amount_microdollars` maps 1:1 to USDC atomic units after the cent-exact Stripe amount is confirmed.
- No floats in billing math.

## Enclave Boundary

x402 is not part of the enclave.

The enclave still:

- accepts prompt requests on `api.trustedrouter.com`
- keeps prompt TLS termination inside Confidential Space
- hashes API key metadata for control-plane authorization
- receives either an authorization or an insufficient-credit error
- does not create Stripe PaymentIntents
- does not parse x402 payment headers
- does not send prompt or output content to the payment system

If a caller lacks credits, the enclave can continue returning its current insufficient-credit response. Docs should instruct agents to call the control-plane x402 funding endpoint and retry after funding.

## Agent Flow

1. Agent calls `POST /v1/chat/completions` with `Authorization: Bearer sk-tr-...`.
2. If credits are sufficient, request proceeds normally.
3. If credits are insufficient, the API returns `402 insufficient_credits`.
4. Agent calls `POST /v1/billing/x402/fund` with the same API key and desired top-up amount.
5. TrustedRouter returns a Stripe x402 `payment-required` challenge.
6. Agent pays through its x402-capable wallet/client.
7. Agent polls `POST /v1/billing/x402/settle` with exponential backoff, or waits for webhook settlement.
8. TrustedRouter credits the workspace.
9. Agent retries the original inference request.

Recommended agent polling:

- First retry after 5 seconds.
- Then 10s, 20s, 30s, max every 60s.
- Stop after 10 minutes and tell the user the payment is still pending.
- Server-side `/settle` should rate limit per PaymentIntent to prevent hot loops.

## Security Requirements

- API key required for all x402 endpoints.
- x402 funding never accepts `workspace_id` from the request body in v1. Workspace is derived from the API key.
- Stripe metadata is treated as semi-public: visible to Stripe, Stripe dashboard viewers, exports, and support workflows. Keep it to workspace/payment bookkeeping only.
- Stripe PaymentIntent metadata must not include prompts, outputs, message arrays, request bodies, user supplied prompt text, or API key raw values.
- Stripe PaymentIntent metadata should not include stable API-key hashes unless a later fraud investigation feature explicitly needs it. Workspace id is enough for v1.
- `payment_intent_id` can only credit the workspace stamped on Stripe metadata.
- Duplicate or replayed PaymentIntent IDs credit once.
- `payment_intent_id` must be a Stripe-shaped `pi_...` id before the server contacts Stripe.
- Pending/failed/canceled PaymentIntents never credit.
- Amount credited must come from Stripe's settled amount, bounded by TrustedRouter's requested amount in metadata, not from a client-supplied settle body.
- Logs and Sentry must allowlist x402 fields. Redact `Authorization`, payment payload headers, and raw request bodies.
- Feature flag disabled by default.

## Config

Suggested settings:

- `TR_X402_ENABLED=false`
- `TR_X402_ALLOW_MOCK_PAYMENTS=false`
- `TR_X402_NETWORK=base`
- `TR_X402_NETWORK_ID=eip155:8453`
- `TR_X402_STRIPE_API_VERSION=2026-03-04.preview`
- `TR_X402_MAX_FUND_DOLLARS=500`
- `TR_X402_RATE_LIMIT_WINDOW_SECONDS=60`
- `TR_X402_RATE_LIMIT_KEY_PER_WINDOW=10`
- `TR_X402_RATE_LIMIT_WORKSPACE_PER_WINDOW=30`
- `TR_X402_SETTLE_RATE_LIMIT_PER_WINDOW=30`
- `TR_X402_SETTLE_WORKSPACE_PER_WINDOW=120`

Production prerequisites:

- `TR_STRIPE_SECRET_KEY`
- `TR_STRIPE_WEBHOOK_SECRET`
- Stripe account eligible for crypto/machine payments
- Stripe webhook includes `payment_intent.succeeded`
- `TR_X402_ALLOW_MOCK_PAYMENTS` must never be enabled outside local/test. Production boot fails closed if mock payments are enabled or x402 is enabled without Stripe credentials.

## Tests

Unit tests:

- x402 disabled returns `404 not_found`.
- missing API key returns `401`.
- funding endpoint requires API key and derives workspace from key.
- funding endpoint rejects `workspace_id` in request body.
- funding endpoint enforces max amount and cent-exact amounts.
- funding endpoint rate limits before Stripe calls.
- funding response has status 402 and `payment-required` header.
- Stripe PaymentIntent create args include crypto deposit mode and safe metadata only.
- settle rejects wrong workspace.
- settle returns `404`, not `403`, for cross-workspace PaymentIntent references so it does not confirm existence to another workspace.
- settle rejects non-x402 PaymentIntent metadata.
- settle rejects malformed `payment_intent_id` before calling Stripe.
- settle rejects wrong currency, wrong asset, and wrong network.
- pending PaymentIntent does not credit.
- partial or underfunded PaymentIntent does not overcredit.
- succeeded PaymentIntent credits exactly once.
- webhook and settle are idempotent in both arrival orders.
- two different Stripe webhook event ids for the same PaymentIntent credit exactly once.
- concurrent settle calls for the same PaymentIntent credit exactly once.
- refund/reversal events create an operator-visible status and do not silently ignore the reversal.
- prompt/output strings are absent from x402 response, Stripe metadata, and ledger records.
- amount conversion uses integer microdollars.
- Stripe webhook signature verification rejects unsigned, invalid, and stale signatures.

Integration tests:

- fake Stripe PaymentIntent create/retrieve.
- agent flow: insufficient credits, x402 fund, settle, retry authorizes.
- duplicate webhook replay.

Manual canary:

- enable `TR_X402_ENABLED=1` for a test workspace.
- create a tiny PaymentIntent through `/v1/billing/x402/fund`.
- pay with a test x402-capable client or Stripe test tooling if available.
- verify credits appear and inference authorizes.

## Rollout

1. Land behind `TR_X402_ENABLED=false`.
2. Enable in local/test only with mocked Stripe.
3. Enable in staging against Stripe test mode.
4. Enable one internal production workspace.
5. Add dashboards/alerts for PaymentIntent create error rate, settle latency, webhook-to-credit latency, and stuck PaymentIntents.
6. Add an operator runbook for "Stripe succeeded but credits did not land" and refund/reversal handling.
7. Add public docs after a real stablecoin canary succeeds.
8. Keep the normal Stripe Checkout stablecoin button unchanged.

## Open Questions

- Stripe account eligibility: confirm whether Lore Hex Corp Stripe account has x402 / Machine Payments enabled in production.
- Stripe exact header names: implementation should follow the current Stripe docs at build time. Docs currently show `payment-required`; Coinbase docs use `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` terminology.
- Payment history: v1 can rely on Stripe and credit ledger, but the console may need a first-class x402 payment list later.
- Minimum top-up: recommend `$1` minimum initially to avoid tiny-payment support issues.
