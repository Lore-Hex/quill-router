# Ahrefs SEO execution: 2026-08-15

## Baseline

The Ahrefs project and connected Google Search Console property disagree because
TrustedRouter is a young site and Ahrefs has not yet modeled its search traffic.
Use Search Console as the source of truth for clicks and Ahrefs for crawl,
backlink, competitor, and rank-change research.

| Metric | Observed value |
| --- | ---: |
| Ahrefs Site Audit health | 100 |
| Crawled URLs | 4,454 |
| Site Audit errors | 2 |
| Site Audit warnings | 402 |
| Indexable pages with one internal dofollow inlink | 1,925 |
| Indexable pages absent from sitemap | 66 |
| Referring domains | about 540 |
| Referring domains with a dofollow link | 43 |
| Search Console clicks, May 1 through August 12 | 585 |
| Search Console impressions, May 1 through August 12 | 11,500 |
| Search Console CTR | 5.1% |
| Search Console average position | 12.1 |

The authenticated API root was the only meaningful 4xx crawl finding. The trust
page linked crawlers directly to it. The public API reference is the correct
destination.

## Changes shipped from this audit

1. Give every public model comparison two deterministic neighboring comparison
   links in addition to its directory and related-model links. This turns the
   2,600-page comparison set into a complete crawl graph.
2. Add `/trust`, `/api/reference`, and `/docs/x402` to the core sitemap.
3. Replace the trust page's authenticated API-root link with the public API
   reference.
4. Add model-specific answer blocks for model ID, current providers, current
   lowest route prices, and fail-closed ZDR selection. Emit matching FAQPage
   structured data.
5. Improve titles and descriptions for the docs, EU gateway, provider catalog,
   and trust page without creating overlapping landing pages.
6. Add complete social metadata and WebPage structured data to the standalone
   trust page.

## Same-day recrawl

The completed Ahrefs crawl at 10:10 AM covered 4,597 internal URLs and reported
a 100 Health Score. It found three errors, 443 warnings, and 4,485 notices.

The three errors were narrow crawl-graph defects:

1. The independently published trust site still linked to the authenticated API
   root, which correctly returns 401 to a crawler. Link to the public API
   reference instead.
2. `/trust` was self-canonical but had no ordinary internal href. Link the
   shared public footer to `/trust`, then let that page link to the live trust
   evidence.
3. `/api/reference` had no server-rendered outgoing links. Add a compact
   server-rendered reference header with an H1, explanatory text, language
   declaration, and links to docs, models, security, and the homepage.

The remaining warning queue should be handled by value, not by raw count:

1. Investigate the 19 slow public pages and fix only repeatable origin latency;
   do not optimize one-off crawl noise.
2. Replace internal links to the final destinations behind the 83 redirects.
   Keep intentional compatibility redirects themselves.
3. Review the two remaining missing-H1 pages and the one low-word-count page
   after the API-reference fix is deployed and recrawled.
4. Treat the 337 noindex pages as an allowlist audit. Console, authentication,
   and filtered/empty catalog pages should stay noindex and out of sitemaps.
5. Submit changed public URLs through IndexNow after releases. Ahrefs identified
   3,919 currently eligible URLs; submit only new or materially changed URLs,
   not the full catalog on every deploy.

## Rank Tracker groups

Track the United States and United Kingdom on desktop. Add Germany as the first
EU market once the initial set is stable. Tag each keyword by the group below.

### Category

- `ai router`
- `llm router`
- `best llm router`
- `ai gateway`
- `llm gateway`
- `llm api gateway`
- `model router api`
- `multi provider llm api`
- `openai compatible llm api`
- `openrouter alternative`

### Privacy and trust

- `private llm api`
- `no log llm api`
- `zero data retention llm`
- `zdr llm api`
- `confidential ai inference`
- `confidential computing llm`
- `end to end encrypted llm api`
- `secure ai proxy`
- `ai prompt privacy`
- `llm attestation`
- `claude api privacy`
- `deepseek api privacy`

### Reliability and performance

- `llm failover`
- `ai provider failover`
- `multi region llm api`
- `llm provider uptime`
- `llm latency benchmark`
- `llm provider latency`
- `ai model latency`
- `llm throughput benchmark`

### EU and compliance

- `eu llm gateway`
- `eu ai gateway`
- `gdpr compliant llm api`
- `llm data residency`
- `eu ai act llm compliance`
- `hipaa llm api`
- `llm api for law firms`
- `llm api for financial services`

### Migration and model APIs

- `litellm alternative`
- `portkey alternative`
- `vercel ai gateway alternative`
- `azure openai alternative`
- `aws bedrock alternative`
- `groq alternative`
- `vertex ai alternative`
- `kimi k2 api`
- `minimax m3 api`
- `glm 5 api`
- `gpt oss 120b api`
- `gemini flash alternative`

## Competitors

Use the following domains for rank overlap and content-gap analysis:

- `openrouter.ai`
- `portkey.ai`
- `helicone.ai`
- `litellm.ai`
- `tinfoil.sh`
- `together.ai`

Large platform domains such as Cloudflare and Vercel are useful for individual
keyword checks but distort domain-level comparisons because most of their search
traffic is unrelated to AI gateways.

## Link earning priorities

Ignore the spam-anchor volume in the backlink report. Prioritize relevant links
that can send developers or validate the product:

1. Keep TrustedRouter provider integrations current in `models.dev` and similar
   model registries.
2. Finish and maintain native setup paths in open-source AI clients and agent
   frameworks, then ask maintainers to link the public integration guide.
3. Offer measured provider latency and privacy data to developer newsletters and
   framework documentation, starting with sources already sending credible links
   such as Mastra and This Week in React.
4. Publish one citable data release per month from first-party leaderboard data.
   Include a methodology, observation window, sample count, and stable URL.

## Weekly operating loop

1. Review Search Console query and page deltas over 28 days and 90 days.
2. Review Ahrefs new and lost dofollow referring domains.
3. Review Rank Tracker by the groups above, not only aggregate visibility.
4. Improve an existing earning URL when impressions rise but CTR is weak.
5. Add a new page only when a distinct query intent cannot be answered by an
   existing page.
6. Re-run Site Audit after every large catalog or template change.

Renew Ahrefs only if this loop is actively used. A focused month each quarter is
enough for crawl and competitor research when weekly backlink outreach is not in
progress.
