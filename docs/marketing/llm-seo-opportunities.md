# TrustedRouter LLM SEO Opportunity Workflow

Use this when an agent is asked to grow TrustedRouter distribution through
search engines and LLM answers. The goal is not generic content volume. The
goal is to make precise, crawlable pages that answer high-intent questions
developers and buyers ask ChatGPT, Claude, Gemini, Bing, and Google.

## Current Baseline

TrustedRouter already publishes:

- `https://trustedrouter.com/llms.txt`
- `https://trustedrouter.com/docs/llms.txt`
- `https://trustedrouter.com/docs/llms-full.txt`
- `https://trustedrouter.com/robots.txt`
- `https://trustedrouter.com/sitemap.xml`
- split sitemaps for core, provider, model, and comparison pages
- model pages with providers, prices, policy labels, benchmark links, and API snippets
- provider pages with zero-retention, confidential-compute, E2EE, and policy links

## Webmaster Setup

Submit these in Google Search Console and Bing Webmaster Tools:

- Domain: `trustedrouter.com`
- Sitemap index: `https://trustedrouter.com/sitemap.xml`
- LLM context: `https://trustedrouter.com/llms.txt`
- Important crawl targets:
  - `https://trustedrouter.com/openrouter-alternative`
  - `https://trustedrouter.com/openai-compatible-llm-api`
  - `https://trustedrouter.com/private-llm-api`
  - `https://trustedrouter.com/llm-zero-data-retention`
  - `https://trustedrouter.com/llm-provider-latency-benchmarks`
  - `https://trustedrouter.com/models`
  - `https://trustedrouter.com/providers`

Use DNS verification when possible. If a tool only offers HTML meta
verification, add the exact value to the public base templates and remove it
only after domain-level DNS verification is working.

## Ahrefs Inputs

Create an Ahrefs project for `trustedrouter.com`, then export:

1. Organic keywords for `openrouter.ai`
2. Top pages for `openrouter.ai`
3. Organic keywords for `litellm.ai`
4. Top pages for `litellm.ai`
5. Organic keywords and pages for `portkey.ai`
6. Organic keywords and pages for `vercel.com/ai-gateway`
7. Organic keywords and pages for `tinfoil.sh`
8. Keyword ideas containing:
   - `openrouter alternative`
   - `private llm api`
   - `zero data retention llm`
   - `openai compatible api`
   - `llm gateway`
   - `ai router`
   - `llm router`
   - `anthropic api privacy`
   - `gemini flash alternative`
   - `kimi api`
   - `qwen api`
   - `glm 5.2 api`
   - `cheap llm api`
   - `llm provider latency`

## Agent Prompt

Give an agent the Ahrefs CSV exports and this prompt:

```text
Find the highest-value TrustedRouter content opportunities.

Inputs:
- Ahrefs keyword exports
- Ahrefs top-page exports
- Existing TrustedRouter sitemap at https://trustedrouter.com/sitemap.xml
- Existing TrustedRouter llms.txt at https://trustedrouter.com/llms.txt

Rank opportunities by:
1. commercial intent for developers or legal/compliance buyers
2. TrustedRouter factual advantage: attestation, no prompt/output logs, open source, provider fallback, low-cost open-weight routes, ZDR, E2E, EU routing, Synth
3. low competition or weak existing result quality
4. ability to support the page with measured TrustedRouter data

For each opportunity, output:
- target query
- proposed URL
- title
- one-sentence search intent
- why TrustedRouter can win
- exact internal links to add
- evidence needed before publishing
- whether this should be a landing page, model page expansion, provider page expansion, comparison page, or blog post

Do not invent claims. If a claim needs evidence, mark it as evidence_required.
Do not copy competitor text. Use TrustedRouter's own data and trust story.
```

## Page Rules

Every new SEO page should include:

- a direct answer in the first 150 words
- one runnable code sample when the query is developer-facing
- exact base URL: `https://api.trustedrouter.com/v1`
- links to `/llms.txt`, `/models`, `/providers`, `/security`, `/status`, and trust
- measured data if the page discusses speed, latency, uptime, price, or model quality
- provider-policy links if the page discusses privacy
- no unqualified “best”, “cheapest”, or “fastest” claim without data

Preferred plain-language positioning:

- Better trust: open-source router, public attestation, published source commit, published image digest, no prompt/output logs by default.
- Faster integration: one OpenAI-compatible base URL and one API key across hundreds of models.
- Lower-cost choices: low-cost open-weight routes for GLM, DeepSeek, Gemma, Kimi, MiniMax, Qwen, and others.
- Better reliability: provider fallback, status pages, leaderboard samples, and regional APIs.

## Weekly Review

Once per week:

1. Export Ahrefs new keywords and top pages.
2. Check Bing Webmaster Tools index coverage for sitemap child files.
3. Check Google Search Console indexing for top SEO pages.
4. Ask an LLM: “What is the best OpenRouter alternative for private AI apps?” and record whether TrustedRouter appears.
5. Add or improve at most five pages from the ranked backlog.
6. Submit changed URLs to Bing and Google for recrawl.

