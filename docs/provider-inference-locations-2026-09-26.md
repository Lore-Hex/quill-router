# Inference location response, September 26, 2026

This records physical model-execution evidence, not corporate jurisdiction,
API ingress, storage location, ZDR or confidential-computing eligibility.
Those properties are separate. No inference-routing or privacy eligibility is
changed by publishing this information.

## Customer reply

Thank you for asking. Here is what we can substantiate as of September 26, 2026.
We cannot yet provide an exhaustive country declaration for every listed route;
the unconfirmed entries below need a model-specific statement from the provider.

| Model | Provider | Inference location evidence | Dynamic behavior / pinning through TrustedRouter |
| --- | --- | --- | --- |
| Qwen3.8-27B | Engy | Countries not disclosed in the reviewed API catalog or public documentation. | Worker/failover geography unconfirmed; no supported regional pin. |
| Qwen3.8-27B | Novita | Serverless model countries not declared in the reviewed catalog. Worldwide GPU rental locations are not evidence for this route. | Placement/failover unconfirmed; no supported regional pin. |
| Qwen3.8-27B | Telnyx | Live native catalog advertises USA only for the default tier. | Availability can change. Current route is not strictly region-pinned. |
| GLM-5.3-Flash | Telnyx | Live native catalog advertises USA, EU, Australia and UAE for the default tier. EU inference country is not specified. | Latency/capacity-based routing can change regions. Current route is not strictly region-pinned. |
| GLM-5.3-Flash | Pearl Research Labs | Provider supplied US Central, Taiwan and Australia during onboarding. This is provider-wide, not a confirmed exhaustive list for this model. | Model placement/failover unconfirmed; no supported regional pin. |
| GLM-5.3-Flash | Engy | Countries not disclosed in the reviewed API catalog or public documentation. | Worker/failover geography unconfirmed; no supported regional pin. |
| DeepSeek-V4.1-Flash | Pearl Research Labs | Same provider-wide US Central, Taiwan and Australia declaration; this model's location set is unconfirmed. | Model placement/failover unconfirmed; no supported regional pin. |
| DeepSeek-V4.1-Flash | SiliconFlow | No verified country list for the integrated api.siliconflow.com route. Singapore corporate jurisdiction does not establish GPU location. | Placement/failover unconfirmed; no supported regional pin. |
| DeepSeek-V4.1-Flash | DeepInfra | DeepInfra publicly states that its own inference infrastructure uses United States data centers. This is provider-declared, not per-request attestation. | Individual US site unspecified; no supported per-request regional pin. |

Telnyx itself documents strict region selection using `region` plus
`mode: "strict"` and a matching regional ingress domain. It fails rather than
serving outside that region. Our current Telnyx integration does **not** expose
that control, so please do not treat selecting Telnyx, its US ingress, or a
TrustedRouter regional hostname as an inference-region guarantee.

For your infrastructure register, use:

- DeepInfra: "United States, provider-declared inference hosting; individual site unspecified."
- Telnyx Qwen: "USA currently advertised; dynamic availability, not region-pinned."
- Telnyx GLM: "Multi-region: US, EU, Australia, UAE; dynamically routed, not region-pinned."
- Pearl: "Provider declares US Central, Taiwan and Australia; model-specific and exhaustive failover scope unconfirmed."
- Engy, Novita and SiliconFlow: "Inference countries unconfirmed; no verified country-specific residency commitment."

Do not write just "global" where the real fact is "unknown": global is not an
exhaustive country list. For a strict residency requirement, use a route with a
confirmed processing-location commitment and fail-closed enforcement.

## Evidence

- [Telnyx GPU regions and pinning](https://developers.telnyx.com/docs/inference/models/regions)
- [Telnyx processing versus storage](https://developers.telnyx.com/docs/inference/data-residency)
- [Telnyx native catalog](https://api.telnyx.com/v2/ai/openai/models), authenticated read on September 26: `Qwen/Qwen3.8-27B` default `USA`; `zai-org/GLM-5.3-Flash` default `AUS, EU, UAE, USA`.
- [DeepInfra infrastructure declaration](https://deepinfra.com/)
- [Engy API documentation](https://engy.ai/docs) and [processing policy](https://engy.ai/privacy). Its reviewed live catalog carries no location fields for the requested models.
- [Novita GPU rental regions](https://blogs.novita.ai/gpu-regions-zones/) describe a different product. Its reviewed [model catalog](https://api.novita.ai/openai/v1/models) carries no location fields for Qwen3.8-27B.
- Pearl's marketplace submission supplied US Central, Taiwan and Australia. The reviewed [native catalog](https://inference.pearlresearch.ai/v1/models) carries no location fields for the requested models. Its [privacy policy](https://pearlresearch.ai/legal/privacy) is not a model-location declaration.
- [SiliconFlow API docs](https://docs.siliconflow.com/) and [privacy policy](https://docs.siliconflow.com/en/legals/privacy-policy) do not substantiate a complete physical inference-country list for this route.
- Current enclave integration uses fixed provider API bases; chat request validation does not expose Telnyx `region`/`mode`. Existing company-jurisdiction filters are not GPU-country filters.

## Provider follow-up

Please confirm, for each named model on the shared API used by TrustedRouter:
every country/broad region that can execute inference, including capacity
failover; whether that set changes dynamically; any region-pin API or contract;
whether an unavailable pin fails closed; and the public source or written
statement we may publish. Separate GPU execution from ingress, storage and
operational metadata. For Pearl, confirm whether the onboarding region list is
still current and exhaustive. For Telnyx, confirm the EU inference countries.

## Website behavior

Provider pages show reviewed evidence and explicitly distinguish upstream
pinning from support in TrustedRouter. Unreviewed providers remain "Not yet
reviewed", not inferred from their legal address or privacy badge. Telnyx
model-level regions use the existing committed catalog and its generation
timestamp, not a duplicate manually maintained model list. Refresh uses the
default service tier, clears withdrawn region declarations and never treats an
unrecognized region code as permission to advertise a narrower country set.
