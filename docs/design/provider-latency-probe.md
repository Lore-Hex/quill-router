# Design: Provider/Model Rotation Probe → Latency Dataset

**Status:** Proposed (design-only; no code, no spend yet)
**Author:** Claude (for founder review)
**Date:** 2026-06-04

## TL;DR

Add one extra synthetic probe per monitor pass that **rotates across
every provider and model**, sending a tiny streaming request and
recording **TTFB, TTFT, total latency, reachability, and cost**.
Over a day this accumulates hundreds of datapoints per provider and
steady coverage of nearly every prepaid-routable model.

Two consumers:

1. **Public latency leaderboard** — a `/status`-class page that ranks
   providers and models by *measured* TTFT / uptime / error-rate.
   Credibility + SEO (the thing Artificial Analysis monetizes).
2. **Measured routing** — replace the hand-typed static
   `_THROUGHPUT_RANK` in `routing.py` with live measured latency so
   `sort_by: latency` reflects reality, per provider *and* per model.

**Cost:** folds into the existing per-minute Cloud Run job (no new
infra). Upstream-token cost only: **~$10–15/month** realistically at
the current cadence; ~$3/mo floor at 1 probe/min; ~$130/mo absolute
worst case if output caps are mishandled.

---

## 1. Motivation & what it replaces

Three things are weak today:

- **`sort_by: latency` is a guess.** `routing.py` ranks providers by a
  hardcoded dict typed from intuition:
  ```python
  _THROUGHPUT_RANK = {"cerebras": 0, "gemini": 1, "together": 2,
                      "deepseek": 3, "kimi": 4, ...}
  ```
  It is provider-level only (no per-model granularity), never updates,
  and nobody re-measures it. A request asking to be routed to the
  "fastest" provider is trusting a static opinion.

- **`ttfb == total latency`.** The current pong probe
  (`openai_chat_pong_probe`) is a buffered `client.post(...)` that
  reads the whole body, then sets `ttfb_milliseconds = latency_ms`.
  There is no *time to first token* anywhere in the codebase
  (`grep ttft` → empty). TTFT is the number users actually feel.

- **We measure one model.** Every pong probe hits
  `settings.synthetic_monitor_model` (the cheap pool leader, currently
  DeepSeek V4 Flash). We have rich uptime data for *one* model and
  nothing systematic for the other 206.

The rotation probe fixes all three with one mechanism.

---

## 2. Where it hooks in (architecture)

The monitor already has the right shape — we extend it, not rebuild.

```
Cloud Scheduler (* * * * *)  ──run──▶  Cloud Run Job (synthetic.cli)
                                          │  runs_per_invocation = 2 passes, 30s apart
                                          ▼
                                  _one_probe_pass()
                                    ├─ run_synthetic_once()  (tls, attestation, pong×2)
                                    ├─ gateway_billing_probe
                                    ├─ gateway_fallback_probe
                                    └─ provider_rotation_probe   ◀── NEW
                                          │
                                          ▼
              POST /v1/internal/synthetic/samples  (existing ingest)
                                          ▼
              Bigtable: synthetic index + rollups (existing)
```

- **New probe:** `provider_rotation_probe()` in `synthetic/probes.py`,
  added to the `_one_probe_pass` fan-out in `synthetic/cli.py`.
- **No new job, no new scheduler.** One more `await` inside the pass
  that already runs every minute. At the current 2 passes/min (× 2
  monitor regions if both are deployed) that is up to **4 rotation
  samples/minute for free**.
