# Maple encrypted-inference pilot

Status (2026-10-28): **internal onboarding only; no customer route is enabled**.
Provider slug `maple`, legal name Maple Privacy Labs Inc. Do not add Maple to
`OPENAI_COMPATIBLE_PROVIDERS` or the gateway Credits allowlist: the backend is
not a bearer-only OpenAI HTTP endpoint.

## Initial model

| Proposed TR ID | Native ID | Input USD/M | Cached input USD/M | Output USD/M |
| --- | --- | ---: | ---: | ---: |
| `maple/deepseek-v4-1-flash-1017-uncensored` | `deepseek-v4-1-flash-1017-uncensored` | 0.65 | 0.13 | 1.45 |

Prices were checked against <https://maple.ai/pricing> on 2026-10-28.
The [model documentation](https://maple.ai/docs/proxy/models) advertises a
1,048,576-token shared context and image/tool/reasoning support. The first pilot
is text Chat Completions only; advertised capabilities are not live-test results.
Do not merge this checkpoint into a stock DeepSeek alias or automatically route
it through a different model. At the source revision below it uses Maple i02;
stock `deepseek-v4-1-flash` can use shared Tinfoil as well as Maple i01.

The operator's offer specifies whole-credit rounding and a USD 0.001 minimum
for nonzero usage. The public table does not specify the rounding algorithm.
Confirm the credit quantum, rounding direction/ties, aggregation boundary,
cache-count semantics, and debit behavior on failed/partial streams. Reconcile
probes against actual Maple account debits. Per-token rates alone are NOT an
adequate production billing adapter. Do not treat complimentary credits as a
zero upstream cost or add this model to token-price-only customer settlement.

## Access and terms

Create a dedicated ordinary account at <https://app.maple.ai/> and put its
inference key in the operator keyfile as `MAPLE_API_KEY`. Share only the account
email or ID with `support@maple.ai`, never the key. The operator supplied
`MAPLE_API_KEY` after the initial check. Authenticated encrypted discovery and
JSON/SSE PONG probes passed for the requested uncensored model, with integer
usage. The key was used locally only; it is not provisioned to serving clouds.

The proposed grant is one calendar month of Pro, 20,000 credits (about USD 20),
with no automatic renewal or charge. It has not been confirmed or applied.
The [public terms](https://maple.ai/terms), sections 3.4-3.5, describe renewal
and trial conversion; section 5.4 restricts competing services. Obtain explicit
confirmation of the pilot exception and TrustedRouter resale permission before
opening customer traffic. Do not start a paid subscription to simulate the grant.

The [privacy policy](https://maple.ai/privacy) distinguishes encrypted saved
conversations from API operational usage data. It is not, by itself, evidence
that this specific API route meets TR's strict ZDR or confidential requirements.
Keep all privacy badges off until the applicable policy and serving path pass review.

## Encrypted path

- Backend: `https://enclave.trymaple.ai`.
- Logical operations: `GET /v1/models`, `POST /v1/chat/completions`.
- Wire operations: `/v2/session` and encrypted `/v2/request`.
- Reviewed source reference: MaplePrivacyLabs/Maple commit
  `c668d817dceec5c2e7390de7c68a581c88ea2eea`.
- [Transport V2 specification](https://github.com/MaplePrivacyLabs/Maple/blob/c668d817dceec5c2e7390de7c68a581c88ea2eea/services/opensecret/docs/transport-v2-protocol.md).
- [Dedicated model routing and worker policy](https://github.com/MaplePrivacyLabs/Maple/blob/c668d817dceec5c2e7390de7c68a581c88ea2eea/services/opensecret/src/maple_inference.rs).

The companion gateway repository's `tools/maple-pilot` uses the pinned SDK for
qualification, not a new home-grown cryptographic implementation. Its fixed
PCR0 is a *candidate* from the pinned source's production history, not proof
that the live image reproduces that source. It does not fetch a mutable remote
allowlist. A successful handshake authenticates the front enclave and binds
session keys; it does not independently prove GPU/model identity or source
provenance. Reproduce the front enclave and review its enforced downstream i02
policy before promotion. The SDK's default remote-history auto-approval must
not be used in the production TR connector.

Maple's [published measurement approval](https://github.com/MaplePrivacyLabs/Maple/commit/942efea0bfcbb7523d320186404dcb4cd8f27c0a)
identifies backend source `7957ce78ad8eee7a173a9d877ff6e320876894c8` and
[release run 40226283053](https://github.com/MaplePrivacyLabs/Maple/actions/runs/40226283053)
for the candidate front measurement. This vendor record narrows the reproduction
target; it does not replace independent build verification. The SDK qualification
PR also has an unresolved dependency-scan range discrepancy documented in the
gateway repository; its security gate remains intact.

The proxy/SDK must run inside TR's measured environment. A host-side plaintext
proxy, direct bearer request, disabled verification, shared unscoped cache root,
or generic HTTP fallback is not an acceptable workaround. Namespace cached
prompts by TR workspace; the ordinary proxy uses one process-wide cache root
and therefore cannot simply be dropped into a multi-tenant gateway unchanged.

## Promotion checklist

1. Confirm the dedicated account identity and verify the non-renewing grant,
   reseller terms and API content-retention commitment.
2. Run positive and changed-PCR negative qualification, then reproduce the
   front enclave measurement and review the downstream dedicated worker pins.
3. Integrate the SDK within the measured image, with isolated cache roots,
   redacted errors, bounded streams, and no automatic inference replay.
4. Pass discovery, JSON/SSE output, real integer/cache usage, missing/wrong-key,
   model/plan errors, truncated/error streams and debit reconciliation tests.
5. Add cloud-local secrets and release through GCP/AWS/Azure CI, trust and
   coordinated rollout gates, preserving a healthy cloud. Test every serving region.
6. Only then add the priced live manifest and gateway eligibility. Attach
   evidence before publishing privacy, residency, vision or tool-support claims.

This onboarding change neither provisions secrets nor deploys a serving adapter.
