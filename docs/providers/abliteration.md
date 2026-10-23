# Abliteration.ai

Provider slug: `abliteration`. This is not `abliterate` (Abliterate.ai).
The vendored provider mark comes from `https://abliteration.ai/apple-icon.png`.
The upstream is `https://api.abliteration.ai/v1`, authenticated with the dedicated
`ABLITERATION_API_KEY`. No credential values belong in this repository.

The hourly refresher reads authenticated `/models` for model availability,
context, modalities, capabilities and decimal USD-per-token input/output/cache
prices. There is no fixed model allowlist. New priced models must pass a real
PONG/usage canary; failed discovery cannot republish old data as fresh. Existing
manifest expiry, coverage and price-spike guards remain in force.

On 2026-10-23, all four available models passed both JSON and SSE PONG probes
with integer usage and a final usage chunk. Rates below are upstream USD per
million tokens, before TrustedRouter's normal pricing policy:

| Native ID | Input | Output | Cached input | Context |
| --- | ---: | ---: | ---: | ---: |
| abliterated-model | 1.00 | 3.00 | 0.10 | 262144 |
| abliterated-model-large | 3.00 | 5.00 | 0.30 | 1000000 |
| abliterated-model-large-v2 | 3.00 | 5.00 | 0.30 | 1000000 |
| abliterated-model-large-flash | 1.00 | 2.00 | 0.10 | 1048576 |

Public model IDs prepend `abliteration/`. Vision is declared only for the base
and Large Flash models. Billing uses actual provider usage, including cache
reads, without Abliterate's estimated-billing exception.

A repeated Large Flash synthetic prompt reported 4,480 cached tokens out of
4,518 input tokens. The first request cost $0.004524 upstream; the repeated
request cost $0.000492 at the declared cache rate. These are probe observations,
not cache-hit guarantees. Callers can use a stable opaque `prompt_cache_key`.

Ordinary chat is ZDR under https://abliteration.ai/data-handling; operational
metadata is retained. Native paid web tools are excluded and rejected by the
gateway because their billing and retention differ. No E2EE/confidential-compute
claim is made. US inference is provider-declared, not attested; EU residency
requires a separate provider agreement and is not enabled here.

Deployment order: provision cloud-local secrets, deploy the companion gateway
integration through CI/attestation gates, then publish active catalog routes.
