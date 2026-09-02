# Google Search Experiment V3

## Goal

Find messages that cause a real developer to make a successful API call and
eventually purchase credits. Click-through rate and signup rate are diagnostics,
not the optimization target.

## Candidate Catalog

The deterministic catalog contains 384 cells:

| Axis | Values |
|---|---:|
| Audience | 6 |
| Product promise | 8 |
| Evidence | 4 |
| Call to action | 2 |

Every cell has an immutable `experiment_id` and `cell_id`. Its final URL carries
the same identity in `tr_exp`, `tr_cell`, and `utm_content`. TrustedRouter
validates the IDs, stores them in the encrypted first-party attribution cookie,
and carries them through signup, first successful API call, checkout, payment
method save, settled purchase, and seven-day retained API use.

Generate the complete catalog:

```bash
uv run python scripts/build_google_ads_experiment_matrix.py > /tmp/google-cells.csv
```

Generate one controlled wave:

```bash
uv run python scripts/build_google_ads_experiment_matrix.py --wave 0 \
  > /tmp/google-wave-00.csv
```

## Exposure Policy

Only four cells run at once. The 384 candidates span 96 non-overlapping waves.
Each pair of waves covers all eight promises, and every promise eventually
visits every audience, evidence, and CTA combination exactly once.

Four simultaneous cells are already aggressive at the current traffic level.
Running hundreds simultaneously at a $100 daily budget would give most cells
too little traffic to distinguish product signal from chance. The catalog is
large; exposure remains controlled.

Advance a wave only after each cell has one of:

1. 100 mature engaged visitors and at least 10 activations.
2. 100 mature engaged visitors with zero activations, which retires the cell.
3. A 30-day maximum run with enough evidence to retain or retire it.

Keep one winning control in the next comparison. Rebuild the next four-cell
wave around the control after the broad screening stage identifies one.

## Cohort Measurement

The report uses acquisition cohorts instead of independent event windows. A
person enters a cell at their first eligible `acquisition.landing_engaged`
event. Every later milestone is assigned to that original cell, even when the
signup or purchase occurs after the acquisition window or after another site
visit.

The default report excludes the newest seven acquisition days while continuing
to observe their later conversions. This reduces right-censoring, where a new
visitor has not had enough time to activate or buy.

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --experiment-id google_search_messages_v3 \
  --days 30 \
  --cohort-lag-days 7
```

Inspect one cell:

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --experiment-id google_search_messages_v3 \
  --experiment-cell-id g3_or_migrate_attest_key \
  --days 30 \
  --cohort-lag-days 7
```

## Spend And Revenue

Google Ads reporting is queried at day, campaign, ad group, and ad ID. Static
final URL attribution maps cost in integer microdollars to the exact experiment
cell. Filtering a funnel report also filters native spend to matching campaign,
creative, or experiment IDs. A server-side landing split cannot know the exact
cost of each individual click, so it reports campaign spend rather than
inventing per-cell cost.

Native spend requires these operator settings:

```text
TR_GOOGLE_ADS_REPORTING_CUSTOMER_ID
TR_GOOGLE_ADS_DEVELOPER_TOKEN
TR_GOOGLE_ADS_REPORTING_LOGIN_CUSTOMER_ID  # only for a manager account
TR_GOOGLE_ADS_REPORTING_TIME_ZONE
```

The current private key file does not contain the customer ID or developer
token. Until they are configured, CAC and ROAS remain withheld. First-party
activation and purchase attribution still works.

## Decision Rules

- Primary early metric: activated users per mature engaged visitor.
- Primary business metric: settled purchasers and revenue per mature engaged visitor.
- Report 95% Wilson intervals for activation and purchase rates; a point estimate
  from a small cell is never treated as a winner.
- Cost metric: integer-microdollar CAC by exact ad cell when native spend is available.
- Guardrails: page errors, signup completion, and first-request success.
- Break ties by purchaser rate, activation rate, then activation CAC.
- Never choose a winner from CTR alone.
- Never compare an immature cohort with a mature cohort as if exposure were equal.
- Never infer an individual headline winner from a mixed responsive-search ad.

Google documents responsive search ads as rotating headline and description
assets into combinations. Asset and combination reports are useful diagnostics,
but an isolated ad with one primary promise and one immutable final URL is the
measurement unit for TrustedRouter's full-funnel tests.

## Privacy Boundary

The experiment stores public campaign metadata, pseudonymous fingerprints,
milestone timestamps, and integer purchase amounts. It does not read or store
prompts, model outputs, account emails, API keys, or payment credentials.
