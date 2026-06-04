# Design: Measured provider/model performance dataset

**Status:** Shipping (production-telemetry-first). This supersedes the original
"rotation probe" proposal — the key discovery during build was that we already
capture per-request performance data, so most of this is surfacing, not new
measurement.
**Updated:** 2026-06-04

## What this is

A measured-performance dataset across every provider and model TrustedRouter
routes to — p50/p95 **TTFT** (time to first token), **TTFB** (time to first
byte), throughput, uptime, and error rate — powering a public leaderboard,
per-model/provider pages, and (later) measured routing. Built on the data we
already collect; metadata only, never prompt/output content.

## Architecture (as built)

### Per-request capture (the spine)
Every production inference already writes a privacy-safe `ProviderBenchmarkSample`
(`storage_models.py`) from each generation **and** each provider error — no
tenant identifiers, no prompt/output. It carries `first_token_milliseconds`
(TTFT), `ttfb_milliseconds` (TTFB), `elapsed_milliseconds`, `status`,
`speed_tokens_per_second`, `error_type/status`, region, and an internal
`source` field (`organic` | `synthetic`). Indexed in Bigtable by provider and
provider#model.

### Synthetic rotation probe (coverage + drift)
`provider_rotation_probe()` (`synthetic/probes.py`) is "just a synthetic user":
each monitor pass it picks a random provider then a random model among that
provider's prepaid **endpoints** (two-stage; iterates ENDPOINTS, not the
`prepaid_available` dedup flag, so supplemental models are covered), streams a
tiny `max_tokens=16` request, and measures real TTFB + TTFT. It pins
`provider.only`, never asserts content, and emits a `ProviderBenchmarkSample(source="synthetic")`
to `POST /internal/synthetic/benchmark` — deliberately separate from the
`/status` router-health SLO. Dark-launched behind `TR_SYNTHETIC_ROTATION_ENABLED`
(+ `TR_SYNTHETIC_ROTATION_PER_PASS`).

### Aggregation + surfaces
`synthetic/leaderboard.py` aggregates samples (organic + synthetic combined;
`source` not surfaced publicly) into per-model and per-provider stats. Surfaced,
all behind short caches (no per-view store scan):
- **`/leaderboard`** — ranked providers + models by measured TTFT/TTFB/throughput/uptime.
- **`/models/{id}/performance`** — per-provider measured table for that model.
- **`/providers/{slug}`** — provider aggregate + per-model table.

### API-drift detection
`synthetic/drift.py` + `scripts/detect_provider_drift.py`: compares a recent
window vs a committed baseline and flags error spikes, new error shapes (a model
404ing = deprecation signal), and TTFT regressions. `--check` exits non-zero for
alerting; `--update-baseline` regenerates the committed baseline.

### Cited external benchmark scores
Separate from measured latency: `benchmark_scores.py` + `data/benchmark_scores.json`
show vendor/paper benchmark scores (SWE-bench, MMLU, …) on `/models/{id}/benchmarks`,
where OpenRouter's tab shows none-cited. Strict rule: a score renders only with a
real `source_url` + class A/B; ToS-restricted aggregators (Artificial Analysis,
LMArena, LiveBench) are link-only.

## Privacy
`ProviderBenchmarkSample` is tenant-free by construction; aggregates go to
provider/model only. Probe content is "reply exactly PONG". No content is ever
read by the probe or proxy. Consistent with the "0 prompt/output logs" promise.

## Cost
Upstream tokens only; folds into the existing per-minute monitor job (no new
infra). Two-stage random over prepaid endpoints, `max_tokens=16` → ~$10–30/mo at
the launch cadence. Dark-launch flag lets us watch real spend before ramping.

## Shipped in
PRs #34 (probe + TTFB/source), #35 (drift), #36 (aggregation), #37 (/leaderboard),
#38 (cited benchmark scores), #39 (measured model/provider pages).

## Not yet done / deferred
- Weekly **measured routing** snapshot replacing the static `_THROUGHPUT_RANK`
  (committed JSON, regenerated weekly; static fallback for low-sample entries).
- **`/apps`** usage leaderboard — needs a tenant-free app-usage pipeline
  (self-reported `X-Title` apps only); empty until callers send attribution.
- Automated ingestion of open benchmark feeds (Aider Polyglot YAML, BFCL,
  MMLU-Pro) to broaden cited-score coverage beyond the curated vendor spine.
