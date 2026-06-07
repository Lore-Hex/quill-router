# AI Data Handling Policy

Status: draft for management approval.

Owner: Privacy/Legal Owner.

## Purpose

Define how TrustedRouter handles prompts, outputs, model routing metadata, provider selection, BYOK, and observability exports.

## Policy

- Prompt/output content is not stored by TrustedRouter by default.
- Prompt/output content must not be sent to Sentry, Axiom, Cloud Logging, Bigtable, Spanner, dashboard payloads, or durable status/benchmark rows.
- Provider compute policies are separate from TrustedRouter's gateway posture and must be surfaced to users.
- Legal and sensitive workloads should default to `trustedrouter/zdr` or `trustedrouter/e2e`.
- Broad fallback aliases may route to providers with weaker or unknown posture unless filtered.
- Customer-enabled content export must be explicit and destination-specific.
- Tools, files, images, and stateful Responses features must be handled according to the current product support boundary and must not silently store content.

## Provider Posture

Provider privacy labels are conservative. Unknown remains unknown unless supported by a tracked public or contractual claim.

## Legal Work Product

Before privileged attorney work product enters production:

1. Customer counsel must review and accept current SOC 2 status.
2. DPA must be signed or written exception recorded.
3. Subprocessors must be reviewed.
4. Routing policy must be set to ZDR, E2E, or approved allowlist.
5. Trust page verification should be completed by the customer or agent.

## Evidence

- Storage tests.
- Broadcast tests.
- Security tests.
- Provider catalog policy URLs.
- Public legal/procurement packet.
- Trust page evidence.
