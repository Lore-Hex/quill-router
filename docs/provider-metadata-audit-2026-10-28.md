# DeepSeek 1017 provider metadata audit

Reviewed 2026-10-28. Scope: the community's 16-provider comparison for
`deepseek/deepseek-v4.1-flash-1017`, and the shared model-page presentation.
The screenshot is a research lead, not evidence. No screenshot latency or
throughput numbers were imported. No inference requests were sent for this audit.

## Published changes

- Model pages show operator country separately from inference geography, with
  expandable company-location evidence, region scope, review dates and sources.
- The same table exposes the actual route's retention policy and source instead
  of requiring readers to infer everything from a badge.
- Mobile layouts stack each provider's labeled fields; desktop tables retain
  sortable prices and keyboard-accessible horizontal scrolling.
- Exact model/provider p50 TTFT and output tokens/s use the existing cached,
  precomputed leaderboard. Each needs at least two valid samples of that metric.
  Missing timings remain insufficient data; the last sample timestamp is shown.
  No provider network calls or production request scans are added to page render.
- Five exact 1017 routes now have reviewed precision records. Public provider
  declarations remain distinguished from pinned serving-code evidence. None
  claims that a particular request's precision has been remotely verified.

## Findings and remaining gaps

| Provider | Evidence and disposition |
| --- | --- |
| Engy | [October 21 policy](https://engy.ai/privacy) requires `provider.zdr=true` or a verified account/key setting for end-to-end ZDR-certified workers. TR does not send that flag and no setting was verified in this audit. Removed unconditional ZDR; default worker access and the Qwen integrity-audit output-token retention are disclosed. Country and 1017 precision remain unverified. |
| Pearl Research Labs | [Policy](https://pearlresearch.ai/legal/privacy) includes submitted prompt personal data and a general retention policy, not an explicit prompt/output ZDR contract. Existing Israeli operator and provider-submitted HQ/location evidence remain separate from exact-model GPU placement. No exact 1017 format declaration verified. |
| Sail Research | [Security policy](https://docs.sailresearch.com/security) and [public DPA](https://docs.sailresearch.com/dpa) cover ephemeral inference processing by Sail and subprocessors. Added policy ZDR; excluded Files uploads and Sailboxes from the claim. [Models](https://docs.sailresearch.com/models) explicitly links the reference 1017 checkpoint, whose reviewed config is FP8 with FP4 experts, not uniform FP4. Default geography is global; [US-only](https://docs.sailresearch.com/us-only-inference) requires a paid-plan option that TR does not enforce. Operator country remains unverified. |
| Wafer | [Public model API](https://pass.wafer.ai/v1/models) confirms exact-model ZDR support; existing TR enforcement sends `Wafer-ZDR: required` for supported models. That catalog does not declare the 1017 format, so the screenshot's MXFP4 label was not copied. US operator country is not a GPU-country commitment. |
| DeepInfra | [Exact-model page](https://deepinfra.com/deepseek-ai/DeepSeek-V4.1-Flash-1017) declares FP8; added a provider declaration, not a serving revision. Existing no-store rather than strict-ZDR classification is preserved because [privacy terms](https://docs.deepinfra.com/account/data-privacy) reserve debugging/security logging. Existing US/Toronto capacity evidence is not model-specific residency. |
| Morph | [1017 launch page](https://www.morphllm.com/deepseek-v4-1-flash-1017) identifies the native route and shared serving pool, but no exact precision declaration was established. [Privacy policy](https://www.morphllm.com/privacy) distinguishes paid-tier retention from enterprise ZDR; no enterprise exception is inferred for TR. |
| Featherless | [Exact-model page](https://featherless.ai/models/deepseek-ai/DeepSeek-V4.1-Flash-1017) declares FP8. [Privacy policy](https://featherless.ai/legal/privacy-policy) explicitly excludes collecting or storing prompts/completions; added policy ZDR while preserving account and usage metadata caveats. Neither establishes confidential compute or GPU countries. |
| Boundless | [October 27 terms](https://inference.boundless.network/terms) now explicitly exclude persistent content storage after real-time processing; content-free request records may last 90 days. Updated the policy summary, but retained strict-ZDR exclusion because equivalent downstream infrastructure retention obligations are not established. Section 3.3 now names US, UK and/or EU infrastructure; added these broad regions without inferring EU countries or a per-request guarantee. Exact 1017 precision remains unverified. |
| Makora | [Policy](https://www.makora.com/privacy-policy) calls Pro/pay-as-you-go ZDR but allows SSD caches without a deletion deadline and flagged-content retention. Clarified these exceptions; did not grant strict ZDR. Section 7 identifies US inference subprocessors, while section 10 permits international processing; added a qualified US-primary declaration. Exact 1017 FP8 remains unverified. |
| Arcee | [July 24 policy](https://www.arcee.ai/privacy-policy) permits input/output collection to provide and improve services; updated the policy link and summary, without inferring a ZDR exception. Operator jurisdiction and exact-model precision remain unverified. |
| Baseten | [Exact-model page](https://www.baseten.co/library/deepseek-v41-flash-1017/) confirms the model, but no 1017 precision declaration was established. Existing synchronous-API ZDR remains scoped by [security docs](https://docs.baseten.co/observability/security); async storage is a separate feature. |
| Cloudflare Workers AI | [October 16 data-usage policy](https://developers.cloudflare.com/workers-ai/platform/data-usage/) distinguishes stateless inference from stateful storage, logging/caching and safety/security/legal exceptions. Replaced the generic unknown-policy text with those qualifications, without granting strict ZDR. No model-specific precision or location proof established. |
| Nebius | [Legal guide](https://docs.tokenfactory.nebius.com/legal/legal-quick-guide) says public inference locations are global/dynamic; Finland retention storage is not the GPU location. Added that distinction and dedicated-endpoint pinning scope. Existing TR account ZDR configuration is not reverified here. [Company newsroom](https://nebius.com/newsroom/nebius-brings-compute-pricing-into-the-open-as-europe-s-ai-builders-scale-on-its-cloud) establishes Amsterdam HQ, not Israel as its HQ. Exact 1017 precision remains unverified. |
| Novita | [Exact-model page](https://novita.ai/models/model-detail/deepseek-deepseek-v4.1-flash-1017) declares FP8; added declaration. Existing [policy](https://novita.ai/legal/privacy-policy) does not establish an account-specific ZDR guarantee. GPU rental region lists remain insufficient evidence of shared API model placement. |
| Lyceum | [Imprint](https://lyceum.technology/legal/imprint/) identifies registered offices in Berlin and Zurich; added them as registered offices, not GPU locations. [1017 announcement](https://lyceum.technology/magazine/deepseek-v4-1-flash-1017-what-changed-and-how-to-switch/) confirms the route but does not establish precision. Existing scoped retention and model-dependent location caveats are preserved. |
| Tinfoil | [Pinned serving config](https://github.com/tinfoilsh/confidential-deepseek-v4-1-flash/blob/ae7a4b1f165502f65ea4b01437f118daa9be762d/tinfoil-config.yml) maps `deepseek-v4-1-flash` to the 1017 checkpoint at `1bae0e1088993b9b82de08f59de5be5a34e42170`, with `fp8_ds_mla` cache. [Pinned weight config](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/1bae0e1088993b9b82de08f59de5be5a34e42170/config.json) declares FP8 plus FP4 experts. Added the exact 1017 record. Source evidence is not a live deployment attestation; existing confidential/ZDR routing requirements are unchanged. |

## Follow-up evidence needed

Providers with unknown precision should supply the exact API model ID,
weight repository/revision, mixed-weight formats and KV cache format. Providers
with unknown geography should supply all possible serving countries, failover
scope and enforceable pinning. Do not infer these from company registration,
CDN location, another provider's weights or a screenshot.

Engy needs a separately tested request/key-level ZDR worker restriction, plus
clarification of integrity-audit retention, before restoring strict eligibility.
No account settings or vendor contracts were changed during this audit.
