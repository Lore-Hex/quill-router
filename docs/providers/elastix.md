# ElastixAI onboarding

Status: held. No customer route, active price entry or production credential
has been provisioned by this integration. Use only `ELASTIX_API_KEY` against
`https://api.elastix.ai/v1`. Do not copy the operator keyfile into the
control plane or attach the application to a public issue.

## Observed November 20, 2026

The authenticated `GET /v1/models` returned HTTP 200 and two native IDs:
`gpt-oss-120b` and `qwen3.8-27b`. Each row had only `created`, `id`,
`object` and `owned_by`. Neither prices nor a v2 contract were present.
The [published API reference](https://www.elastix.ai/docs) does not specify
a version selector for this endpoint. Do not backfill prices, limits, cache
rates or availability from application examples.

Two direct synthetic requests (no retries, maximum 96 output tokens each)
returned HTTP 200, matching model IDs, nonempty outputs and consistent
integer usage. The GPT-OSS JSON response reported 73 input and 45 output
tokens; the Qwen stream reported 58 input and 38 output tokens and ended
with `[DONE]`. These are direct onboarding probes, not TrustedRouter canaries
or tests of tools, caching, all modalities or per-request residency.

## Activation checklist

1. Confirm the production account, billing terms and express marketplace
   resale authorization under the [ElastixAI terms, section 2.1](https://www.elastix.ai/legal/terms).
   Test credit alone is not a production funding arrangement.
2. Obtain a key-scoped [Contract v2 feed](https://trustedrouter.com/providers/marketplace/catalog.v2.schema.json)
   with callable canonical IDs, prices as exact USD/M decimal strings, cache
   charges, limits, capabilities, lifecycle and reliability metadata. Run the
   [provider checker](https://github.com/Lore-Hex/trustedrouter-provider-check)
   against the same account and retain its dated, redacted report.
3. Confirm account-specific content retention under the
   [DPA](https://www.elastix.ai/legal/dpa). Do not label this provider as
   Confidential or 100% renewable. Do not advertise city-level pinning.
4. Wire the cloud-local gateway secrets and adapter following the normal
   guarded release. Keep all models held until the deployed gateway and
   TrustedRouter billing, provider-pinned JSON and streaming canaries pass.
   Only then enable the price refresh and public listing, verify the public
   provider/model endpoints and notify the operator.
