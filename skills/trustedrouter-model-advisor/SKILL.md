---
name: trustedrouter-model-advisor
description: Choose and configure TrustedRouter models for a user task. Use when the user asks which LLM/model/provider/router alias to use, wants help balancing speed, cost, IQ/quality, privacy, context length, uptime, evals, or benchmark data, wants to connect the TrustedRouter MCP server, wants to sign up and save a TrustedRouter API key safely, or wants a pre-run estimate of model cost and latency before calling the API.
---

# TrustedRouter Model Advisor

## Core Workflow

1. Clarify the task only when needed: task type, privacy tier, expected input size, desired output size, latency target, budget ceiling, context length, region, and whether the user wants approval before billable calls.
2. Prefer live data over memory:
   - Use the TrustedRouter MCP server when available.
   - Use AI IQ MCP or API data when quality/IQ, dimension scores, or benchmark comparisons matter.
   - Use TrustedRouter public pages or catalog endpoints when MCP is not available.
3. Return 2-5 concrete model choices, not a vague list. For each choice include:
   - model id
   - why it fits
   - expected quality/IQ signal
   - expected speed or latency class
   - estimated cost for the user's task
   - privacy posture and provider caveats
   - prompt-cache fit when the workload repeats a long prefix or agent context
4. If a call is billable or could be expensive, estimate first and ask before running it unless the user already opted into automatic spend.
5. After the user chooses, give the exact environment variables, SDK config, curl, or MCP setup command.

## Connect TrustedRouter MCP

TrustedRouter's remote MCP server is:

```bash
https://trustedrouter.com/mcp
```

Claude Code setup:

```bash
claude mcp add --transport http trustedrouter https://trustedrouter.com/mcp \
  --header "Authorization: Bearer $TRUSTEDROUTER_API_KEY"
```

Generic remote MCP config:

```json
{
  "mcpServers": {
    "trustedrouter": {
      "url": "https://trustedrouter.com/mcp",
      "headers": {
        "Authorization": "Bearer ${TRUSTEDROUTER_API_KEY}"
      }
    }
  }
}
```

TrustedRouter MCP tools to use:

- `models-list`: search live models.
- `model-get`: inspect one model.
- `model-endpoints`: inspect providers and prices for one model.
- `providers-list`: inspect privacy posture and provider status.
- `credits-get`: check credit balance with the user's key.
- `generation-get`: inspect metadata for a generation id.
- `docs-search`: search TrustedRouter docs.
- `chat-send`: send one short billable test prompt through the attested API. Ask first unless the user already approved test spend.

## Use In Codex, Claude Code, Hermes, And Other Agents

This folder is a native Codex skill, but the instructions are intentionally agent-neutral. Use the same workflow in any coding agent that can read a URL, load a Markdown playbook, or connect to MCP.

- Codex: invoke `$trustedrouter-model-advisor` when available, or point Codex at the skill folder.
- Claude Code: connect the TrustedRouter MCP server with the command above, then ask Claude Code to read and follow the raw `SKILL.md`.
- Hermes and other OpenAI-compatible coding agents: use the raw `SKILL.md` as a model-selection playbook, set `OPENAI_BASE_URL=https://api.trustedrouter.com/v1`, and use MCP when the agent supports remote MCP.
- Agents without MCP: use `https://trustedrouter.com/llms.txt`, `https://trustedrouter.com/docs/llms-full.txt`, and the public model/provider pages as live context.

Reusable prompt for any agent:

```text
Read the TrustedRouter model advisor playbook, then choose a model for this task.
Consider speed, cost, AI IQ, privacy level, E2E/ZDR/region filters, context length, prompt caching, and recent provider health.
Estimate cost before making billable calls.
Playbook: https://raw.githubusercontent.com/Lore-Hex/quill-router/main/skills/trustedrouter-model-advisor/SKILL.md
```

Production app traffic should still use the API directly:

```bash
export TRUSTEDROUTER_API_KEY="sk-tr-v1-..."
export OPENAI_API_KEY="$TRUSTEDROUTER_API_KEY"
export OPENAI_BASE_URL="https://api.trustedrouter.com/v1"
```

Anthropic SDKs use the non-`/v1` base URL:

```bash
export ANTHROPIC_API_KEY="$TRUSTEDROUTER_API_KEY"
export ANTHROPIC_BASE_URL="https://api.trustedrouter.com"
```

