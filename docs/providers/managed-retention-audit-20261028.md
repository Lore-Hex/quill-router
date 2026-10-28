# Managed OpenAI and Vertex retention review

Reviewed October 28, 2026. Account inspection was read-only. No PSP setting,
Google addendum, storage bucket, cache setting or credential was changed.

## Account evidence

- OpenAI organization **Lore Hex Corp**, Data controls > Data retention:
  modified retention Active; organization default Zero Data Retention;
  Default project Zero Data Retention. PSP was not enabled in this dashboard.
- The managed production OpenAI credential could list models (HTTP 200).
  That response did not identify its project or organization, so the probe
  alone does **not** independently prove its binding to the displayed project.
  This change narrows, rather than expands, the existing reviewed account claim.
- OpenAI's October 28 enterprise notice explicitly says existing models can
  continue ordinary ZDR. Future models may require PSP; no universal cutover is
  inferred from a recommended test date.
- The managed Vertex project `quill-cloud-proxy` returned
  `retentionConfig.retentionType=DURABLE` from the regional `cacheConfig` API
  in us-central1, us-east1, europe-west4 and southamerica-east1. All four reads
  returned HTTP 200. Google's project-specific notice described October 15
  durable caching activation for Gemini 3.x and newer eligible models.
- The existing Vertex managed-account contract classification is retained only
  for the explicitly reviewed legacy routes. This audit did not obtain a new
  signed contract or accept Advanced AI/Frontier terms.

## Enforcement and publication

`managed_retention.py` reviews exact provider, canonical model and upstream ID
pairs for the Credits credential path. It can only restrict an account claim.
Automatic model discovery must not append to the review registry. New or
remapped IDs fail closed for strict ZDR without preventing ordinary routing.

The current OpenAI routes have no verified PSP activation. Do not label them
PSP merely because the feature exists. When a model is explicitly reviewed as
PSP, associate its exact pair with the appropriate retention policy; the same
policy drives endpoint APIs, model/provider pages and strict routing filters.
Review the entire affected set before any managed-account setting change.

OpenAI's current prompt-caching documentation also describes encrypted GPU-local
cache state up to 24 hours. GPT-5.5 rejects in-memory-only retention; GPT-5.6 and
later use minimum TTLs, not deletion deadlines. Without a verified and enforced
in-memory-only configuration these exact reviewed models are labeled durable
cache and excluded from strict ZDR, independently of the ordinary ZDR account
setting. Earlier reviewed models default to in-memory caching on a ZDR account;
the router does not forward the unsupported extended-retention control.

PSP is not a higher privacy tier: retained encrypted safety records remain
retained content. OpenAI's customer-storage PSP requires **at least** 30 days,
not a universal maximum of 30 days. On a managed account that storage is
TrustedRouter-controlled, not the user's. Automated attested safety review is
not proof of confidential model inference. Other PSP storage variants require
their own policy and must not reuse customer-storage claims.

Google's Advanced AI and Frontier safety retention are labeled separately,
not as OpenAI PSP. Scope depends on model, feature and agreement. Unreviewed
Claude variants are excluded rather than asserting they all retain content.
Known affected Gemini routes are labeled durable cache and excluded. Other new
models stay unverified until their actual settings and terms are reviewed.

To restore affected Vertex routes to strict ZDR, disable durable caching through
Google's documented API, check all serving locations and any data expiration
requirements, verify the governing agreement and feature exceptions, then update
the exact reviewed routes in a separate tested release. Do not change the label
first or interpret encryption/no-training as zero retention.

Use `provider.min_privacy="zdr"` for a hard floor. Provider selection alone is
not a retention guarantee. The central endpoint policy also governs fallback,
auto pools and the gateway authorization path. No gateway binary/pin changes
are required for this control-plane classification change.

## Sources

- [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)
- [OpenAI private safety processing](https://developers.openai.com/api/docs/guides/private-safety-processing)
- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Google ZDR and caching](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/zero-data-retention)
- [Google abuse monitoring and model-specific safety retention](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/abuse-monitoring)

Do not store credentials, bucket contents or raw private correspondence in this
repository. Re-audit account controls before changing these classifications.
