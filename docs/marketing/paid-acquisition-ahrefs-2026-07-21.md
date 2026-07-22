# Paid Acquisition Research: Ahrefs

Date: 2026-07-21

Market: Google US

This report summarizes Ahrefs exports used to evaluate the first TrustedRouter
Google and X campaigns. Ahrefs is useful for competitor and keyword discovery.
Google Ads search terms and TrustedRouter first-party activation data must be
the source of truth once the campaigns have traffic.

## Decision Summary

The current ads have the right differentiator but several low-demand search
themes. Buy existing demand for routers and gateways, then explain provable
privacy in the ad and landing page.

At a $10 daily Google budget, use a focused Search campaign before relying on
Performance Max. Performance Max currently has no meaningful conversion signal
and can spread a small budget across weak inventory.

Recommended primary query families:

1. `llm router`
2. `ai router`
3. `llm gateway`
4. `ai gateway`
5. `openrouter alternative`
6. `openai compatible api`

Recommended privacy and compliance tests:

1. `confidential ai`
2. `ai data privacy`
3. `hipaa compliant llm`
4. `hipaa compliant ai`

Keep zero-retention and no-log language in the copy. Do not depend on those
exact phrases for search volume.

## Seed Keyword Results

| Keyword | US volume | KD | CPC | Recommendation |
|---|---:|---:|---:|---|
| `llm router` | 500 | 2 | $3.00 | Primary exact and phrase match |
| `ai router` | 400 | 28 | $0.50 | Test with network-router negatives |
| `glm api` | 300 | n/a | $0.60 | Model-specific ad group |
| `openrouter alternative` | 200 | 1 | n/a | Primary exact and phrase match |
| `openai compatible api` | 200 | 24 | $3.00 | Primary exact and phrase match |
| `deepseek api privacy` | 20 | n/a | n/a | Landing page and copy, not a core buy |
| `private llm api` | 10 | n/a | n/a | Landing page and copy, not a core buy |
| `aws bedrock alternative` | 10 | n/a | n/a | Small exact-match test only |
| `azure openai alternative` | 0 | n/a | n/a | Organic page, not paid priority |
| `no log llm api` | 0 | n/a | n/a | Differentiating copy, not a keyword |

## Broader Privacy Results

| Keyword | US volume | KD | CPC | Recommendation |
|---|---:|---:|---:|---|
| `ai gateway` | 1,600 | 50 | $7.00 | High-volume commercial test with a bid cap |
| `llm gateway` | 800 | 40 | $2.50 | Primary commercial test |
| `ai data privacy` | 600 | 58 | $0.80 | Content and low-cost audience test |
| `hipaa compliant ai` | 600 | 53 | $7.00 | Exact match only; requires careful claims |
| `confidential ai` | 350 | 36 | $5.00 | Strongest privacy-positioning query |
| `hipaa compliant llm` | 200 | 2 | $4.00 | High-intent exact and phrase match |
| `gdpr compliant ai` | 150 | n/a | n/a | EU-focused page and exact test |
| `confidential computing ai` | 100 | n/a | n/a | Technical buyer content |
| `zero data retention ai` | 20 | n/a | n/a | Copy and organic page |
| `soc 2 ai` | 20 | n/a | n/a | Procurement content only |
| `end to end encrypted ai` | 10 | n/a | n/a | Copy and organic page |
| `private ai api` | 10 | n/a | n/a | Copy and organic page |
| `privacy preserving llm` | 0 | n/a | n/a | Research language, not paid demand |
| `secure llm api` | 0 | n/a | n/a | Copy, not a keyword |

## Competitor Findings

Ahrefs detected no current US paid keywords or ads for `openrouter.ai` and no
ads for `portkey.ai` in the six-month ads view. This does not prove that the
companies never advertise. Ahrefs is a sampled third-party index and can miss
paid activity.

Organic US snapshot:

