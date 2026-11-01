# November 1 Sentry catalog recovery

## Confirmed causes

The November 1 11:23 UTC read of the configured production Sentry project
returned the newest 100 unresolved issues. This is bounded triage, not a claim
that every historical issue was examined or resolved.

### Catalog refresh, stale catalog, and watchdog

Run [41282099348](https://github.com/Lore-Hex/quill-router/actions/runs/41282099348)
failed in `scripts.pricing.transcription.parse_price` before the language-model
catalog refresh ran. Mistral's pinned `voxtral-mini-2602` model page now reports
deprecation on October 31 and no longer publishes its tariff. Its successor is
a different model, not an interchangeable price source. The speech refresh also
holds the old Voxtral TTS tariff because the main price table lists TTS 3 instead.

Sources checked November 1:

- https://docs.mistral.ai/models/voxtral-mini-transcribe-26-02
- https://docs.mistral.ai/inference/pricing
- https://docs.mistral.ai/inference/model-lifecycle

Transcription previously aborted the entire refresh on any source failure, unlike
the existing language-model and speech refreshers. The correction groups work by
provider, stages each provider atomically, keeps failed sources' committed prices,
and reports holds to the existing held-price issue. A strict majority of sources
failing still publishes nothing and exits nonzero. The twofold price-change guard,
billing units, and parser rejection of ambiguous tariffs are unchanged. ECB feed
failure affects only EUR-denominated Privatemode tariffs. A partial refresh does
not advance the snapshot's `checked_on` date.

This repairs refresh isolation; it does not manufacture a fresh price for the
deprecated Mistral model or approve its replacement. Missing tariffs remain held
for review. No committed prices change in this patch.

A live public-source dry run against a temporary snapshot refreshed three of four
providers and held Mistral for its missing tariff. It exited successfully, kept the
October 26 `checked_on` date, and left the committed snapshot byte-for-byte
unchanged. The temporary Privatemode prices reflected the current ECB feed; they
were not published.

### Databricks Inkling

Production synthetic metadata recorded HTTP 404, endpoint `databricks-inkling`
does not exist. Databricks confirms PAYGO retirement on October 30:

- https://docs.databricks.com/aws/en/machine-learning/retired-models-policy
- https://docs.databricks.com/aws/en/release-notes/product/2026/october

An exact provider/model/native-ID retirement rule removes this route from
admission and synthetic rotation. Other providers' Inkling routes and other
Databricks models are unaffected. Suggested replacement models are informational,
not silent substitutions. The date-only notice follows the existing UTC-date
cutover convention.

### NVIDIA Llama 3.2 11B and ZRO MiniMax M3.1 Flash

At 11:34 UTC the authenticated NVIDIA catalog still listed the exact native
`meta/llama-3.2-11b-vision-instruct` ID. One direct canary at 11:35:43 UTC returned
404 with the same missing-function classification as production synthetic probes.
The canary used at most 64 output tokens, a 30-second timeout, and no retry. This
is an unavailable account/function route, not evidence of a global retirement.

At 11:34:46 UTC the authenticated ZRO catalog contained 14 IDs and no MiniMax ID.
The configured `minimax-m3.1-flash` probe had returned 403, explicitly rejecting
that model for the API key. Other ZRO routes had successful synthetic samples.

Only these two routes enter the existing `provider-canary-failed` state. Prices,
native IDs, manifest timestamps, and other models remain unchanged. When the
provider rediscovers the held route, existing canary gating requires a fresh
success before restoring it. Regressions exercise both failure and recovery.

## Still actionable, not cleared

- BytePlus and IO-net both reported catalog expiry on November 3 at
  12:05:14 UTC in the 14:01 UTC synthetic cycle. The failed guarded refresh
  preserved their October 20 manifests, correctly retaining the old deadlines.
  BytePlus's public pricing document now uses separate Standard, Flex, audio,
  video-input, and video-resolution tables. At 14:17:59 UTC, its unchanged parser
  failed because the old `Online inference (standard)` heading no longer exists.
  The parser repair accepts the observed new headers and USD-per-million units,
  still supports the legacy layout, and rejects ambiguous identities, prices,
  columns, units, and tiers. Only Standard non-audio and online no-video-input
  tariffs are selected; time-dependent rates remain excluded. Paid canary,
  discovery, and gateway-support gates are unchanged. Source:
  https://docs.byteplus.com/en/docs/modelark/model-pricing
- IO-net's authenticated `GET /api/v1/models` at 14:18:42 UTC reported
  `XiaomiMiMo/MiMo-V2.5` input `4e-7`, output `2e-6`, and cache read `2e-7`
  USD per token. Its first-party payment documentation identifies the models API
  as the current per-token tariff source:
  https://io.net/docs/guides/payment/io-intelligence-payments
  These are $0.40/$2.00/$0.20 per million, versus the stored
  $0.199/$0.638/$0.0995. Exact reviewed transitions cover the existing snapshot
  and native-manifest identities. Other prices and future changes remain subject
  to the unchanged twofold guard. Neither this approval nor the BytePlus parser
  repair manually changes committed prices or advances freshness timestamps.
  A successful normal refresh, publication CI, and deployment are still required.
- Inception (distinct from Inceptron) retained its October 19 catalog because
  Mercury 2.5's three prices increased fivefold and the refresh correctly held
  them for review. At November 1 13:31:02 UTC the authenticated
  `https://api.inceptionlabs.ai/v1/models` and the first-party
  `https://docs.inceptionlabs.ai/get-started/models` agreed on $0.20 input,
  $0.02 cached input, and $0.75 output per million tokens. The committed rates
  were $0.04, $0.004, and $0.15, respectively. A follow-up adds only those three
  exact provider/native-route/dimension/old/new transitions to the existing
  reviewed-tariff mechanism. Different values, identities, dimensions, or future
  increases still fail the unchanged twofold guard. Mercury 3's current launch
  promotion is not changed. This does not manually rewrite prices or refresh
  freshness timestamps: a subsequent normal refresh must still pass live
  discovery, canaries, publication CI, and deployment before the alert clears.
- Hixtube's fresh provider declaration still ends its pilot at November 2
  23:56:20 UTC. The refresh deliberately uses that real cutoff rather than a
  rolling freshness window. Do not extend it to silence an expiry alert;
  renewal requires a fresh provider declaration and successful canaries.
- Inceptron synthetic probes return HTTP 402 `prepaid_credit_exhausted` across
  multiple models, including at 11:23 UTC. Provider funding is required. No
  payment, credit top-up, quota bypass, or alert suppression was performed.
- Privatemode returns 401 because its attestation manifest differs from the
  reviewed pin. The fail-closed refusal is intentional. Source, policy, and
  encrypted-path review is still required before any authorized pin transition.
  This patch does not change trust policy or accept an observed runtime pin.
- Chutes Mistral Nemo returned a generic upstream 502 at 01:49 UTC. That alone
  does not establish its initiating cause or justify permanent retirement.
- The Maple route-health issue's last event at 03:01 UTC predates the completed
  gateway rollout and final paid acceptance. It is not new post-release failure
  evidence. No completed Maple release is repeated by this investigation.

## Verification scope

The first guarded refresh after #2239 passed the formerly crashing transcription
step but its generated commit `1de4926d38a784559b356a1258d08b0f899b7012`
failed CI: a third `decision-models` catalog entry required an origin-metadata
row, and a routing unit test assumed DeepSeek always outranked Anthropic despite
fresh measured evidence. Both failures reproduced unchanged under the CI
post-cutover clock. The follow-up documents Baseten/Base Labs' Qwen3.5-derived
namespace without inferring a country or confusing it with Decision Models IO,
and tests throughput ordering against two explicit, opposite rank maps. The
three-model metadata threshold, real ranking evidence, routing implementation,
and publication gate remain unchanged. The failed snapshot is not published.

The next guarded refresh, 41329832709, generated snapshot
`b3a0f042739f66b87b0dbecdd3baa146b49f71db` after #2241. BytePlus, IO-net,
and Inception all refreshed at November 1 17:10:26 UTC, including the exact
reviewed IO-net and Mercury 2.5 tariffs. Publication CI 41330231436 then found
another incomplete origin-metadata prefix: `reflection`, now represented by
`reflection/beam`, `reflection/beam-1.0`, and
`reflection/beam-1.0-501b-a23b` across provider manifests and the snapshot.
The existing three-model completeness check correctly withheld publication.
The follow-up adds a sourced Reflection AI model-developer entry and regressions
for those three aliases, without changing the threshold, canonicalization,
provider jurisdictions, inference-location policy, or prices. Reflection's
[first-party Beam announcement](https://reflection.ai/blog/introducing-beam)
identifies the developer; its [August 2026 privacy policy](https://reflection.ai/legal/privacy-policy)
provides a New York company address. This is model-developer metadata, not a
claim about where an inference request is executed. The failed snapshot is not
made publishable by a focused local pass; normal full CI and a fresh guarded
publication remain required.

The missing-tariff regression reproduced the original exception before the fix.
Both Databricks retirement regressions failed before the retirement rule.
Focused pricing, discovery, route, and recovery tests: 120 passed, one live-provider
health test excluded by the normal release marker. Full local gates, exact-head
CI, guarded deployment, and production verification must complete before this
incident can be described as repaired in production. No Sentry issue has been
muted or resolved to substitute for those checks.

The first PR CI run passed Ruff and mypy but rejected stale catalog change
history. The existing generator recorded exactly the three intended endpoint
removals; no gate was relaxed. Local suite runs interrupted before this complete
artifact update are not full-suite passes.

The next CI run exposed an existing MiniMax inventory assertion that still
required the rejected ZRO route. It now explicitly checks the exact native ID,
recoverable hold reason, and exclusion while preserving every other host's
native-ID and capability assertions. Neighboring route and recovery tests pass
under both the normal clock and CI's May 1, 2027 post-cutover clock.
The existing held-price workflow test also now requires all three summary files,
including transcription, while retaining its publication and alert-gate checks.

Diagnostics used selected Sentry metadata and bounded synthetic-only ClickHouse
queries with a 200-result limit, one-million-row read ceiling, 15-second execution
limit, and 256 MiB memory cap. No billing-store body scan, customer prompt/output,
credential publication, or production data mutation was performed.

## Post-publication follow-up

The normal refresh published `971d99c6cf7bf56342e8f482532e011b98c830b3`
after all 46 exact-source CI jobs passed. GCP rollout 41343091759 completed all
15 jobs at 21:33:52 UTC; normal reconciliation then completed Azure 41353092916
at 21:47:05 and AWS 41353083988 at 21:55:22. Independent serving-source,
image, readiness, traffic, and public catalog checks passed on all three clouds.
Public Mercury 2.5 and IO-net MiMo V2.5 tariffs match the reviewed upstream
rates with the existing markup. BytePlus, IO-net, and Inception catalog-expiry
issues last occurred at 20:01:46 in the bounded 22:02:48 Sentry read. This is
not a claim that every Sentry issue is resolved or that future recurrence is
impossible. No issue was muted or manually resolved.

### Streamlake MiMo V2.5 Pro

Sentry issue 157874336 reports the exact `MiMo-V2.5-Pro` route returning HTTP
400 with an upstream deprecation rejection. Bounded stored synthetic metadata
independently confirms this classification at 14:51, 15:25, 17:36, and 21:21 UTC,
including before the latest deployment. Streamlake's public model and pricing
pages still list this native ID:

- https://www.streamlake.ai/document/DOC/mh1gbfvrdn6hpbzxixv
- https://www.streamlake.ai/document/DOC/mgrnm4xm362hvp5wyce

The discrepancy does not establish a global permanent retirement. The committed
route was still routable, so ordinary catalog rediscovery did not require a new
canary. Put only this route into the existing `provider-canary-failed` state.
Its existing usage-bearing, message-bearing canary must succeed before recovery;
tests exercise both rejection and successful recovery. Prices, native IDs,
timestamps, alternative providers, and other Streamlake models are unchanged.
MiMo V2.6 is not silently substituted for the failed native ID.

### Chutes Mistral Nemo

The earlier isolated 502 now has corroboration: the authenticated TEE catalog
at 16:14 and 16:19 UTC contains 17 models and no Nemo native ID; bounded stored
synthetic metadata records further 502s at 19:24 and 21:09 UTC. The first fresh
catalog miss recorded `missing_since` but kept the route under the existing
two-miss delisting guard. That guard alone does not address confirmed runtime
unavailability while the grace period is open.

Add an exact `provider-route-unavailable` operator hold for
`unsloth/Mistral-Nemo-Instruct-2407-TEE`. This reuses Chutes' existing hold mechanism
and rejects stale snapshot reintroduction for both Credits and BYOK. Catalog
reappearance alone must not clear it; verified operator recovery is required.
The existing Qwen attestation hold, two-miss guard, tariffs, and trust policy
remain unchanged. The provider-side initiating cause of the 502s is not proven,
and this is neither permanent retirement nor evidence of a measurement mismatch.

### Price-refresh watchdog

The 21:43 UTC event for existing Cron issue 157278794 is distinct from catalog
publication failure. The hourly observer's most recent scheduled run was
41339697665 (19:02 UTC), followed by refresh-triggered 41343108333 (success
19:43:09). No 20:23 or 21:23 scheduled observer run appeared in the bounded
workflow listing. The normal observer was dispatched once as 41355399811 and
completed successfully at 22:06:46. This repairs the immediate observer execution
gap, not the unestablished cause of missed GitHub scheduling or proof of durable
Cron recovery. The monitor schedule, grace, alert logic, and durable ledger were
not weakened or edited. A subsequent read-only Sentry Cron check at 22:15:50
independently confirmed production status `ok` and the accepted 22:06 check-in,
while retaining the prior missed check-ins. This confirms immediate recovery,
not prevention of another scheduling gap.

All seven new route-hold regression cases failed before the follow-up changes;
the three retained Qwen hold cases passed. Full local gates, exact-head and
merged-source CI, normal deployment, and fresh production checks remain required
for these two additional route corrections.

The first follow-up PR CI post-cutover shard found a stale generated fastpath
alias corpus after Nemo lost its final routes. The unchanged generator removes
exactly seven Nemo spellings; canonicalization and the Go port are unchanged.
Its parity test remains mandatory. The first local full-suite run was stopped
to update that artifact and is not a full-suite pass; a fresh complete run and
exact-head CI are required on the corrected tree.
