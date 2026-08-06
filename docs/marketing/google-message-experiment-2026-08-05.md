# Google Message Experiment: August 2026

## Objective

Find the words that produce a first successful API call, not merely a click or
signup. Run every variant in the same high-intent search campaign and ad group
so keyword intent, bids, geography, and landing-page experience remain fixed.

Primary event: `acquisition.first_successful_api_call`.

Secondary events: `acquisition.signup_completed` and
`acquisition.purchase_completed`.

## Current Signal

The July 21 through August 4 account results show:

- `openai_compatible_rsa1` is the only paid creative associated with an
  activated buyer: three engaged visitors, two signups, one activated user,
  one purchaser, and $5 in credited revenue. The sample is directional only.
- `openrouter_migration_rsa2` produced 81 engaged visitors and 11 signups, but
  no first successful API call.
- `router_privacy_rsa1` produced 69 engaged visitors and 10 signups, but no
  first successful API call.
- `message_test_10_rsa1` produced 73 engaged visitors and eight signups, but no
  first successful API call.
- `kimi_k3_rsa1` produced 208 engaged visitors and six signups after $493.29 in
  spend, but no first successful API call. It is paused.

The next round therefore tests concrete product promises. It does not add more
hot-model variants.

## Active Test

- Campaign: `Search | High Intent | 2026-07-25`
- Ad group: `OpenAI Compatible API`
- Landing page: `https://trustedrouter.com/openai-compatible-llm-api`
- Geography, keywords, bids, audience, schedule, and device settings: unchanged
- Each challenger uses one primary promise.
- Each ad has a unique `utm_content` value.
- Do not combine hooks inside one responsive ad.

The active ad group contains exactly three responsive search ads:

| Cell | `utm_content` | Primary promise | Status at launch |
|---|---|---|---|
| Control | `openai_compatible_rsa1` | One API for every model | Eligible |
| Ease | `oa_keep_sdk_r1` | Keep your OpenAI SDK and change one base URL | Under review |
| Breadth | `oa_every_model_r1` | Every model through one API | Eligible |

The ease challenger sends users directly to a runnable three-step quickstart.
Its CTA is `Create my API key`, with `No card required` next to the action. The
snippet uses the standard OpenAI package, reads the API key from the
environment, calls `trustedrouter/cheap`, and can be copied with one click.

The old `OpenRouter Migration` group keeps only `openrouter_migration_rsa2`
enabled as its historical control. `enterprise_platform_rsa1` and
`message_test_10_rsa1` are paused.

## Next Message Cells

| Cell | `utm_content` | Headline | Primary promise |
|---|---|---|---|
| Privacy | `msg_never_logged_r1` | Your Prompts Are Never Logged | No prompt or output storage |
| Reliability | `msg_provider_failover_r1` | Provider Failover Built In | Automatic independent-provider rollover |
| Price | `msg_five_percent_r1` | 5% Fee. No Subscription. | Transparent usage pricing |
| Verification | `msg_code_saw_prompt_r1` | Know What Code Saw Your Prompt | Live attestation of the open source gateway |

Run the cells in three waves:

1. Ease and breadth, currently active.
2. Privacy and reliability.
3. Price and verification.

Keep the same control ad in every wave. Pause the previous challengers before
enabling the next pair.

## Decision Rules

- Do not select a winner from CTR alone.
- Record directional results after 50 engaged visitors per cell.
- Make a promotion decision after 100 engaged visitors per cell.
- Pause a cell after 100 engaged visitors with no first successful API call.
- Promote a cell only when its activated-user rate beats the high-intent
  baseline and it has at least three activated users.
- Break ties by purchaser rate, then signup rate, then engaged-visit cost.
- TrustedRouter's metadata-only first-party funnel is the source of truth.

## Reporting

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --campaign message_words_20260805 \
  --days 30
```

Report spend, engaged visitors, signups, activated users, purchasers, and
credited purchases for every `utm_content` cell. Preserve money as integer
microdollars until display.
