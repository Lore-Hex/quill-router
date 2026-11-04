# Negbeta

Negbeta Limited operates the OpenAI-compatible endpoint
`https://inference.negbeta.com/v1`. Use only `NEGBETA_API_KEY` for this origin.
The authenticated `/v1/models` Contract v2 catalog is authoritative for the
managed account's models, prices, capabilities and lifecycle. Native IDs are
case-sensitive and currently equal the canonical IDs.

| Model | Input USD/M | Cached input USD/M | Output USD/M | Context | Max output |
| --- | ---: | ---: | ---: | ---: | ---: |
| `qwen/qwen3.8-27b` | 0.19 | 0.045 | 1.95 | 262144 | 65536 |
| `deepseek/deepseek-v4.1-flash-1017` | 0.30 | 0.03 | 1.20 | 1048576 | 393216 |

These are upstream prices, not customer retail prices. Existing TrustedRouter
markup, integer token accounting and cache-read pricing apply unchanged. No
minimum-request or cache-write fee is accepted by the catalog parser. Catalog
capabilities include image input, tools, structured output and reasoning; the
initial live checks cover text JSON/SSE only, not all declared capabilities.

## Privacy And Location

Standard privacy, not strict ZDR, Confidential or provider-side E2EE. Negbeta
declares no training or prompt/output logging. Its current prefix caches are
memory-only, shared per model server, LRU-evicted and restart-cleared. The
October 29 data policy explicitly permits future local-disk caching, unlike the
API documentation's memory-only description. Strict ZDR must remain disabled
until this discrepancy is resolved with a binding account-specific commitment.
This is not evidence that disk caching is currently in use.

Operational metadata: 180 days; raw IP/user agent: 30 days. Cloudflare terminates
TLS and can process traffic outside the EU. No workload attestation is offered.
The company is registered in the UK; that is distinct from GPU location.

The application pins TrustedRouter's key to EU/Spain, while the current catalog
and response header identify only `eu`. Publish the scoped EU declaration, not
an independently verified Madrid location or a claim about all Negbeta routes.
Select `provider.only=["negbeta"]` with `allow_fallbacks=false` to restrict the
provider; this does not constrain TrustedRouter gateway geography or transit.

Sources reviewed November 4, 2026:
- https://inference.negbeta.com/docs
- https://negbeta.com/inference/data-policy/
- https://negbeta.com/inference/privacy/

The local provider logo is a PNG rendering of Negbeta's official
`https://negbeta.com/favicon.svg`, retrieved November 4, 2026. The social card
uses the existing provider renderer and reports zero active routes while held.

## Release Gate

The account owner authorized ongoing production traffic on November 4, 2026.
Four bounded upstream checks passed at 18:34 UTC: JSON and SSE for both models,
64 output tokens maximum, `reasoning_effort=none`, no retries. All returned 200,
exact model identity, expected output, integer usage, request IDs and declared
`X-Serving-Region: eu`. Cache-read counts were zero; no live cache-hit proof is
claimed. Generated content and credentials were not logged.

The initial manifest has `gateway-rollout-pending` operator holds on every row,
including newly discovered rows. Upstream success is not gateway deployment.
Remove this hold only after the companion gateway change passes normal CI,
all-cloud credential/attestation/release gates and actual production checks.
Then run the normal new-route canaries before publishing active routes. Never
convert a passed onboarding probe into a claim of production availability.

Credentials belong only in cloud-local gateway secret stores and the pricing
job, never mounted into the production control plane. Provision only the new
secret; do not replay whole-account secret synchronization or replace newer
Azure bundles. No credential provisioning or gateway deployment is recorded by
this source change.