- **Reuses the ingest + storage path verbatim.** `_sample(...)` already
  carries `provider`, `model`, `selected_provider`, `selected_model`,
  `latency_milliseconds`, `ttfb_milliseconds`, `cost_microdollars`,
  `output_match`. We populate `provider`/`model` with the rotated pick
  (today they're null for pong probes) and add **one field:
  `ttft_milliseconds`**.

---

## 3. Sampling strategy: two-stage + floor

The founder's instinct (uniform provider → uniform model within it) is
the right default and is actually *cheaper* than uniform-over-models:

- It gives **every provider equal airtime** regardless of catalog size
  — small providers (Together, DeepInfra: 1 model each) get the same
  attention as OpenAI (17). **~90 datapoints/provider/day at 1/min,
  ~360/day at 4/min.**
- It **dilutes the expensive crowd**: the 17 OpenAI + 9 Anthropic
  models (which include the $0.026/probe gpt-5.x-pro) each get only
  `1/16 · 1/n` probability, so the pricey tail is rarely sampled.

**Downside:** individual big-provider models get ~5 samples/day. If we
care about per-*model* accuracy at OpenAI/Anthropic, add a **coverage
floor**: maintain a "last sampled" timestamp per (provider, model) and,
with some probability, override the random pick with the
longest-unsampled model. Guarantees every model is hit at least once
every few hours while keeping the cost profile flat.

Selection pool = **prepaid-routable models only** (see §8). Pseudo:

```python
provider = rng.choice(providers_with_prepaid_models)
model    = rng.choice(prepaid_models[provider])   # or oldest-unsampled w.p. p_floor
# pin the route so we measure THAT provider, not TR's auto-route:
body["provider"] = {"only": [provider]}
```

Pinning `provider.only` is essential — otherwise TR's router might
serve the model from a different upstream and the datapoint is
mislabeled.

---

## 4. Measuring TTFB + TTFT (needs streaming)

The rotation probe must use `stream: true` and time the byte/token
arrivals against a single `perf_counter()` start:

| Metric | Definition | Captured at |
|---|---|---|
| **TTFB** | start → first HTTP response byte (headers) | `response.aiter_bytes()` first chunk / `response` ready |
| **TTFT** | start → first SSE chunk carrying a content delta | first `data:` line with non-empty `choices[].delta.content` |
| **total** | start → stream end (`[DONE]`) | loop exit |

Mechanics (httpx streaming):

```python
async with client.stream("POST", url, json=body, headers=h) as resp:
    t_ttfb = elapsed()                      # headers in
    async for line in resp.aiter_lines():
        if first content delta:
            t_ttft = elapsed(); break-ish    # keep draining to [DONE] for total
```

Notes:
- **Reasoning models** emit `reasoning_content` before `content`. For
  TTFT we should count the first *visible-output* token; optionally
  also record `reasoning_ttft` later. v1: first `content` delta.
- Some providers don't stream token-by-token (batch the whole thing in
  one chunk). That itself is a datapoint — TTFT≈total means "no real
  streaming," which is worth surfacing on the leaderboard.

---

## 5. Output-token discipline (the cost lever)

The $3 ↔ $33 spread is entirely reasoning models running to
`max_tokens=128`. For a *latency* probe we do not care whether the
answer is exactly "PONG" — only that tokens flowed. So:

- Rotation probe sets **`max_tokens: 16`** and does **not** assert
  content correctness (`output_match` left null/informational).
- Content-correctness stays the job of the existing dedicated pong
  probe on the monitor model.

This keeps the rotation probe at the bottom of the cost range
regardless of which model is drawn.

---

## 6. Storage & rollups

- **Index rows** already keyed for samples
  (`storage_gcp_synthetic_index.py`). Rotation samples carry
  `probe_type="provider_rotation"` so they're filterable.
- **Rollups** (`storage_gcp_synthetic_rollups.py`) currently bucket by
  component into TTFB histograms. Add a **per-(provider, model) rollup
  family** with `ttft_histogram` alongside the existing
  `ttfb_histogram`, plus up/down counts → uptime% and error-rate. p50 /
  p95 via the existing `percentile_from_histogram`.
- **Volume:** ~200–300 B/sample → ~13 MB/month extra at 4/min. Bigtable
  storage + writes for this are rounding error (<$0.20/mo).
