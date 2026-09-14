# Catalog refresh repair: September 14, 2026

## Verified sources and causes

- **io.net:** its [payment documentation](https://io.net/docs/guides/payment/io-intelligence-payments)
  designates `GET https://api.intelligence.io.solutions/api/v1/models` as the
  current per-token price source. The authenticated response reports DeepSeek
  V4.1 Flash at $0.315 input, $0.1575 cached input, and $1.26 output per million
  tokens. The previous snapshot had $0.15/$0.003/$1.20. These are real upstream
  price changes, not a parser multiplication error. Exact endpoint/axis/value
  approvals unblock only those transitions; other spikes still fail.
- **NextBit:** `GET https://api.nextbit256.com/v1/models`, the provider's own
  priced catalog, reports `deepseek:v4-flash-0731` at $0.352/$0.012/$1.056.
  Previous prices were $0.16/$0.07/$0.30. Approvals are similarly exact.
- **Confidential.ai:** its [pricing page](https://confidential.ai/pricing) added
  a Status column. Parse the required price headers by name, reject duplicate
  headers, and continue intersecting prices with the authenticated catalog.
  Request-access models do not become available just because they have prices.
- **Thinking Machines:** the [model documentation](https://tinker-docs.thinkingmachines.ai/tinker/models/)
  added an HTML column and explicitly provides stable JSON feeds. Read
  [serverless.json](https://tinker-docs.thinkingmachines.ai/tinker/serverless.json)
  and [models.json](https://tinker-docs.thinkingmachines.ai/tinker/models.json),
  matching exact deployed IDs and named price fields, never training prices.
- **Upstage:** the [pricing page](https://www.upstage.ai/pricing/api) now uses
  named `data-rate` fields and dated promotion schedules. Solar Pro 4 is
  $0.09/$0.018/$0.36 until October 10 at 00:00 UTC, then its published regular
  rate is $0.30/$0.06/$1.20. A manifest's `pricing_valid_until` can shorten,
  never extend, the usual 14-day validity limit. Expired promotions fail closed.
- **Reka:** its [Markdown feed](https://docs.reka.ai/pricing.md) changed model
  labels from HTML bold to Markdown bold. Preserve exact model-name matching
  with both representations; do not confuse research request pricing with
  chat token pricing or automatically assign old prices to new model versions.
- **Sail Research:** the [ASAP pricing table](https://docs.sailresearch.com/pricing)
  lists new models outside the parser's old fixed list. Discover canonical IDs
  from the named table rows. Only synchronous ASAP prices qualify; flex-only
  models remain excluded. Five new routes passed the existing live canaries.
- **Novita:** its [pricing page](https://novita.ai/pricing) lags the authenticated
  `/openai/v1/models` catalog for `zai-org/glm-5.3-p`. The API supplies
  $1.4/$0.26/$4.4 per million tokens. Preserve its existing scale of 100
  microdollars per manifest unit. API-only launches must keep refreshing after
  first publication, rather than retaining stale prices once they are known.
- **Featherless:** [DeepSeek V4.1 Flash](https://featherless.ai/models/deepseek-ai/DeepSeek-V4.1-Flash)
  was already discoverable but absent from the last published manifest while
  the global refresh gate was blocked. Current-plan discovery reports 262,144
  context, 32,768 output, $0.30/$0.03/$1.20. Its task list correctly advertises
  image-text-to-text even though legacy vision fields say otherwise. A real
  image request returned the expected color, and the text probe returned PONG.

Prices above are upstream USD per million tokens in input/cached-input/output
order, before TrustedRouter's customer markup. No privacy or attestation
classification was changed by this repair.

## Operational boundaries

The 11 runtime-only provider manifests were refreshed using their locally
authorized credentials. Their keys remain excluded from CI, and their existing
age-based route quarantine remains active. Do not solve future staleness by
granting these credentials to the hourly GitHub workflow.

Crusoe's credential returned 401 during the audit. Krea's earlier paid-path
canary returned 402. io.net's MiniMax M2.7 route still failed its canary with
402, but direct DeepSeek V4.1 Flash, GLM 5.3 and GPT-OSS probes returned 200.
SambaNova's M3 canary returned 429. Failed routes remain dark; catalog reads or
HTTP 200 alone do not justify clearing their existing canary holds.

The anonymous November 2 retirement notice has no identified sender. It must
not become a global or guessed provider retirement rule.
