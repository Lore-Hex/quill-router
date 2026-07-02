# TrustedRouter Model Selection Reference

Use this reference when a user wants a careful model choice, cost estimate, speed estimate, privacy-aware routing decision, or benchmark-based recommendation.

## Data Sources

Use data in this order:

1. TrustedRouter MCP tools for live catalog, providers, credits, docs, and test calls.
2. TrustedRouter public pages:
   - `https://trustedrouter.com/choose`
   - `https://trustedrouter.com/leaderboard`
   - `https://trustedrouter.com/models`
   - `https://trustedrouter.com/providers`
   - `https://trustedrouter.com/docs/mcp`
   - `https://trustedrouter.com/docs/agent-setup`
   - `https://trustedrouter.com/docs/synth`
   - `https://trustedrouter.com/eu`
   - `https://trustedrouter.com/trust`
3. TrustedRouter blog for product thesis, eval context, and named model families:
   - `https://trustedrouter.com/blog`
   - Use it for context, then verify current model availability, price, provider health, and privacy posture from MCP/catalog data.
4. TrustedRouter public catalog fallback:
   - `https://trustedrouter.com/v1/models`
5. TrustedRouter SDKs:
   - Python SDK GitHub: `https://github.com/Lore-Hex/trusted-router-py`
   - Python SDK PyPI: `https://pypi.org/project/trusted-router-py/`
   - JS/TS SDK GitHub: `https://github.com/Lore-Hex/trusted-router-js`
   - JS/TS SDK npm: `https://www.npmjs.com/package/@lore-hex/trusted-router`
   - Swift SDK GitHub: `https://github.com/jperla/trusted-router-swift`
6. AI IQ MCP or API for independent quality/benchmark context:
   - `https://www.aiiq.org/api/mcp`
   - `https://www.aiiq.org/api/models`
   - `https://www.aiiq.org/api/rankings`
   - `https://www.aiiq.org/api/benchmarks`
   - `https://www.aiiq.org/api/charts`
   - `https://www.aiiq.org/api/methodology`

Do not use memory as the source of truth for current prices, provider health, model availability, or latest benchmark scores.

## Blog-Informed Heuristics

Use the blog as current product context, not as a replacement for live data:

- The open-source and attestation posts support recommending TrustedRouter for prompts where the user needs verifiable routing, source inspection, and no prompt/output logging by default.
- The "smart, cheap, fast" and model-choice posts support presenting tradeoffs explicitly. Do not claim one model is universally best.
- The Synth/combo-model posts support recommending orchestration when multiple perspectives, self-fusion, judging, or synthesis are worth the extra subcalls.
- The Prometheus/Zeus/Iris posts support choosing open-weight or budget panels when cost matters and frontier panels when maximum score matters.
- The endpoint-behavior posts support checking provider health, refusals, empty responses, censorship, and route-specific behavior before pinning a provider.
- The benchmark posts support comparing TrustedRouter results to AI IQ and other public evals, but always label what is measured and what is only contextual.

## Task Mapping

| Task | Primary signals | Good starting routes |
|---|---|---|
| Sensitive legal or customer work | ZDR, provider retention, reliability, cost | `trustedrouter/zdr`, explicit ZDR endpoints |
| End-to-end encrypted work | E2E provider posture, availability, latency | `trustedrouter/e2e`, explicit E2E endpoints |
| Europe-focused work | EU provider/region, data residency, latency | `trustedrouter/eu`, EU regional base URL |
| US-only provider policy | provider headquarters/jurisdiction, contract allowlist | `provider.jurisdiction = "us"`, optional `provider.only` |
| Cheap tests and eval sweeps | low output price, provider health, acceptable IQ | `trustedrouter/cheap`, direct cheap models |
| Low-latency agent turns | TTFT, output tokens/sec, health | `trustedrouter/fast`, direct fast endpoints |
| Hard coding or terminal tasks | AI IQ production-engineering and computer-use, recent evals, context | code Synth presets, Socrates, strong direct coding models |
| Long-context analysis | context window, input price, prompt-cache fit, retrieval need, privacy | direct long-context models, 1M-context presets |
| Summarization/extraction | input cost, context, reliability | cheap long-context model first, stronger fallback if needed |
| Creative writing | style, long output price, speed | compare one frontier model and one cheap open model |
| High-stakes answer synthesis | benchmark IQ, reliability, multiple perspectives | `trustedrouter/synth`, Prometheus/Zeus/Socrates, estimate first |

## Privacy Classes

Always explain who may see the prompt:

- Attested TrustedRouter gateway: prompt TLS terminates inside the measured gateway.
- Downstream provider: the selected provider still receives the prompt unless the route is an E2E confidential provider with that guarantee.
- ZDR route: provider/data policy should say no training or no retention. Verify from live provider metadata when possible.
- E2E route: use only when the provider path itself provides end-to-end encrypted or confidential-compute handling.
- Control plane and MCP: metadata/catalog/docs calls do not need prompt content. `chat-send` forwards only the short test prompt.

## Provider And Region Filters

Use model aliases for convenient defaults and provider filters for hard policy requirements.

Common request body shapes:

```json
{
  "model": "trustedrouter/zdr",
  "provider": {
    "data_collection": "deny"
  }
}
```

```json
{
  "model": "trustedrouter/eu",
  "provider": {
    "only": ["mistral", "gemini"],
    "allow_fallbacks": true
  }
}
```

