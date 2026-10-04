# Verda Onboarding

Status: registered and visible, not routable. No customer traffic, production
credentials, or speculative model/pricing entries are enabled by this change.

## Live Checks

Checked September 23, 2026 UTC:

- The official console publishes `https://inference.verda.com` as its inference
  base URL, with OpenAI chat at `/v1/chat/completions`.
- Authenticated `GET https://inference.verda.com/v1/models` returns HTTP 200,
  but the only model is `no-default-models`. The same request without a key
  returns HTTP 401. Authentication works; model availability is not established.
- `GET https://api.verda.com/v1/inference/models` rejects the inference key.
  The [Cloud API](https://api.verda.com/v1/docs) documents OAuth client
  credentials separately from inference keys. This does not prove the
  inference key is invalid.
- The public `/v1/managed-endpoints/pricing` feed has image/request/video
  prices, but no verified input/output token rates for the chat endpoint.
  Do not turn per-request media rates into per-token rates.
- The Cloud API docs describe inference catalog token pricing as placeholder
  pricing, not yet billed. Do not publish that as a confirmed commercial rate.

The website advertises confidential-computing infrastructure. This is not
evidence that the shared inference route is attested or end-to-end encrypted.
No route-specific ZDR, confidential-compute, or E2EE guarantee is recorded.

## Activation Requirements

1. Confirm the inference account/project has callable model IDs, through the
   console or Verda support. Do not provision paid GPU deployments implicitly.
2. Obtain authoritative, billable input/output/cache pricing and a stable
   discovery contract. Implement hourly refresh using the existing shared
   catalog/pricing helpers, retaining price-spike and coverage gates.
3. Verify streaming and non-streaming inference, integer usage, and billing
   with the actual IDs. Never publish `no-default-models` as a model.
4. Confirm resale authorization: the published
   [terms](https://verda.com/terms-and-conditions), Schedule 1, restrict resale
   unless separately agreed. The account may have separate terms; verify them.
5. Stage the inference credential independently in each cloud's secret store,
   then enable only priced, canaried routes through normal deployment gates.

Keep the provider visible with zero eligible routes until these checks pass.
Do not copy keys, account identifiers, or private console exports into this
repository.
