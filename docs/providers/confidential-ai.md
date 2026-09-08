# Confidential AI Onboarding

Provider slug: `confidential-ai`. Company: Inexorable, Inc. (US), doing business
as Confidential AI. Serving regions in its policy are the US and El Salvador;
US company jurisdiction is not a US-only residency guarantee.

## Verified September 8, 2026

The authenticated `https://api.confidential.ai/v1/models` catalog returned:

| Native model | Catalog model | State |
| --- | --- | --- |
| `deepseek-ai/DeepSeek-V4-Flash-0731` | `deepseek/deepseek-v4-flash-0731` | Inference and price verified locally |
| `MiniMaxAI/MiniMax-M3-MXFP8` | `minimax/minimax-m3` | Disabled: no published price |

DeepSeek's published USD per million tokens: input $0.20, cached input $0.018,
output $0.40. The existing customer markup and integer token ledger apply.
Cache discounts apply only to usage explicitly reported as cached by upstream.
No new billing or settlement implementation is introduced.

Both non-streaming and streaming synthetic PONG requests succeeded, including
integer usage and the final `[DONE]` event. The Go adapter's opt-in live smoke
also passed. These are local integration checks, not production rollout evidence.

## Automatic Discovery

`scripts/pricing/providers/confidential_ai.py` joins the authenticated catalog
with the named input/cached-input/output columns on the public pricing page.
Exact decimal conversion, shared price-spike guards, canaries, manifest expiry,
and discovery coverage checks remain enabled. Hardware-hour price tables are
ignored. A model on the price page alone never creates a route.

M3 remains under an explicit missing-price hold, so this reviewed gap does not
block other providers' refreshes. When its exact price is published,
the same refresh can price and canary it without a manual enable switch.
The documented Flash 0731 release maps to the public Flash price; future dated
releases do not automatically inherit that price.

## Privacy Boundary

The public privacy policy promises zero prompt/completion retention and no
training on customer data. Operational/billing metadata are retained. Routes
qualify for ZDR, not the strict `e2e` / `confidential` filter.

The live attestation advertised schema version 2, scope
`launch-or-admission-only`, and `operationalStatus: not-verified`. A subsequent
[independent audit](confidential-ai-e2e-audit-2026-09-08.md) verified CPU evidence,
the front-door TLS binding and sealed policy, and eight Blackwell GPU reports.
Full reviewed-release and downstream request-path verification remains open.
The current adapter uses ordinary HTTPS, not a measurement-pinned attested
transport. No end-to-end verification claim should be made for this integration.

## Release Order

The private keyfile variable is `CONFIDENTIAL_AI_API_KEY`; never commit its value.
The enclave uses `QUILL_CONFIDENTIAL_AI_SECRET`, with logical secret name
`trustedrouter-confidential-ai-api-key`. GCP, AWS and Azure provisioning use
independent local-source copies, not cross-cloud secret reads.

1. Provision the new secret using the existing per-cloud deployment tooling.
   Grant the GCP refresher its existing narrow per-secret accessor binding.
2. Deploy the companion enclave provider-registry change across serving regions.
   Verify the configured key and local/region smoke before publishing routes.
3. Merge/deploy the control-plane change, including the refresh credential.
4. Pin a production test to `provider.only: ["confidential-ai"]`, verify response
   and usage, and confirm the next hourly refresh and coverage gate pass.

Do not merge the new mandatory refresh-secret coordinate before provisioning it.
Do not treat the initial test key as evidence of production capacity or SLA.

## Sources

- Provider artwork: the official `https://confidential.ai/icon.png`, vendored
  locally; its social card uses the shared provider-card generator.
- [API reference](https://confidential.ai/docs/inference-api/reference)
- [Pricing](https://confidential.ai/pricing)
- [Privacy policy](https://confidential.ai/legal/privacy-policy)
- [Attestation](https://confidential.ai/docs/inference-api/attestation)
