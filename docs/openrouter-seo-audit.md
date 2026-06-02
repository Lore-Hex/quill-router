# OpenRouter SEO Audit For TrustedRouter

Audit date: 2026-06-02

This is a public-surface SEO audit of OpenRouter for planning TrustedRouter pages. It is not a content-copying plan. TrustedRouter should copy the useful page families and information architecture, but use its own trust, pricing, uptime, provider-policy, and benchmark data. Do not link OpenRouter from TrustedRouter benchmark pages.

## Sources Checked

- `https://openrouter.ai/robots.txt`
- `https://openrouter.ai/sitemap.xml`
- `https://openrouter.ai/llms.txt`
- `https://openrouter.ai/docs/llms.txt`
- `https://openrouter.ai/docs/llms-full.txt`
- Sample pages:
  - `/`
  - `/models`
  - `/rankings`
  - `/providers`
  - `/provider/minimax`
  - `/pricing`
  - `/apps`
  - `/works-with-openrouter`
  - `/compare/anthropic/claude-opus-4.8-fast/anthropic/claude-opus-4.8`
  - `/minimax/minimax-m3`
  - `/minimax/minimax-m3/benchmarks`
  - `/minimax/minimax-m3/providers`
  - `/minimax/minimax-m3/performance`
  - `/minimax/minimax-m3/pricing`
  - `/docs/quickstart`

## Robots And Indexing

OpenRouter's `robots.txt` is permissive:

- `Allow: /`
- `Disallow: /seo/`
- `Disallow: /uptime`
- `Sitemap: https://openrouter.ai/sitemap.xml`

TrustedRouter currently has no production `robots.txt` and no `sitemap.xml`; both return 404. That is a hard SEO gap.

## Sitemap Inventory

OpenRouter's sitemap had 4,170 URLs in the audit. Route-family counts:

| Route family | Count | Priority pattern |
|---|---:|---|
| `/compare/{a}/{a-model}/{b}/{b-model}` | 2,860 | 0.6 |
| model detail `/{author}/{slug}` | 550 | mostly 0.8 / 0.7 |
| docs pages | 304 | 0.6 |
| publisher/provider collection pages | 143 | 0.7 / 0.8 |
| announcements | 62 | 0.6 |
| works-with app pages | 41 | 0.6 |
| apps pages/categories | 37 | 0.6 / 0.7 / 0.9 |
| model benchmark pages | 6 | 0.7 |
| model provider/performance/pricing/activity/api/uptime pages | 10 each for selected models | 0.7 |
| core pages: home, rankings, models, providers, pricing, enterprise, data, state-of-AI, SDK, chat | 1 each | 0.7-1.0 |

Important observation: OpenRouter does not only rank for `/models`; it creates a page cluster around models and comparisons. The comparison pages are by far the largest sitemap family.

## Page Families To Replicate

### P0: Must Ship

1. `robots.txt`
   - Allow public pages.
   - Disallow console/auth/private/internal pages.
   - Point to `https://trustedrouter.com/sitemap.xml`.

2. `sitemap.xml`
   - Include home, models, providers, status, security, chat, comparison pages, docs, and per-model detail pages.
   - Include model subpages for every public model:
     - `/models/{author}/{slug}`
     - `/models/{author}/{slug}/benchmarks`
     - `/models/{author}/{slug}/providers`
     - `/models/{author}/{slug}/performance`
     - `/models/{author}/{slug}/pricing`
     - `/models/{author}/{slug}/uptime`
     - `/models/{author}/{slug}/api`
   - Do not include internal-only models such as `trustedrouter/monitor`.

3. Per-model SEO cluster
   - Existing: `/models/{author}/{slug}`.
   - Missing: benchmark, provider, performance, pricing, uptime, API pages.
   - Each page needs a distinct title and meta description. Do not canonicalize all subpages to the model detail page; canonicalize each to itself.

4. Rankings / benchmarks page
   - Existing equivalent is status/provider transparency, but not model rankings.
   - Add `/rankings` or `/benchmarks`.
   - Surface TrustedRouter's own latency, uptime, provider failover, and price-per-token data.
   - Do not show user counts or popularity until there are enough users.

5. Provider detail pages
   - Existing: `/providers` table.
   - Missing: `/providers/{provider}` or `/provider/{provider}` detail pages.
   - Include retention policy links, provider-side ZDR/E2EE/confidential-compute claims, models served, route modes, pricing, and benchmark/status summaries.

6. AI-readable docs
   - OpenRouter has `/llms.txt`, docs-level `llms.txt`, docs `.md` pages, and one huge `llms-full.txt`.
   - TrustedRouter should ship:
     - `/llms.txt`
     - `/docs/llms.txt`
     - `/docs/llms-full.txt`
     - `.md` variants for important docs pages.

### P1: High-Value

1. Model-vs-model comparison pages
   - Add `/compare/{author_a}/{slug_a}/{author_b}/{slug_b}`.
   - Generate only sane pairs first:
     - within the same publisher/family,
     - top 20 models,
     - common migration pairs,
     - `trustedrouter/auto` vs popular models.
   - Include price, context, route count, provider policy, uptime, latency, tool/image/structured-output support, and benchmark links.

