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

## Initial Proposal: OpenRouter Migration

This proposal is superseded by the version 2 test below. It is retained to
explain the experiment's original control and challenger.

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

## Active Experiment: Two-Arm Activation Test

The seven-day first-party cohort on August 24 showed that the OpenAI-compatible
page activated 4 of 26 engaged visitors, compared with 2 of 93 for the general
OpenRouter alternative page. The six-arm exploration spread too little traffic
across too many pages, so version 2 tests the strongest observed page against
one focused migration challenger.

The existing exact-intent ad keeps its approved destination and assets unchanged:

```text
https://trustedrouter.com/openrouter-alternative
  ?utm_source=google
  &utm_medium=cpc
  &utm_campaign=openrouter_alternative_exact
  &utm_term={keyword}
  &utm_content=search
```

TrustedRouter recognizes that campaign and assigns each visitor consistently
to one of two pages. The direct `/openrouter-alternative/experiment` entry point
remains available for future campaigns. Both paths preserve the complete query
string, so the campaign and ad creative remain measurable while the landing
path identifies the page arm. Assignment redirects are private and `no-store`,
which prevents a CDN from serving one visitor's arm to another visitor.

| Arm | Landing path | Promise |
|---|---|---|
| Control | `/openai-compatible-llm-api` | Keep the SDK and change one base URL |
| Challenger | `/openrouter-alternative/quickstart` | Switch from OpenRouter and make the first request |

Both arms put a working OpenAI SDK example and key-creation action above the
fold. The experiment therefore measures the migration framing rather than the
presence of a quickstart. The four exploratory promise pages remain available,
but receive no version 2 experiment traffic.

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
- Require at least 100 engaged visitors and 10 activated users in each arm
  before naming a winner, unless the 30-day stop is reached first.
- Compare the challenger to the OpenAI-compatible control with a two-sided 95%
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

Report the two-arm experiment together:

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --campaign openrouter_alternative_exact \
  --days 30
```

Google receives no signup, activation, or payment event from TrustedRouter.
This experiment is evaluated from TrustedRouter's metadata-only first-party
events. Prompt and output content never enter the report.
