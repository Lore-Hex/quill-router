# Google exact-intent search campaigns: August 2026

## Objective

Separate four distinct buyer intents so the keyword, ad promise, landing page,
and first-party conversion record all describe the same job. Optimize from
TrustedRouter's metadata-only funnel, with `first_successful_api_call` as the
primary event and paid workspace plus seven-day retained use as confirmation.

The launch budget remains $300 per day in total. Create the four campaigns in a
paused state, pause the superseded broad campaign, then enable these budgets:

| Campaign | Daily budget | Landing page | First-party campaign ID |
|---|---:|---|---|
| Search · OpenRouter Migration · 2026-08-09 | $90 | `/openrouter-alternative` | `exact_openrouter_migration_20260809` |
| Search · Private LLM API · 2026-08-09 | $90 | `/private-llm-api` | `exact_private_llm_20260809` |
| Search · Provider Failover · 2026-08-09 | $90 | `/llm-failover` | `exact_provider_failover_20260809` |
| Search · Hot Model APIs · 2026-08-09 | $30 | `/latest-model-apis` | `exact_hot_models_20260809` |

The hot-model budget stays smallest because the previous Kimi creative produced
signups but no first successful API calls after substantial spend.

## Shared controls

- Search only, United States, English.
- Exact and phrase match only. Search partners and Display expansion off.
- Manual CPC or Maximize Clicks with a $5 initial CPC ceiling.
- One responsive search ad per campaign for the first controlled round.
- No Google behavioral tag or customer-data upload. Attribution remains
  first-party and metadata-only.
- Final URL parameters:

```text
utm_source=google&utm_medium=paid_search&utm_campaign=<campaign_id>&utm_content=rsa1&utm_term={keyword}
```

- Shared negatives: `jobs`, `career`, `salary`, `course`, `tutorial`, `torrent`,
  `download weights`, `huggingface download`, `jailbreak`, `crack`, `free account`,
  and `consumer chat`.

## OpenRouter migration

Keywords:

```text
[openrouter alternative]
"openrouter alternative"
[openrouter migration]
"switch from openrouter"
[openrouter api alternative]
"openrouter pricing alternative"
```

Headlines:

```text
Switch From OpenRouter
OpenRouter Alternative
Keep Your OpenAI SDK
Change One Base URL
5.5% Platform Fee
No Subscription Required
No Prompt Or Output Logs
Automatic Provider Failover
Open Source AI Router
Privacy With Proof
Hundreds Of Models
Start In Minutes
```

Descriptions:

```text
Keep your SDK and model IDs. Change one base URL to migrate from OpenRouter.
Route hundreds of models through an attested open source gateway with zero content logs.
Automatic provider failover, a 5.5% fee, prepaid credits, and BYOK. No subscription.
Create a key, use your existing code, and run the first request with starter credit.
```

## Private LLM API

Keywords:

```text
[private llm api]
"private llm api"
[secure llm api]
"confidential ai api"
[zero retention llm api]
"zdr llm api"
[no log llm api]
```

Headlines:

```text
Private LLM API
Privacy With Proof
No Prompt Or Output Logs
Attested AI Gateway
Open Source Prompt Path
ZDR And TEE Routes
Secure Multi Model API
Claude GPT Gemini And More
Built For Sensitive Data
Verify The Running Code
Keep Content Out Of Logs
Start Without A Card
```

Descriptions:

```text
Route sensitive AI workloads through an attested gateway with no prompt or output logs.
Choose routes by upstream ZDR, verified TEE status, region, model, price, and speed.
Use one OpenAI-compatible API for Claude, GPT, Gemini, DeepSeek, Kimi, and more.
Open source prompt path, published image digest, live attestation, clear provider labels.
```

## Provider failover

Keywords:

```text
[llm failover]
"llm provider failover"
[ai api failover]
"multi provider llm api"
[llm redundancy]
"openai api failover"
"anthropic api failover"
```

Headlines:

```text
LLM Provider Failover
Automatic Model Rollover
Keep Serving Through Outages
Multi Provider LLM API
Built In Retries And Routing
One API Multiple Providers
Reduce LLM Provider Risk
OpenAI Compatible Gateway
Live Provider Benchmarks
Route Around 429s And 5xx
Hundreds Of Model Routes
Test Failover In Minutes
```

Descriptions:

```text
Retry eligible provider failures on another authorized route without changing application code.
Keep one OpenAI-compatible endpoint while TrustedRouter handles selection and rollover.
Compare measured latency and health before an outage forces you to choose another route.
Use exact models or trustedrouter/auto with transparent route metadata and integer billing.
```

## Hot model APIs

Keywords:

```text
[kimi k3 api]
"kimi k3 api"
[glm 5.2 api]
"glm-5.2 api"
[deepseek v4 api]
"deepseek v4 flash api"
[gemini 3.6 flash api]
"gemini 3.6 api"
```

Headlines:

```text
Kimi K3 API
GLM 5.2 API
DeepSeek V4 API
Gemini 3.6 Flash API
One API For Every Model
Try New Models In Minutes
Compare Cost And Speed
No Separate Provider Accounts
OpenAI Compatible API
Provider Fallback Included
Privacy Labels Per Route
Hundreds Of Current Models
```

Descriptions:

```text
Call Kimi K3, GLM 5.2, DeepSeek V4, and Gemini 3.6 Flash with one API key.
Keep the OpenAI SDK. Change the model string and compare results on your own prompts.
See providers, prices, context, privacy labels, latency, and health before you ship.
Use prepaid credits or BYOK. TrustedRouter keeps no prompt or output logs, always.
```

## Decision rules

- Do not choose winners from CTR alone.
- Review direction after 50 engaged landings per campaign.
- Pause after 100 engaged landings with zero first successful API calls.
- Scale only after at least three activated users and a better activated-user
  rate than the prior high-intent baseline.
- Break ties by payer rate, retained use, activation rate, then cost per engaged
  landing.
- Review search terms twice in the first week and add negatives before raising
  any budget.
