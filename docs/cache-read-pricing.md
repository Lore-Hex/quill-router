# Cache-read pricing: how it flows, and the per-provider record

Written 2026-08-31, after auditing all 76 provider price sources against each
provider's published caching policy. Keep this current when adding a provider.

## How a cache read gets its price (settle path)

1. The enclave reports `cache_read_input_tokens` on settle; `input_tokens` are
   the UNCACHED prompt tokens.
2. `_endpoint_cost_microdollars` bills cache reads at the endpoint's per-model
   `prompt_cached` tier price when the catalog has one — sourced from the
   provider manifest (`cached_input_token_price_per_m` in
   `src/trusted_router/data/provider_models/<slug>.json`, written by the hourly
   refresh) or from OR-snapshot `input_cache_read`.
3. Only when the endpoint carries no cached price does
   `cache_token_prices_microdollars` fall back to
   `_CACHE_READ_PRICE_MULTIPLIER` — per-provider multipliers for providers with
   a CONFIRMED uniform published policy; default 1.0 (full prompt price).

The 1.0 default is deliberate: for a provider that does not discount cache
hits (or does not publish a rate), billing full price matches what the
provider charges us. Do not "fix" absence into a discount without a source.

As of 2026-08-31, 1,048 of 1,740 endpoints carry a per-model cached price.

## Per-provider findings (2026-08-31)

| Provider | Upstream policy | Our source | State |
|---|---|---|---|
| anthropic | −90% reads, +25% writes, storage fee | manifest + 0.1×/1.25× fallback | covered (20/20) |
| openai | −50% reads (4o+; legacy models have no caching) | manifest + 0.5× fallback | covered; legacy absent upstream = correct |
| google (studio/vertex) | −75% implicit | manifest + 0.25× fallback | covered |
| deepseek | per-model hit price (~10–20× cheaper); peak/off-peak schedule | HTML parser, per-model | covered (7/8) |
| moonshotai (kimi) | per-model hit price: k3 $3→$0.30, k2.6 $0.95→$0.16, k2.7-code $0.95→$0.19 (+highspeed 2×) | HTML parser, per-model | covered for every model Moonshot still prices; legacy moonshot-v1 pages are gone — absence stays |
| mistral | **flat −90%** ("cached input tokens reduce input cost by up to 90%") | **0.1× fallback (added 2026-08-31)** + parser reads explicit "Cached input" card rows where published | was billing 1× — fixed |
| fireworks | automatic −50% | manifest (31/31) + **0.5× fallback (added)** | covered |
| alibaba | implicit cache hits at 20% of input | manifest (76/77) + **0.2× fallback (added)** | covered |
| z-ai (GLM) | per-model | parser (22/26) | covered for current models |
| x-ai (grok) | per-model | manifest (12/13; missing = video, N/A) | covered |
| deepinfra | per-model `cache_read` price in /v1/openai/models metadata | API pass-through (99/194) | models without an upstream rate genuinely publish none — absence correct |
| together | model-specific cached-input rates on select models | API pass-through (12/26) | absence upstream = correct |
| tinfoil | per-model `cachedInputTokenPricePer1M` in /v1/models | API pass-through (6/12) | absence upstream = correct |
| siliconflow | per-model cached column on pricing page | parser (31/59) | rows without the column publish none — correct |
| novita, venice, gmi, io-net, telnyx, crusoe, baseten, parasail, minimax, friendli, near-ai, makora, chutes, wandb, atlas-cloud, akashml, aion-labs, and the rest of the covered set | per-model | manifest/API/parser pass-through | covered where upstream publishes |
| **cerebras** | **caching automatic, NO discount** — "billed at the standard input token rate" | none needed | absence = correct; do NOT add a multiplier |
| **nebius** | **no prompt caching yet** (open feature request) | none | absence correct |
| **lightning** | no cached rate in /v1/models, none published | none (extension point documented in the provider module) | absence correct |
| cohere, nvidia-nim, nscale, featherless, mancer, neurometric, reka, perplexity | no published cache-hit pricing found | none | absence correct — revisit if they publish |
| jina, voyage (embeddings); bfl, fal, krea, kling, ltx, recraft, runway, decart (image/video) | token caching not applicable | none | N/A by construction |

## Rules distilled

- **Per-endpoint price beats multiplier beats default.** Never encode a
  per-model ratio as a provider multiplier — deepseek/kimi/z-ai ratios vary by
  model and change; multipliers are only for flat published policies
  (currently anthropic, openai, google×3, mistral, fireworks, alibaba).
- **Absence is a claim** — it asserts "this provider bills cache reads at full
  price." When adding a provider, check their caching page and either wire the
  cached price or record the N/A here.
- The public `/v1/models` payload publishes `input_cache_read` (model headline
  and per-endpoint) — a customer must be able to see the discount they get.
