# Meta Direct API

Meta chat traffic uses `https://api.meta.ai/v1`, authenticated with
`META_API_KEY` (`trustedrouter-meta-api-key` in the provider secret stores).
It does not use an OpenRouter credential or transport.

## Published Models

| TrustedRouter model | Native model | Tier |
| --- | --- | --- |
| `meta/muse-spark-1.1` | `muse-spark-1.1` | Standard |
| `meta/muse-spark-1.2` | `muse-spark-1.2` | Standard |
| `meta/muse-spark-1.3` | `muse-spark-1.3` | Standard |

On October 6, 2026, Meta's published Standard rates are $1.25 input, $4.25
output, and $0.15 cached input per million tokens. The hourly refresh reads
Meta's authenticated model inventory and first-party pricing page. Unknown
price shapes fail closed. Newly admitted routes must pass a content and usage
canary. The native manifest is authoritative, so aggregator-only routes cannot
reappear under the direct Meta provider.

Standard tier does not use prompts or completions for model training. This is
not a zero-data-retention or confidential-compute guarantee. Contributor tier
explicitly permits training and is excluded. Image generation, transcription,
and segmentation models are not published by this chat adapter; they require
separate endpoint and billing support. Chat supports text and image input.

## Deployment Order

1. Provision the direct key using the existing cloud-local secret mechanisms.
2. Deploy the enclave adapter and verify regional readiness.
3. Publish the control-plane catalog.
4. Run provider-pinned production PONG canaries with fallbacks disabled.

Azure's key binding is optional until its immutable bootstrap bundle is
resealed and pinned with the Meta key. Never name an absent bundle entry.
AWS requires the `api.meta.ai:443` egress tunnel on port 8107.

Direct canaries verified all three Standard chat models. SambaNova also passed
a production, provider-pinned streaming canary with reported token usage.
Abliterate remains held: its successful JSON and SSE canaries still omitted
usage, including with `stream_options.include_usage=true`. Do not clear that
hold by substituting estimated billing.

Sources: [Meta models](https://dev.meta.ai/docs/models),
[pricing and tier terms](https://dev.meta.ai/docs/pricing-rate-limits),
[reasoning controls](https://dev.meta.ai/docs/reasoning).
