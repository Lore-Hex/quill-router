# Greenference

Reviewed 2026-10-09. Provider slug: `greenference`.

- Native base: `https://llm.eu.greenference.com/trustedrouter/v1`.
- Discovery and exact USD-per-million prices: authenticated `GET /models`, Catalog v2.
- Ten initial routes passed non-streaming PONG and streaming/usage probes. Streaming
  carries integer input/output/total counts and reasoning counts where provided.
  Qwen3-30B-A3B emits inline `<think>` content; do not imply separated reasoning.
- Canonical shared model IDs retain the exact `greenference/*` native upstream ID.
  New contract-compliant IDs are automatically discovered and canaried. Invalid
  prices, duplicate IDs and failed canaries do not silently become healthy routes.
- Input/output and cache-read prices come from the catalog, never inferred from
  another provider. Cache discounts require reported cached tokens, not estimates.
- Live GLM-5.3-Flash cache probe: 2,825 input / 4 output tokens; repeat reported
  2,816 cached input tokens and $0.000082068, exactly matching the published
  $0.14/$0.49/$0.028 input/output/cache-read per-million rates.

## Privacy and location

[DPA](https://greenference.com/legal/dpa): EU-only, no persistent prompt/output
storage and no training. Prefix caches are shared volatile GPU memory with no
fixed TTL. Metadata retention is distinct from content retention. This is ZDR,
not TEE or E2EE. Current rented GPU electricity mix is unverified; no renewable
badge. Exact inference countries and country pinning are not available.

The public registry contains Greenference SAS's French jurisdiction and policy
links. Named application contacts and phone numbers must not be published.

## Deployment

Local source `GREENFERENCE_API_KEY` maps to `trustedrouter-greenference-api-key`.
Only attested gateways and authenticated pricing discovery need the credential;
do not mount it in the public control plane. Publish independent cloud copies.
Azure must seal a bundle containing the secret before setting
`QUILL_GREENFERENCE_SECRET=trustedrouter-greenference-api-key`; the existing
bundle-manifest gate must remain enforced. Deploy the gateway before publishing
the control-plane routes. Verify forced-provider streaming and settlement after
rollout, with no fallback masking a missing provider credential.