| Domain | Ranking keywords | Estimated traffic | Main acquisition pattern |
|---|---:|---:|---|
| `openrouter.ai` | 1,594 | 243,764 | Brand, app pages, model pages, quickstart, free models, rankings |
| `litellm.ai` | 224 | 3,739 | Brand, provider documentation, gateway terminology |
| `portkey.ai` | 386 | 3,424 | Brand, gateway education, MCP and enterprise content |
| `helicone.ai` | 290 | 2,167 | Brand and programmatic model pricing pages |

OpenRouter's estimated traffic is not one generic landing page. About 75% is
from OpenRouter-branded queries, 12% from app pages, 8% from model pages, 2%
from the quickstart, 1.4% from free-model collections, and 0.9% from rankings.
The percentages can overlap when a query and URL fit more than one group.

The repeatable lessons for TrustedRouter are:

1. Keep every model page current and indexable.
2. Build high-quality quickstart and migration pages.
3. Publish measured comparison, pricing, latency, and ranking pages.
4. Add real integration pages when an integration works.
5. Use programmatic pages only when each page contains distinct, current data.

## Google Campaign Structure

Create a focused Search campaign with these ad groups:

### Router And Gateway

Keywords: `llm router`, `ai router`, `llm gateway`, `ai gateway`, and
`openai compatible api`.

Landing pages: `/best-llm-router`, `/llm-failover`, and
`/openai-compatible-llm-api`.

### OpenRouter Alternative

Keywords: `openrouter alternative`, `open router api`, and migration variants.

Landing pages: `/openrouter-alternative`, `/compare/openrouter`, and
`/docs/migrate-from-openrouter`.

### Confidential AI

Keywords: `confidential ai`, `ai data privacy`, and
`confidential computing ai`.

Landing pages: `/private-llm-api`, `/no-log-llm-api`,
`/confidential-computing-llm`, and `/security`.

### Compliance

Keywords: `hipaa compliant llm`, `hipaa compliant ai`, and
`gdpr compliant ai`.

Landing pages: `/hipaa-llm-api`, `/gdpr-compliant-llm-api`, and the relevant
readiness pages. Ads must say readiness rather than certification until an
audit or certification is complete.

Start with exact and phrase match. Add negatives for `wifi`, `wireless`,
`modem`, `router hardware`, `cisco`, `tp-link`, `home network`, `vpn`, and
other network-router intent. Inspect the Google search-terms report at least
twice per week during the first month.

## Ad Message Tests

Use at least three independent messages instead of one all-purpose ad:

1. **Every model. Provable privacy.** One OpenAI-compatible API with an
   attested, open-source prompt path.
2. **Switch from OpenRouter in one line.** Keep the SDK and change the base
   URL. No prompt or output logs by default.
3. **One provider can go down. Your API should not.** Route across providers
   with measured fallback and public status data.

For X, keep the current privacy creative as the control and add migration and
reliability variants. One creative cannot distinguish message quality from
audience quality.

## Measurement Required Before Scaling

Capture the following first-party attribution fields:

- `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`
- `gclid`, `gbraid`, `wbraid`, and `twclid`
- first landing page and first-touch timestamp
- last-touch source before signup

Persist attribution through these product events:

1. `sign_in_opened`
2. `signup_completed`
3. `api_key_created`
4. `first_successful_api_call`
5. `credit_purchase_completed`
6. `retained_api_usage_7d`

Do not send prompts, outputs, API keys, BYOK secrets, or request bodies to an
ad platform. Use consented click identifiers and aggregate product events.

The first Google primary conversion should be `signup_completed`. Move the
primary optimization event to `first_successful_api_call` or a credit purchase
only after there is enough volume for the bidding system to learn. Report both
CAC and activated CAC; signup CAC alone is easy to make look good.

## Raw Inputs

The source CSVs were downloaded locally from Ahrefs on 2026-07-21:

- `openrouter.ai` organic keywords, US
- `portkey.ai` organic keywords, US
- `helicone.ai` organic keywords, US
- `litellm.ai` organic keywords, US
- initial TrustedRouter campaign seed keywords, Google US
- privacy and compliance seed keywords, Google US

Raw Ahrefs exports are not committed to this public repository.