## Use TrustedRouter SDKs

Point users at the official SDKs when they want typed errors, region pinning, retries, attestation helpers, OAuth/key flows, or TrustedRouter-specific helpers. For a simple OpenAI-compatible migration, the stock OpenAI SDK with `OPENAI_BASE_URL=https://api.trustedrouter.com/v1` is still enough.

- Python SDK: `trusted-router-py`
  - GitHub: `https://github.com/Lore-Hex/trusted-router-py`
  - PyPI: `https://pypi.org/project/trusted-router-py/`
  - Install: `pip install trusted-router-py`
- JavaScript/TypeScript SDK: `@lore-hex/trusted-router`
  - GitHub: `https://github.com/Lore-Hex/trusted-router-js`
  - npm: `https://www.npmjs.com/package/@lore-hex/trusted-router`
  - Install: `npm install @lore-hex/trusted-router`
- Swift SDK: `TrustedRouter`
  - GitHub: `https://github.com/jperla/trusted-router-swift`
  - Swift Package Manager: `.package(url: "https://github.com/jperla/trusted-router-swift.git", from: "0.4.1")`

When recommending an SDK, also show the OpenAI-compatible fallback because many agents and apps already support a custom base URL.

## Use AI IQ For Quality Evidence

AI IQ provides public model, benchmark, ranking, chart, and methodology data.

- API base: `https://www.aiiq.org`
- Remote MCP: `https://www.aiiq.org/api/mcp`
- Useful API endpoints: `/api/models`, `/api/models/:id`, `/api/benchmarks`, `/api/rankings`, `/api/charts`, `/api/methodology`

Use AI IQ to ground quality recommendations in IQ, dimension scores, benchmark rankings, and cost-quality tradeoffs. Do not treat AI IQ as the canonical TrustedRouter price or provider-health source; use TrustedRouter catalog/provider data for those.

## Use The Blog As Product Context

Read `https://trustedrouter.com/blog` when the recommendation depends on TrustedRouter's current product thesis, eval findings, or named combo models. Use blog claims as context, then verify current availability, prices, provider health, and privacy posture from MCP/catalog data.

Current blog themes to apply:

- Open source + attestation: prefer routes whose trust boundary and provider posture can be explained.
- Smart, cheap, fast is task-dependent: use one fast/cheap model for routine work and orchestration only when the task justifies fanout.
- Combo models are model containers: Synth, Socrates, advisor, selector, mapreduce, and subagent routes can hide multiple calls behind one model id, so estimate subcall cost and privacy exposure.
- Open-weight models can be first-class choices: consider Prometheus/open-weight routes for strong quality at lower cost when the user's privacy and license needs fit.
- Provider behavior matters as much as model weights: censorship, refusal, uptime, and empty-response patterns can be endpoint-specific, so use leaderboard/provider data before pinning.

## Model Selection Rules

Read `references/model-selection.md` when the task needs a careful recommendation, a cost estimate, or a privacy/speed/quality tradeoff. For simple setup questions, the core workflow above is enough.

Default heuristics:

- Sensitive legal, healthcare, enterprise, or customer data: start with `trustedrouter/zdr`; add `provider.data_collection = "deny"` for an explicit ZDR filter; consider `trustedrouter/e2e` when end-to-end encrypted providers are required; use `trustedrouter/eu` plus the EU regional base URL for Europe-focused workloads; use `provider.jurisdiction = "us"` when the user requires US-based providers.
- Maximum uptime and broad fallback: use `trustedrouter/auto` or an explicit model with multiple healthy provider endpoints.
- Cheap experimentation: start with `trustedrouter/cheap`, then compare one stronger candidate if the task matters.
- Fast small tasks: start with `trustedrouter/fast` or a directly fast provider endpoint from the live catalog.
- Repeated long-context tasks: prefer one cache-friendly model/provider so prompt caching can compound. Do not rotate models casually if the stable context is large and cache hit rates matter.
- Hard coding, agentic terminal work, or evals: compare a code-focused Synth preset and a strong single model. Use AI IQ production-engineering and computer-use dimensions when available.
- High-stakes synthesis or research: consider `trustedrouter/synth`, `trustedrouter/prometheus-1.0`, `trustedrouter/zeus-1.0`, or `trustedrouter/socrates-1.1`, but estimate cost first because orchestration can make multiple subcalls.
- User-created custom models: `trustedrouter/user-*` aliases are unlisted and callable by id. Do not assume their hidden prompt, provider route, or privacy class without inspecting owner-visible metadata.

