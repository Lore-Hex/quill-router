# Google Ads Landing Page Experiment

## Objective

Improve paid-search conversion through the complete first-party funnel:

1. engaged visit
2. signup
3. first successful API call
4. checkout
5. settled credit purchase

The experiment keeps the existing Google Ads budget. It reallocates traffic
away from weak hot-model cells and compares distinct landing promises under the
same search intent.

## Production Baseline

The 30-day first-party report on August 20, 2026 shows:

| Segment | Engaged | Signups | Activated | Buyers | Revenue |
|---|---:|---:|---:|---:|---:|
| OpenRouter migration | 185 | 23 | 3 | 0 | $0 |
| OpenAI compatible | 19 | 2 | 1 | 1 | $5 |
| OpenRouter privacy | 117 | 14 | 1 | 0 | $0 |
| Kimi K3 | 216 | 6 | 0 | 0 | $0 |

OpenRouter intent creates signup volume. The action-led OpenAI-compatible page
produces materially better activation and the only observed purchase. Kimi K3
traffic has high volume and weak downstream intent.

## Experiment 1: OpenRouter Migration

Use a Google Ads custom experiment with a 50/50 traffic split. Keep keywords,
negative keywords, bids, geography, schedule, devices, and responsive-search-ad
assets identical.

### Control

```text
https://trustedrouter.com/openrouter-alternative
  ?utm_source=google
  &utm_medium=paid_search
  &utm_campaign=openrouter_lp_20260820
  &utm_content=or_lp_proof_v1
```

### Challenger

```text
https://trustedrouter.com/openrouter-alternative/quickstart
  ?utm_source=google
  &utm_medium=paid_search
  &utm_campaign=openrouter_lp_20260820
  &utm_content=or_lp_quickstart_v1
```

The challenger preserves the OpenRouter promise but puts key creation, working
SDK code, and the three migration steps in the first screen. It is no-index and
canonicalized to the permanent OpenRouter page.

## Experiment 1B: Multi-Arm OpenRouter Landing Test

The first directional results favored action-led pages, but did not identify
which product promise drives the first successful API call. Send one unchanged
ad to the first-party experiment router:

```text
https://trustedrouter.com/openrouter-alternative/experiment
  ?utm_source=google
  &utm_medium=paid_search
  &utm_campaign=openrouter_lp_multi_20260822
  &utm_content=<unchanged-ad-creative-id>
```

The router assigns a visitor consistently to one of six pages. It preserves the
complete query string, so the ad creative remains measurable while the landing
path identifies the page arm.

| Arm | Landing path | Promise |
|---|---|---|
| Control | `/openrouter-alternative/quickstart` | Keep the SDK and switch the base URL |
| Breadth | `/openrouter-alternative/lp/every-model` | Hundreds of models behind one key |
| Reliability | `/openrouter-alternative/lp/provider-failover` | Automatic provider fallback |
| Privacy | `/openrouter-alternative/lp/privacy-with-proof` | Verify the prompt path |
| Price | `/openrouter-alternative/lp/usage-pricing` | Usage pricing without a subscription |
| Controls | `/openrouter-alternative/lp/production-controls` | Scoped keys, limits, and policy |

Every arm uses the same OpenAI SDK sample, key-creation flow, page structure,
and no-card-required reassurance. The promise, proof, and sample route vary.
All experiment pages are `noindex,follow` and canonicalize to the permanent
OpenRouter alternative page.

Assignment uses a one-way hash of the anonymous first-party attribution ID.
TrustedRouter does not store an additional experiment identity. Global Privacy
Control and Do Not Track requests receive the control page without an
attribution cookie.

## Experiment 2: Private LLM API

Start only after Experiment 1 has enough activation data. Use the same 50/50
structure for high-intent privacy queries.

### Control

```text
https://trustedrouter.com/private-llm-api
  ?utm_source=google
  &utm_medium=paid_search
  &utm_campaign=private_lp_20260820
  &utm_content=privacy_lp_proof_v1
```

### Challenger

```text
https://trustedrouter.com/private-llm-api/quickstart
  ?utm_source=google
  &utm_medium=paid_search
  &utm_campaign=private_lp_20260820
  &utm_content=privacy_lp_quickstart_v1
```

## Decision Rules

- Primary early metric: activated users per engaged visitor.
- Diagnostic metric: signups per engaged visitor.
- Business metric: purchasers and settled revenue per engaged visitor.
- Guardrail: the challenger must not reduce activation per engaged visitor.
- Do not optimize toward click-through rate or signup volume alone.
- Do not name a payment winner from one purchase.
- For the six-arm test, require at least 100 engaged visitors and three
  activated users in an arm before promoting its promise.
- Compare each challenger to the quickstart control with a two-sided 95%
  interval for activated users per engaged visitor.
- Keep the ad text, keywords, geography, device mix, bids, and schedule fixed
  during the landing-page test.
- Review after each arm has at least 20 activated users or after 30 days,
  whichever comes first.
- Pause an arm early only for a severe defect or when its 95% confidence
  interval excludes a practically useful result.

## Budget Reallocation

- Keep total Google Ads daily spend unchanged for the first experiment.
- Reduce or pause Kimi K3 acquisition traffic while it remains at zero
  activations from 216 engaged visitors.
- Move that budget into the OpenRouter experiment.
- Keep the existing OpenAI-compatible creative live as a reference cell because
  it produced the only observed paid conversion.

## Reporting

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --campaign openrouter_lp_20260820 \
  --days 30
```

Inspect one destination directly:

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --landing /openrouter-alternative/quickstart \
  --days 30
```

Report every multi-arm landing page together:

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --campaign openrouter_lp_multi_20260822 \
  --days 30
```

Google receives no signup, activation, or payment event from TrustedRouter.
This experiment is evaluated from TrustedRouter's metadata-only first-party
events. Prompt and output content never enter the report.
