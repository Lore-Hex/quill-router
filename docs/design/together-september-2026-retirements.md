# Together serverless retirements: September 2026

The operator's Gemma email names September 15. Together's current
[official deprecation page](https://docs.together.ai/docs/deprecations),
verified September 14, lists September 14 for these four serverless routes:

| Native ID | Recommended replacement |
| --- | --- |
| `google/gemma-4-31B-it` | `zai-org/GLM-5.3-Flash` |
| `openai/gpt-oss-20b` | `Qwen/Qwen3.5-9B` |
| `thinkingmachines/Inkling-Small` | `zai-org/GLM-5.3-Flash` |
| `intfloat/multilingual-e5-large-instruct` | None listed |

Use the earlier published date, at 00:00 UTC because no exact time or zone
is given. This is a conservative routing cutoff, not a claim that the
provider stopped serving at that instant. Dedicated endpoints are outside
this serverless route policy. Other providers of the same models are unchanged;
replacement recommendations never become cross-model aliases.

## Live evidence

Authenticated `/v1/models` still listed all four retiring models. The serverless
endpoint feed still labeled Gemma, GPT-OSS 20B, and E5 `STARTED`. Small direct
calls returned HTTP 200 for all four, demonstrating that live feeds lag the
published transition. Sixteen-token chat probes proved acceptance, not useful
completion: they exhausted the small output cap. The lifecycle rule must remain
authoritative even when such a feed is stale or a refresh falls back to a
committed snapshot.

The recommended GLM 5.3 Flash route showed the inverse discrepancy: its endpoint
was `STOPPED`, but a 128-token request returned `PONG` with 17 input and 64 output
tokens. The priced model feed reports $0.15 input, $0.03 cached input, and $0.50
output per million tokens. Reuse the existing bounded expected-model probe for
this exact known feed gap. Do not publish all STOPPED or dedicated models.

Tests cover exact cutoff boundaries, native and canonical IDs, both Credits
and BYOK routes, preservation of other providers and model identity, stale
price refreshes, and stale STARTED/STOPPED feeds. Static embedding allowlists
apply the same lifecycle rule even when the dynamic manifest is absent or
malformed; they cannot re-authorize a retired embedding route.
