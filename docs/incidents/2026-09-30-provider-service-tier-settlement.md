# Provider service-tier settlement failure

## Evidence

On September 30, 2026, US Central returned seven HTTP 500 responses from
`/internal/gateway/settle` between 14:29:02 and 14:29:22 UTC. Each raised
`ValueError: OpenAI service tiers require an OpenAI endpoint` before the
settlement intent was persisted. The retry cadence and common Azure gateway
address are consistent with one settlement being retried, not seven distinct
customer requests. Azure's container-log API returned an internal error, so
the seven attempts could not be joined to one gateway request ID.

A bounded reservation-expiry-index lookup found one open hold in the matching
window: GPT-5.6 Sol AZ through Atlas Cloud, belonging to TrustedRouter Synthetic
Monitoring. Its authorization at 14:26:41 UTC coincides with an authorize call
from the same gateway address. The hold was 16,247 microdollars; it had no
settlement intent and had not been charged. This is attribution evidence for
the probe, not proof that no customer could encounter the same code defect.

The direct Atlas model catalog published its own prompt, completion, cached
input and long-context tariffs. A tiny direct diagnostic succeeded with
`service_tier=default`; it did not reproduce the intermittent `priority`
report. The production exception itself proves that a non-OpenAI endpoint
reached the OpenAI-only priority branch.

## Root Cause

The shared cost helper selected a tariff from the tier string before checking
the selected provider. A compatible provider can report `priority`, but that
does not authorize applying OpenAI's direct-service tariff to its endpoint.
The ensuing exception also affected refunds because cost calculation preceded
the zero-cost refund branch. Repeated delivery could not repair a deterministic
pricing exception.

## Repair

Dispatch OpenAI Priority pricing only for direct OpenAI endpoints. Other
endpoints retain their selected catalog tariff, including cached tokens,
context tiers and authorization-time price history. This does not enable
requesting an unsupported priority tier: authorization still rejects those
requests. Unknown or discounted actual-tier labels remain rejected rather
than silently priced as ordinary traffic.

Eight new regression cases failed on the previous code with its exception or
HTTP 500. They cover provider-owned tariffs, the difference between publisher
and provider, memory/typed-Spanner settlement, refund release and replay without
double charging. Existing OpenAI Priority and tier-vocabulary tests protect
the direct-provider premium and discounted-tier rejection.

No alerts were loosened, raw request content was logged, or production ledger
rows were changed during the investigation. The existing expiry/reaper path
remains responsible for the unmatched synthetic hold unless exact delivered
usage is recovered for a separately verified repair. Deployment and live
verification must be recorded separately; a local test pass is not a release.