- **Retention:** keep raw samples on the existing TTL; keep *rollups*
  long-term (they're tiny) so the leaderboard can show 30/90-day trends.

---

## 7. SLO isolation (don't poison paging)

Rotation probes deliberately hit cheap/flaky models that **will**
legitimately fail sometimes (rate limits, model deprecations, cold
starts). They must NOT drag the `router_core` / `provider_effective` /
`control_plane` SLOs or trip burn-rate alerts.

- New component/SLO class `provider_probe` (informational only, **no
  paging**) in `synthetic/components.py`
  (`API_PROBES` / `PROVIDER_EFFECTIVE_PROBES` sets).
- The existing dedicated pong probe remains the SLO source of truth for
  router health.

---

## 8. Coverage reality

- **99 of 207 models are prepaid-routable**, across **16 of 22
  providers**. The monitor spends TR's own prepaid credits, so it can
  only cheaply probe prepaid models. BYOK-only providers (lightning,
  gmi, novita, minimax, nebius, tinfoil in the current snapshot) need
  provider keys wired into the monitor to be covered — out of scope for
  v1, tracked as a follow-up.
- So "nearly every model" honestly means "nearly every **prepaid**
  model." The leaderboard must label un-probed models as "not yet
  measured" rather than implying a score.

---

## 9. Consumer 1 — public latency leaderboard

A new public page (e.g. `/leaderboard` or a tab on `/status`) built
from the per-(provider, model) rollups:

- Columns: provider, model, **p50/p95 TTFT**, p50 total, **uptime%**,
  error-rate, samples (n), last-measured, "real streaming?" flag.
- Sort/filter by metric; toggle 24h / 7d / 30d windows.
- Per-provider summary row (its equal-airtime sampling makes this fair).
- SEO: this is evergreen, query-rich content ("fastest llama 3.3
  provider", "claude opus latency") — same playbook as the SEO landing
  pages. Reuses `_base.html` + OG card infra.
- **Honesty guardrails:** show n and confidence; mark low-sample models;
  measured from TR's monitor regions only (state which).

## 10. Consumer 2 — measured routing

Replace the static `_THROUGHPUT_RANK` lookup in
`_sort_candidates` / `_sort_endpoint_candidates`:

```python
# today:
sort_rank = _THROUGHPUT_RANK.get(model.provider, 50)
# proposed:
sort_rank = measured_latency_rank(model.provider, model.id)  # from rollups
            or _THROUGHPUT_RANK.get(model.provider, 50)        # fallback
```

- Source the rank from a **cached, periodically-refreshed** snapshot of
  the p50-TTFT rollups (NOT a live Bigtable read on the hot routing
  path — load into memory on a timer, like the price snapshot).
- Per-model granularity beats today's provider-level guess (a fast
  provider can still be slow for one big model).
- **Safety:** fall back to the static rank when a (provider, model) has
  too few samples; require a minimum n before trusting measured data.
  Keep the static dict as the floor so a cold cache never breaks
  routing.

---

## 11. Cost analysis (real catalog math)

Per-probe = `(20 × prompt_price + OUT × completion_price)` over all
prepaid models, weighted by the two-stage probability.

| Cadence | latency-only (max_tokens=16) | worst case (every probe → 128) |
|---|---|---|
| 1 probe/min (43.8k/mo) | **~$3/mo** | ~$33/mo |
| 2 passes/min (current) | ~$6/mo | ~$65/mo |
| 4/min (2 passes × 2 regions) | **~$12/mo** | ~$130/mo |

Two-stage is ~40% cheaper than uniform-over-models. Realistic figure
sits near the left column because non-reasoning models stop at ~2–5
output tokens regardless of the cap. **Infra adds ~$0** (folds into the
existing job; storage <$0.20/mo).

---

## 12. Phased PR plan

1. **PR 1 — probe + data field.** `provider_rotation_probe()`
   (streaming, TTFB/TTFT, `provider.only` pin, max_tokens=16); add
   `ttft_milliseconds` to `SyntheticProbeSample` + ingest; wire into
   `_one_probe_pass`; new `provider_probe` SLO class (no paging). Ship
   behind `TR_SYNTHETIC_ROTATION_ENABLED=false` so it's dark until we
   watch cost for a day.
2. **PR 2 — rollups.** Per-(provider, model) rollup family with
   ttft/ttfb histograms, uptime, error-rate; backfill helper.
3. **PR 3 — leaderboard page.** Public read-only view + SEO + OG card.
4. **PR 4 — measured routing.** In-memory rank snapshot + swap
   `_THROUGHPUT_RANK` to measured-with-fallback; min-sample guard.

Each PR independently revertable. PR 1 gated off by default → enable,
watch the real spend for 24h, then proceed.

---

## 13. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Cost surprise from reasoning models | max_tokens=16; dark-launch flag; watch 24h before scaling |
| Flaky models trip alerts | isolated `provider_probe` SLO class, no paging |
| Mislabeled datapoints | pin `provider.only`; record `selected_provider` and discard mismatches |
| Routing destabilized by thin data | min-sample guard + static fallback floor |
| Leaderboard implies scores for un-probed models | label "not yet measured"; show n |
| Provider ToS on synthetic traffic | low volume (≤4/min total), tagged `metadata.trustedrouter_synthetic` |

---

## 14. Open questions for review

1. **Cadence to start at** — 1/min (dark, cheapest) or fold into all
   existing passes immediately (~$12/mo)?
2. **Leaderboard home** — new `/leaderboard` page, or a section on the
   existing `/status`?
3. **Wire BYOK keys** for the 6 prepaid-gap providers so the dataset is
   truly catalog-complete, or accept prepaid-only for v1?
4. **Routing swap appetite** — change real routing behavior in this
   effort (PR 4), or land the dataset + leaderboard first and treat
   measured routing as a separate, later decision?