```json
{
  "model": "z-ai/glm-5.2",
  "provider": {
    "jurisdiction": "us",
    "sort": "throughput"
  }
}
```

Filter meanings:

- `provider.data_collection = "deny"`: explicit zero-data-retention filter.
- `provider.min_privacy = "zdr"` or `"maximum"`: privacy-tier floor when available.
- `provider.jurisdiction = "us"`: restrict to US-based providers. Supported aliases include `us`, `usa`, `united-states`, and `united states`.
- `provider.only`: allowlist provider slugs. Use for contracts, BAAs, enterprise allowlists, or strict EU/provider choices.
- `provider.ignore`: denylist provider slugs.
- `provider.order`: prefer providers in a chosen order while keeping remaining fallback candidates.
- `provider.sort = "throughput"` or model suffix `:nitro`: prefer faster endpoints.
- `provider.sort = "price"` or model suffix `:floor`: prefer cheaper endpoints.
- `allow_fallbacks = false`: pin the first eligible route only. Warn that this reduces uptime.

Region guidance:

- For EU gateway routing, set base URL to `https://api-europe-west4.quillrouter.com/v1` and model to `trustedrouter/eu`.
- `trustedrouter/eu` is EU-focused routing, not a blanket data-residency promise for every upstream provider. Use `provider.only` for strict approved-provider lists.
- Do not use `provider.jurisdiction = "eu"`; the API currently supports only US jurisdiction filtering. For Europe, use the EU alias, regional base URL, and explicit provider allowlists.
- For sensitive legal, healthcare, or financial workloads, combine the alias with a hard filter when possible: `trustedrouter/zdr` plus `provider.data_collection = "deny"`, or `trustedrouter/e2e` plus an allowlist of approved E2E providers.

## Cost Estimation Details

Use provider prices from `model-endpoints` or catalog model pricing. If the route has multiple providers, calculate a low/high range from eligible endpoints.

For a single endpoint:

```text
input_cost = input_tokens * input_price_per_1m / 1_000_000
output_cost = output_tokens * output_price_per_1m / 1_000_000
total = input_cost + output_cost
```

For cached-token pricing, include it only when the workload has a stable repeated prefix or when the user explicitly says caching applies. Otherwise use uncached pricing.

Prompt caching guidance:

- Caching usually rewards consistency: a stable system prompt, tool spec, repo context, legal matter, or retrieved corpus should often stay on one model/provider so cached reads accumulate.
- Broad routing can lower uptime risk, but it can also fragment cache hits across providers. Call out that tradeoff before recommending `trustedrouter/auto`, Synth, or frequent model switching for repeated long-context work.
- Estimate cached reads, cache writes, and uncached input separately when the catalog exposes those prices.
- After launch, verify cached-read rates continuously in generation metadata, analytics, or provider billing. If cached reads stay low, revise the model choice or prompt layout instead of assuming the savings.

For orchestration:

- Ask whether the user wants exact or conservative budgeting.
- Estimate panel size and fallback count.
- Treat failed/refunded routes as uncertain unless generation metadata exposes subcall accounting.
- Prefer a range: "likely $0.02-$0.06; worst case under this configuration about $0.14."

## Speed Estimation Details

Do not collapse speed into one number. If available, show:

- TTFT: responsiveness.
- Output tokens/sec: generation speed.
- Wall time: likely user-visible completion time.
- Failure/fallback risk: high error rate can dominate latency.

For orchestration:

- Parallel panel calls are usually bounded by the slowest panel member plus judge/synthesizer time.
- Advisor models may not call the advisor on every request.
- MapReduce and subagent patterns can multiply wall time if configured serially.

## Approval Policy

Ask before spending when:

- The user has not authorized billable test calls.
- The estimated call is more than a tiny smoke test.
- The task may fan out to multiple subcalls.
- The model may use expensive long context or high output tokens.

Use concise approval language:

```text
This should be about 18k input tokens and 2k output tokens. Cheapest route: about $0.01. Synth route: likely $0.06-$0.18 depending on subcalls. Want me to run the cheap route first?
```

## Common Outputs

### One-off app migration

Recommend `trustedrouter/zdr` by default for sensitive apps, or `trustedrouter/auto` when the user values broad fallback more than strict provider privacy.

For app code, recommend the Python SDK `trusted-router-py`, JS/TS SDK `@lore-hex/trusted-router`, or Swift SDK `TrustedRouter` when the user wants TrustedRouter-specific helpers. Recommend the stock OpenAI SDK plus `OPENAI_BASE_URL=https://api.trustedrouter.com/v1` when the app already has OpenAI-compatible provider wiring.

### Eval sweep

Recommend a short model set:

- one cheap model
- one fast model
- one high-IQ model
- one privacy-constrained model if relevant
- one orchestration preset only if the benchmark rewards synthesis or agentic decomposition

### Agent coding

Recommend testing a cheap/fast model for routine turns and a stronger advisor or Synth route for stuck turns. If the agent supports model switching, propose a two-tier policy instead of one expensive default.

For agents with a large stable repo or tool context, consider keeping routine turns on one cache-friendly model. Switching models for every turn can erase prompt-cache savings even when the headline token price looks cheaper.

### Legal team

Default to `trustedrouter/zdr` or a verified explicit ZDR model endpoint. Show the provider and data policy. Mention `trustedrouter/e2e` only when the user requires the provider-side confidential path.