## Privacy And Region Filter Shapes

Use aliases for easy defaults and provider filters for hard requirements:

```json
{
  "model": "trustedrouter/zdr",
  "provider": {
    "data_collection": "deny",
    "jurisdiction": "us",
    "only": ["anthropic", "openai"],
    "allow_fallbacks": true
  }
}
```

- `trustedrouter/zdr`: zero-retention providers first.
- `trustedrouter/e2e`: provider-side confidential or end-to-end encrypted routes.
- `trustedrouter/eu`: EU-focused route pool; pair with `https://api-europe-west4.quillrouter.com/v1` when the gateway region matters.
- `provider.data_collection = "deny"`: explicit ZDR filter.
- `provider.jurisdiction = "us"`: US-based provider filter. Do not invent `jurisdiction = "eu"`; use `trustedrouter/eu`, the EU regional base URL, and explicit `provider.only` allowlists instead.
- `provider.only`, `provider.ignore`, `provider.order`, `provider.sort`, and `allow_fallbacks` narrow or rank eligible endpoints. Tell the user if filters reduce fallback reliability.

## Cost And Speed Estimate

When the user gives enough information, estimate before execution:

```text
input_tokens ~= provided tokens or ceil(chars / 4)
output_tokens ~= requested max_tokens or expected response size
cost_usd ~= (input_tokens / 1_000_000 * input_price_per_million)
         + (output_tokens / 1_000_000 * output_price_per_million)
```

For orchestration models, multiply by the expected number of subcalls:

- single model: usually 1 call
- advisor/Socrates: 1 worker call, plus advisor calls only when used
- Synth: panel calls plus judge/synthesizer calls
- MapReduce/selector/subagent: depends on configured workers or subagents

Report estimates as ranges when route fallback or orchestration depth makes exact cost unknown. State which assumptions drive the estimate.

For prompt caching:

- Consider cache economics when the same system prompt, repo context, legal record, retrieved corpus, or agent transcript prefix repeats across calls.
- Recommend sticking with one model/provider when cache savings outweigh marginal quality or uptime gains from routing broadly.
- Include cached-read and cache-write assumptions separately from uncached input tokens when the catalog exposes cached-token pricing.
- Remind users to monitor cached-read rates in generation metadata, analytics, or provider billing. Low cached reads mean the expected savings are not actually happening.

For speed, prefer live TrustedRouter provider health and recent benchmark/leaderboard data. Distinguish:

- time to first token
- output tokens per second
- full response wall time
- orchestration overhead from parallel or serial subcalls

## Signup And Key Handling

If the user needs onboarding:

1. Send them to `https://trustedrouter.com`.
2. Have them sign in and create an API key in the console.
3. Tell them to save it locally as `TRUSTEDROUTER_API_KEY`.
4. Keep the key out of source control, logs, screenshots, and prompts.
5. Prefer a secret manager, 1Password, direnv with a gitignored `.envrc`, or a local shell profile with restrictive permissions.
6. Run a cheap PONG smoke test before changing application code.
7. Link SDKs for native integrations: `https://github.com/Lore-Hex/trusted-router-py`, `https://github.com/Lore-Hex/trusted-router-js`, and `https://github.com/jperla/trusted-router-swift`.

Smoke test:

```bash
curl https://api.trustedrouter.com/v1/chat/completions \
  -H "Authorization: Bearer $TRUSTEDROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "trustedrouter/zdr",
    "messages": [{"role": "user", "content": "Reply with PONG only."}],
    "max_tokens": 4
  }'
```

## Answer Shape

Use this format for recommendations:

```markdown
**Best Fit**
Use `<model-id>` because ...

**Alternatives**
| Model | Best for | Quality signal | Speed | Estimated cost | Privacy |
|---|---|---:|---:|---:|---|

**Estimate**
Assuming X input tokens and Y output tokens, this should cost about $Z and take roughly T.

**Setup**
```bash
export OPENAI_BASE_URL="https://api.trustedrouter.com/v1"
export OPENAI_API_KEY="$TRUSTEDROUTER_API_KEY"
```
```

Keep recommendations honest. If live catalog, provider health, AI IQ, or pricing data is unavailable, say so and give a provisional recommendation with the missing checks named explicitly.
