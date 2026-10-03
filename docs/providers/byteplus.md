# BytePlus ModelArk Readiness

## Verified October 3, 2026

The operator's updated `BYTEPLUS_API_KEY` authenticates against
`https://ark.ap-southeast.bytepluses.com/api/v3/models` (HTTP 200).
The account's inference activation is no longer a blocker.

Real, paid text-to-video canaries completed successfully. Each requested four
seconds at 480p, 16:9, without audio. The resulting HTTPS MP4 was downloaded
without forwarding the API key and passed a size/header check.

| Native model | Task ID | Reported completion tokens | MP4 bytes |
| --- | --- | ---: | ---: |
| `dreamina-seedance-2-5-260628` | `cgt-20261003224324-vdy12` | 38,830 | 897,249 |
| `dreamina-seedance-2-0-260128` | `cgt-20261003224628-dzcpm` | 40,594 | 590,630 |
| `dreamina-seedance-2-0-fast-260128` | `cgt-20261003224430-bcsrr` | 40,594 | 454,427 |

`dola-seed-2-1-turbo-260628` also passed a direct chat canary: HTTP 200,
nonempty choices, 49 prompt tokens and 33 completion tokens (31 reasoning).
These are direct upstream checks, not production TrustedRouter route checks.
Other discovered models and input modes have not been certified by these tests.

## Remaining Integration Work

The public `bytedance/seedance-*` models currently use Venice. BytePlus itself
remains disabled for both credits and BYOK. Do not conflate the model's creator
with its selected inference provider.

The enclave's direct OpenAI-compatible chat registry has BytePlus transport and
secret wiring, but its native video provider registry has no BytePlus adapter.
There is also no BytePlus pricing-refresh implementation.

ModelArk video charges depend on actual output-video tokens. The existing
video settlement path uses the fixed quote saved on the job and does not carry
native token usage from `PollResult`. Do not publish a per-second estimate as
an exact upstream charge, borrow BytePlus LAS pricing (a different API), or
enable routes merely because an upstream task was accepted.

Before enabling direct BytePlus routes:

1. Add the native ModelArk task create/poll/download/delete adapter, with
   bounded requests, credential isolation on downloads, and truthful errors.
2. Ingest exact first-party tariffs and supported billing dimensions, including
   image/video-input and resolution differences, through hourly pricing refresh.
3. Reserve a validated cost bound, settle actual reported video-token usage,
   and test idempotency, refund, malformed usage, overflow, and missing usage.
   Preserve the fixed-price contract for existing video providers.
4. Run end-to-end tests through TrustedRouter before publishing the direct
   endpoints. Deploy gateway support before enabling the control-plane catalog.

First-party references:
- [ModelArk pricing](https://docs.byteplus.com/en/docs/modelark/model-pricing)
- [Seedance 2.5](https://docs.byteplus.com/en/docs/modelark/seedance-2-5)
- [Create video task](https://docs.byteplus.com/en/docs/modelark/create-video-generation-task-api)

Credentials belong only in the restricted operator keyfile and each cloud's
own secret store. No API keys, signed download URLs, prompts, or video content
are recorded here.
