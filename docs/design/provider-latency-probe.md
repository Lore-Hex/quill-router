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
`source` field (`organic` | `synthetic` | `synthetic_throughput`). Indexed in
Bigtable by provider and provider#model.

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

### Sustained throughput probe
`provider_throughput_probe()` measures real post-first-token decode speed rather
than dividing token count by total request latency. A deterministic selector
chooses 200 routes: every active chat provider receives one slot, models used by
TrustedRouter aliases and orchestration presets rank first, recent launches get
a bounded bonus, and measured provider rank breaks ties.

An isolated US throughput job runs one sustained route every two minutes with a
512-token cap. That is 3.6 samples per route per day, roughly 25 per week and
108 per 30 days. The ordinary US/EU monitor jobs never run this long probe, so
slow streams cannot delay health, billing, fallback, TLS, or attestation checks.
The probe requires final provider usage, discards response bytes, and persists
only token counts, timing, route, finish reason, and calculated cost. These
samples use `source="synthetic_throughput"` and contribute only throughput:
they cannot change uptime, TTFT, API drift, route-health alerts, or app usage.

### Aggregation + surfaces
`synthetic/leaderboard.py` aggregates organic and short synthetic samples for
availability/latency. When sustained samples exist for a route, their median
replaces the older end-to-end token-rate estimate. `source` is not surfaced
publicly. All pages remain behind short caches (no per-view store scan):
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
The short random probe remains mostly `max_tokens=16`. The sustained set runs
in its own Cloud Run Job with a 512-token cap and one US request per two-minute
invocation. A deterministic CI test prices all 200 selected routes at their
full cap and enforces a reviewed $75/month upstream-token ceiling; the July
2026 catalog projects about $52/month. The single small Cloud Run task adds a
minor compute charge independent of provider token spend.

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
