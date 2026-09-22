# Catalog refresh repair: September 22, 2026

## Causes and fixes

- Refresh run [35669540025](https://github.com/Lore-Hex/quill-router/actions/runs/35669540025)
  stopped in Xiaomi's manifest writer. Its official
  [overseas pricing table](https://mimo.mi.com/docs/en-US/price/pay-as-you-go)
  now groups old/new model IDs and uses row spans to distinguish real-time
  and batch rates. The old parser missed the grouped IDs and could read the
  trailing batch row as a real-time price. Parse the inference type, row spans,
  and every explicit model ID; reject ambiguous flattened batch tables.
- Provider fetch failures already had per-provider recovery, but manifest
  exceptions escaped that boundary and stopped all providers. The dispatcher
  now restores the exact previous manifest after a failed/invalid write and
  removes that provider's fresh prices before existing stale recovery. Failed
  rollback remains fatal. Missing/unusable fallback still counts against the
  existing publication budget. No snapshot is written before this recovery.
- Grok's native discovery already returns Grok 4.7. A direct chat smoke
  returned HTTP 200 and PONG. Its unrelated multi-agent endpoint rejects
  chat completions and remains held by its canary; this is not a Grok 4.7
  discovery failure.
- SiliconFlow's current authenticated discovery returns 77 live models and
  27 exact price matches. Its earlier 401 did not reproduce in the current
  scheduled run or direct checks. No credential changes or broadened price
  family matching are justified by that transient observation.

## Verified price transitions

The repaired live refresh then reached the spike guard, which correctly held
these increases. Verified on September 22 UTC, in upstream USD per million:

| Provider/model | Input | Cached input | Output |
| --- | ---: | ---: | ---: |
| SiliconFlow Gemma 4 31B | 0.13 -> 0.75 | 0.25 unchanged | 0.40 -> 1.00 |
| Inceptron DeepSeek V4 Flash 0731 | 0.06 unchanged | 0.02 -> 0.04 | 0.30 unchanged |

Sources: [SiliconFlow pricing](https://www.siliconflow.com/pricing) and
Inceptron's authenticated `GET https://api.inceptron.io/v1/models`
(`pricing.input_cache_reads` is `0.00000004` dollars/token).
Approvals pin endpoint identity, price dimension, old value, and new value.
Mutation tests keep other endpoints and larger/unreviewed increases blocked.

## Verification and boundaries

The isolated live refresh completed; strict discovery coverage and the spike
guard passed with these exact approvals. Regression tests cover grouped aliases,
real-time versus batch, partial writes, malformed JSON, missing/empty manifests,
byte-for-byte restoration, log redaction, healthy-provider continuation,
last-known-good prices, unrecoverable failure budgets, and failed rollback.

Provider authentication/entitlement failures are not repaired by publication:
Crusoe still returned 401 and Krea's paid inference probe returned 402 during
the rehearsal. Existing route holds, manifest age limits, coverage checks,
price-spike limits, and deployment locks remain in force. Runtime-only provider
credentials were not granted to CI. Deployment evidence belongs in the PR;
this local rehearsal alone does not establish production rollout.