2. Works-with / integrations pages
   - Add `/works-with-trustedrouter`.
   - Add app/integration pages for Claude Code, Cursor, OpenAI SDK, Vercel AI SDK, LiteLLM, LangChain, Open WebUI, and common coding agents.
   - These pages should be migration/how-to pages, not fake partner pages.

3. Feature docs
   - Route pages around actual product differentiators:
     - attested API,
     - BYOK envelope encryption,
     - provider fallback,
     - status/synthetics,
     - broadcast/PostHog/webhook,
     - Responses API,
     - image URL handling,
     - function tools,
     - no prompt logs,
     - provider privacy matrix.

4. Announcements
   - Add `/announcements` and release posts for real changes.
   - These become durable landing pages for each model/provider launch.

### P2: Later

1. Apps marketplace
   - OpenRouter's `/apps` and category pages are a growth surface.
   - TrustedRouter should only add this once third-party apps actually integrate.

2. Enterprise / case studies
   - Add once there are design partners or credible internal deployments.

3. State/data pages
   - OpenRouter has `/state-of-ai` and `/data`.
   - TrustedRouter can do a trust-focused equivalent after 30-60 days of status and routing data.

## Metadata Patterns Observed

OpenRouter pages generally have:

- unique `<title>`;
- unique `meta name="description"`;
- canonical link;
- `og:title`, `og:description`, `og:url`;
- Twitter card metadata;
- OpenGraph image metadata;
- no `meta robots` on public pages;
- no JSON-LD detected on sampled pages;
- Next/Vercel prerender/cache headers on several public pages.

TrustedRouter already has canonical and descriptions on several public pages, and Product JSON-LD on model details. Gaps:

- `/providers` rendered without a visible title in the audit sample.
- No robots/sitemap.
- No model subpage cluster.
- No `llms.txt`.
- Weak model descriptions: many are generic and do not include benchmark/provider/status facts.

## Benchmark Link Policy

TrustedRouter benchmark pages should not link OpenRouter. Use:

- TrustedRouter's own synthetic and provider benchmark data.
- Official model/provider benchmark pages when available.
- Independent benchmark sources when a direct page is known, such as Artificial Analysis, LMArena, LiveBench, HELM, BenchLM, or provider-neutral eval reports.

Do not manufacture direct links to benchmark pages that may not exist. For models without verified external benchmark URLs, show:

- TrustedRouter internal measurements;
- official provider/model page if known;
- a clear "external benchmark link not verified yet" state.

## Recommended TrustedRouter URL Surface

Core:

- `/`
- `/models`
- `/providers`
- `/status`
- `/benchmarks`
- `/rankings`
- `/security`
- `/docs`
- `/docs/quickstart`
- `/docs/responses-api`
- `/docs/provider-routing`
- `/docs/broadcast`
- `/docs/byok`
- `/docs/attested-gateway`
- `/docs/model-fallbacks`
- `/docs/image-inputs`
- `/docs/tool-calling`
- `/docs/privacy`
- `/llms.txt`
- `/docs/llms.txt`
- `/docs/llms-full.txt`
- `/sitemap.xml`
- `/robots.txt`

Per model:

- `/models/{author}/{slug}`
- `/models/{author}/{slug}/benchmarks`
- `/models/{author}/{slug}/providers`
- `/models/{author}/{slug}/performance`
- `/models/{author}/{slug}/pricing`
- `/models/{author}/{slug}/uptime`
- `/models/{author}/{slug}/api`

Per provider:

- `/providers/{provider}`
- `/providers/{provider}/models`
- `/providers/{provider}/privacy`
- `/providers/{provider}/pricing`
- `/providers/{provider}/performance`

Comparisons:

- `/compare/{author_a}/{slug_a}/{author_b}/{slug_b}`
- `/compare/openrouter`
- `/compare/vercel-ai-gateway`
- `/compare/litellm`
- `/compare/provider/{provider_a}/{provider_b}`

Integrations:

- `/works-with-trustedrouter`
- `/integrations/openai-sdk`
- `/integrations/anthropic-sdk`
- `/integrations/vercel-ai-sdk`
- `/integrations/langchain`
- `/integrations/litellm`
- `/integrations/open-webui`
- `/integrations/claude-code`
- `/integrations/cursor`

## Implementation Order

1. Ship `robots.txt`, `sitemap.xml`, and `/llms.txt`.
2. Add model subpage routes and templates.
3. Add internal links from the model table and model detail page to benchmarks/providers/performance/pricing/uptime/API.
4. Add benchmark-link data model with verified official/independent links.
5. Add provider detail pages.
6. Add top-model comparison pages.
7. Add docs `.md` variants and `docs/llms-full.txt`.
8. Add announcement/blog pages for new model/provider launches.

## Current TrustedRouter Gap Summary

TrustedRouter has a credible model catalog and provider privacy page, but is missing the SEO mechanics that make those pages discoverable:

- no sitemap;
- no robots;
- no AI-readable `llms.txt`;
- no per-model benchmark/pricing/performance/uptime/API pages;
- no provider detail pages;
- no generated model comparison pages;
- no docs index with Markdown variants;
- no announcements page for product/model launches.

This is fixable with mostly deterministic pages generated from the catalog, status data, and provider policy metadata already in the repo.
