# TrustedRouter Fusion DRACO Reproduction

TrustedRouter's Fusion work is developed in the open. This document is the
committed runbook for reproducing the OpenRouter-style Fusion experiment and the
summary of the first completed live run.

## Source Context

OpenRouter's June 2026 Fusion announcement reported that budget model panels can
beat individual frontier models on DRACO-style deep research tasks. The relevant
public sources are:

- OpenRouter Fusion announcement:
  https://openrouter.ai/blog/announcements/fusion-beats-frontier/
- DRACO dataset:
  https://huggingface.co/datasets/perplexity-ai/draco

## Model Policy

The first TrustedRouter reproduction deliberately avoids:

- Claude Fable
- Claude Opus
- GPT-5.5

The default run matrix uses cheaper panel and judge models:

- `fusion_tr_budget`: Gemini 3 Flash, Kimi K2.6, DeepSeek V4 Pro, synthesized by GLM 4.7.
- `fusion_tr_current`: Gemini 3 Flash, Kimi K2.7 Code, DeepSeek V4 Pro, synthesized by GLM 4.7.
- `fusion_tr_ultra_cheap`: DeepSeek V4 Flash, MiniMax M3, Mistral Small, synthesized by Mistral Small.
- `fusion_mythos_candidate_6`: GPT-5.5, Opus 4.8, Kimi K2.7 Code, GLM 5.2, MiniMax M3, and Gemini 3 Flash, synthesized by Opus 4.8.
- `fusion_mythos_candidate_7`: the same six-model panel plus Gemini 3.1 Pro Preview.
- Solo controls: DeepSeek V4 Pro, Kimi K2.6, Kimi K2.7 Code, Gemini 3 Flash, Mistral Small.

## Cost-First Development Loop

Run the $1 tuning estimate first:

```bash
uv run python scripts/fusion_micro_eval.py \
  --mode micro-hybrid \
  --max-cost-usd 1.00
```

Then estimate the pilot and full DRACO-style runs:

```bash
uv run python scripts/fusion_full_eval.py --pilot
uv run python scripts/fusion_full_eval.py --fetch-draco
```

As of 2026-06-14, using the checked-in TrustedRouter catalog:

- `micro-hybrid`: estimated `$0.823725`
- 10-task pilot: estimated `$1.6679`
- 100-task full plan: estimated `$16.679`

These estimates include model calls, live search-with-content budgeting, and
three judge passes for the full DRACO plan. They are intentionally conservative.

## Completed 100-Task Legacy Run

The first completed public DRACO run finished on 2026-06-14. It is useful for
testing the pipeline, but it is **not comparable** to OpenRouter's published
table because it used the first holistic judge path rather than DRACO
criterion-level scoring with Gemini 3.1 Pro Preview.

- Config: `fusion_tr_budget`
- Scoring mode: `holistic`
- Comparable to OpenRouter table: `false`
- Task count: `100`
- Successful latest task rows: `100`
- Latest failed task rows: `0`
- Mean score: `80.10`
- Median score: `85.0`
- Min score: `25.0`
- Max score: `98.0`
- Model calls: `700`
- Exa search calls: `100`
- Input tokens reported by providers: `3,055,034`
- Output tokens reported by providers: `1,118,277`
- Estimated model cost from returned usage and catalog pricing: `$3.86284`
- Exa reported search cost: `$0.70`
- Estimated measured total: `$4.56284`

The public summary artifact is
`docs/evals/fusion-draco-live-2026-06-14.json`. It contains task ids, aggregate
scores, costs, token counts, model ids, and truncation counts. It does not
contain prompts, retrieved source excerpts, panel answers, final answers, judge
rationales, API keys, or request bodies.

The original local JSONL had 101 physical rows because a transient 502 was
recorded before retry support was added. The task was retried successfully.
Published metrics deduplicate by task id and use the latest successful row.

## Exact-Reproduction Requirements

Before publishing a Fusion comparison, the harness must first reproduce the raw
solo numbers from OpenRouter's post within reasonable noise:

- `google/gemini-3-flash-preview`: OpenRouter reports `43.1`
- `moonshotai/kimi-k2.6`: OpenRouter reports `53.7`
- `deepseek/deepseek-v4-pro`: OpenRouter reports `60.3`

The reproduction runner now defaults to:

- Gemini 3.1 Pro Preview judge: `google/gemini-3.1-pro-preview`
- DRACO criterion-level scoring: `--scoring-mode criteria`
- Three independent judge passes
- Clean Exa query text that does not mention rubrics, answer keys, or benchmark
  artifacts

Do not use the legacy holistic result as a marketing score.

To run a bounded live pilot after the estimate passes, put
`EXA_API_KEY` and a restricted `TR_FUSION_EVAL_API_KEY` in
`~/.quill_cloud_keys.private`, then run:

```bash
uv run python scripts/fusion_live_eval.py \
  --task-count 3 \
  --config fusion_tr_budget \
  --budget-usd 5.00 \
  --execute
```

For the explicit high-cost Mythos-style experiment, use the non-financial DRACO
slice first:

```bash
uv run python scripts/fusion_live_eval.py \
  --task-filter non-financial \
  --task-count 10 \
  --config fusion_mythos_candidate_7 \
  --budget-usd 50.00 \
  --output artifacts/fusion-draco/mythos-candidate-7-non-financial.jsonl \
  --execute
```

Run `fusion_mythos_candidate_6` first if Gemini 3.1 Pro should be held out of
the panel and used only as the judge.

The live runner fetches the public DRACO rows from Hugging Face, blocks
benchmark/rubric hostnames from Exa search, runs the panel, synthesizes the
answer, and judges against the DRACO rubric. It writes JSONL under
`artifacts/`. By default those result rows include metadata and scores but not
model-generated text. Add `--include-content` only for private local debugging.

## Artifact Policy

Generated artifacts go under `artifacts/` and are ignored by git:

- `costs.json`
- `scores.json`
- `frontier.svg`
- `draco-costs.json`
- `draco-frontier.svg`
- `tasks.json`
- `live-results.jsonl`

Do not commit generated prompt or output content. Public score artifacts should
include aggregate metrics and task ids only unless a future benchmark explicitly
approves content publication.

## Search Policy

Use `EXA_API_KEY` from `~/.quill_cloud_keys.private` for live search execution.
The estimator never prints or reads the key value. Live execution must block
DRACO rubric and solution URLs from search/fetch so models cannot inspect the
grading rubric.

The default excluded domains are:

- `huggingface.co`
- `datasets-server.huggingface.co`
- `openrouter.ai`

## API Status

`trustedrouter/fusion` is cataloged as the public alias for this work. The live
API executes inside the attested gateway by running panel calls, a selectable
judge call, and a final synthesis call against concrete TrustedRouter models.
Control-plane routing still fails closed so it cannot silently degrade to
`trustedrouter/auto` or any single-model route outside the enclave.
