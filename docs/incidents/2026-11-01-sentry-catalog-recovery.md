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

The missing-tariff regression reproduced the original exception before the fix.
Both Databricks retirement regressions failed before the retirement rule.
Focused pricing, discovery, route, and recovery tests: 120 passed, one live-provider
health test excluded by the normal release marker. Full local gates, exact-head
CI, guarded deployment, and production verification must complete before this
incident can be described as repaired in production. No Sentry issue has been
muted or resolved to substitute for those checks.

Diagnostics used selected Sentry metadata and bounded synthetic-only ClickHouse
queries with a 200-result limit, one-million-row read ceiling, 15-second execution
limit, and 256 MiB memory cap. No billing-store body scan, customer prompt/output,
credential publication, or production data mutation was performed.
