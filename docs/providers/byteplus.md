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

## Implemented, Activation Gated

The native task create/poll/download/delete adapter is implemented in
quill-cloud-proxy PR #444. It calls BytePlus, not Venice, and isolates API
credentials from content downloads. V1 supports four-to-fifteen-second text or
first-frame image generations at 480p/720p. Video input, reference images and
1080p remain unsupported because they require separate tariffs.

Hourly refresh reads the authenticated ModelArk catalog and the first-party
standard-inference pricing document. New chat models need a successful canary;
unknown prices, unsupported APIs and time-dependent tariffs stay non-routable.
Ten chat models passed live canaries. Published token tariffs include cache
rates and context tiers; Flex/batch discounts are never used for online calls.

Video billing reserves a conservative token ceiling, freezes the authorized
tariff, and settles the completed task's integer output-token count exactly
once. It never bills the ceiling as usage. A separate content-free snapshot
persists in all storage backends without enrolling video in Stage D. Missing,
invalid or excessive usage fails closed; refunds release the hold without
requiring usage. Existing fixed-price provider jobs remain compatible.

Native video list prices for these supported modes are $10.70/M output tokens
for 2.5, $7/M for 2.0, and $5.60/M for 2.0 Fast, before the standard router
markup. Account-dependent temporary promotions are not assumed.

Before enabling `NATIVE_ROUTES_DEPLOYED` in the pricing parser:

1. Deploy the control-plane billing change and the native gateway in GCP, AWS
   and Azure through normal CI, attestation and release gates. Verify each
   cloud has its own updated credential; preserve all existing Azure bundle
   entries when sealing a new immutable bundle.
2. Verify public health and regional attestation, then regenerate the manifest
   with the activation flag enabled in a separate reviewed release. Explicitly
   clear only the approved models' `gateway-upgrade-required` holds using
   `set_manifest_model_canary_states`; discovery deliberately preserves
   operator holds even after the activation flag changes. Never clear failed
   canaries or unsupported-price holds.
3. Complete one small production job per Seedance route using
   `provider.only: ["byteplus"]`, check MP4 retrieval and compare usage/cost
   against the authorization's frozen tariff. Repeated polls must not rebill.
4. Email Joseph the actual production result. Upstream success and merged PRs
   alone do not establish production readiness.

Existing Venice endpoints remain for callers explicitly selecting Venice and
for in-flight jobs. BytePlus has the preferred native-provider rank; callers
requiring BytePlus exclusively should set `provider.only: ["byteplus"]`.

First-party references:
- [ModelArk pricing](https://docs.byteplus.com/en/docs/modelark/model-pricing)
- [Seedance 2.5](https://docs.byteplus.com/en/docs/modelark/seedance-2-5)
- [Create video task](https://docs.byteplus.com/en/docs/modelark/create-video-generation-task-api)

Credentials belong only in the restricted operator keyfile and each cloud's
own secret store. No API keys, signed download URLs, prompts, or video content
are recorded here.
