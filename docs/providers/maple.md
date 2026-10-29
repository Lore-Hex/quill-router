# Maple confidential inference

The managed route uses the measured Transport V2 adapter with fixed front-enclave
and dedicated-worker policy. Production activation requires qualification in
every admitted GCP, AWS and Azure gateway region; preparing a catalog or secret
does not satisfy that requirement. Release evidence and regional completion
checks are tracked in [gateway PR #624](https://github.com/Lore-Hex/quill-cloud-proxy/pull/624)
and [activation PR #2162](https://github.com/Lore-Hex/quill-router/pull/2162).
Provider slug `maple`, legal name Maple Privacy Labs Inc. Maple must never be
added to `OPENAI_COMPATIBLE_PROVIDERS`: its managed Credits adapter uses
Transport V2, not bearer-only OpenAI HTTP.

## Initial model

| TR ID | Native ID | Input USD/M | Cached input USD/M | Output USD/M |
| --- | --- | ---: | ---: | ---: |
| `dealignai/deepseek-v4.1-flash-1017-uncensored` | `deepseek-v4-1-flash-1017-uncensored` | 0.65 | 0.13 | 1.45 |

The canonical ID names the author of the immutable weights in the reviewed
i02 deployment, not the inference host. It does not alias stock DeepSeek.
Only this exact model/native-ID pair under the managed credential qualifies;
unknown models, alternate native IDs and BYOK cannot inherit its privacy claims.

Prices were checked against <https://maple.ai/pricing> on 2026-10-28.
The [model documentation](https://maple.ai/docs/proxy/models) advertises a
1,048,576-token shared context and image/tool/reasoning support. The first pilot
is text Chat Completions only; advertised capabilities are not live-test results.
Do not merge this checkpoint into a stock DeepSeek alias or automatically route
it through a different model. At the source revision below it uses Maple i02;
stock `deepseek-v4-1-flash` can use shared Tinfoil as well as Maple i01.

The operator instructed shipping on October 28 without waiting for an account
debit export. Charge actual reported tokens at the published rates above; do
not invent a customer credit-rounding fee or USD 0.001 minimum. The pilot offer
mentioned provider credit rounding, so actual provider-debit reconciliation
remains unverified. Complimentary credits are not a zero upstream cost. Cached
input receives the discounted rate only when reported as cached by the provider.
The manifest stores upstream cost, not retail. TR's existing 5.5% markup yields
retail USD/M rates of 0.68575 input, 0.13715 cached input and 1.52975 output.
No additional Maple-specific fixed fee or request minimum is added.

## Access and terms

Create a dedicated ordinary account at <https://app.maple.ai/> and put its
inference key in the operator keyfile as `MAPLE_API_KEY`. Share only the account
email or ID with `support@maple.ai`, never the key. The operator supplied
`MAPLE_API_KEY` after the initial check. Authenticated encrypted discovery and
JSON/SSE PONG probes passed for the requested uncensored model, with integer
usage. On October 29 separate key copies were provisioned to GCP Secret Manager
and AWS Secrets Manager (Dublin primary, Paris replica InSync), with read-back
verification. Azure bundle `8df3ba4381d74315b2d1c696e63e839d` was sealed/uploaded,
preserving all 83 prior entries and adding Maple. Azure releases must pin that
reviewed bundle (or a separately reviewed superset), attest the generated policy,
and publish the signed measurements before admitting traffic.

The dedicated account dashboard confirms Pro through November 28, 2026, with
no purchased extra credits. It does not show per-request debits or the remaining
included allowance. The operator confirmed resale permission and zero API
prompt/response retention on October 28, 2026.
The [public terms](https://maple.ai/terms), sections 3.4-3.5, describe renewal
and trial conversion; section 5.4 restricts competing services. The operator's
pilot confirmation is recorded above; do not start or renew a paid subscription.

The [privacy policy](https://maple.ai/privacy) distinguishes encrypted saved
conversations from API operational usage data. It is not, by itself, evidence
that this specific API route meets TR's strict ZDR or confidential requirements.
The candidate's scoped privacy metadata must not be deployed before the
reviewed serving path is qualified in every admitted gateway region.

Catalog admission distinguishes control-plane locations from gateway origins.
AWS deploys with native `TR_PRIMARY_REGION=eu-west-1` or `eu-west-3`, and Azure
uses `uaenorth`; catalog publication maps these to their already-qualified
namespaced gateway regions. The GCP control plane in `southamerica-east1`
publishes the qualified GCP fleet's catalog even though no gateway runs there.
These catalog-only mappings do not qualify new inference origins: authorization
still requires an explicit origin from the exact reviewed gateway allowlist.
Unknown regions, an empty qualified fleet, and stock/BYOK Maple remain excluded.

## Encrypted path

- Backend: `https://enclave.trymaple.ai`.
- Logical operations: `GET /v1/models`, `POST /v1/chat/completions`.
- Wire operations: `/v2/session` and encrypted `/v2/request`.
- Reviewed source reference: MaplePrivacyLabs/Maple commit
  `c668d817dceec5c2e7390de7c68a581c88ea2eea`.
- [Transport V2 specification](https://github.com/MaplePrivacyLabs/Maple/blob/c668d817dceec5c2e7390de7c68a581c88ea2eea/services/opensecret/docs/transport-v2-protocol.md).
- [Dedicated model routing and worker policy](https://github.com/MaplePrivacyLabs/Maple/blob/c668d817dceec5c2e7390de7c68a581c88ea2eea/services/opensecret/src/maple_inference.rs).

The companion gateway repository's `enclave-go/maple-transport` uses the pinned SDK for
qualification, not a new home-grown cryptographic implementation. Its fixed
PCR0 was independently reproduced as recorded below; it does not fetch a mutable
remote allowlist. The handshake authenticates the front enclave and binds session
keys. GPU/model identity additionally depends on the reviewed, enforced downstream
i02 policy, not just a successful front-enclave handshake. The SDK's default
remote-history auto-approval is not used in the production TR connector.

Maple's [published measurement approval](https://github.com/MaplePrivacyLabs/Maple/commit/942efea0bfcbb7523d320186404dcb4cd8f27c0a)
identifies backend source `7957ce78ad8eee7a173a9d877ff6e320876894c8` and
[release run 40226283053](https://github.com/MaplePrivacyLabs/Maple/actions/runs/40226283053)
for the pinned front measurement. This vendor record identified the reproduction
target; it did not replace independent build verification. The SDK uses a
dependency-only patch to remove an affected dependency version; its required
security scan passes without exclusions. Cloud-image and independent-source
checks remain mandatory for confidential activation and subsequent updates.

Independent front-image reproduction completed on October 29 using the exact
source and pinned Determinate Nix with sandbox fallback disabled. The entire
233,941,161-byte EIF matches SHA-256
`9702de6b825bb024d843fb840bf1c638ca7443bbd6ba49f93d14f6e6dfc60e23` and approved
PCR0 `ada74c7aa10cbde75f83d7ec674cc4fc4a4cf9cc555e367189fbec332dffa985c6082ae4647fd50242a590bf9cd1d3f0`.
All kernel/initramfs bytes and PCR1/PCR2 match too. The earlier discrepancy was
Nix silently falling back outside its sandbox. Full evidence, downstream i02
verification, and negative tests are recorded in gateway PR #624 and its
`docs/providers/maple.md`. Reproduction is not production rollout evidence.

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
   model/plan errors, truncated/error streams and normal billing-oracle tests.
   Track the unavailable provider debit export separately, as authorized above.
5. Add cloud-local secrets and release through GCP/AWS/Azure CI, trust and
   coordinated rollout gates, preserving a healthy cloud. Test every serving region.
6. Only then add the priced live manifest and gateway eligibility. Attach
   evidence before publishing privacy, residency, vision or tool-support claims.

The price parser now participates in the shared 14-day stale-manifest expiry
policy. Neither a successful price refresh nor a held manifest enables customer
traffic; the attested gateway rollout must qualify first.
