"""Evidence-backed AI gateway and model-router comparison content."""

from __future__ import annotations

from dataclasses import dataclass

VERIFIED_ON = "2026-08-16"


@dataclass(frozen=True)
class ComparisonSource:
    label: str
    url: str


@dataclass(frozen=True)
class CompetitorComparison:
    slug: str
    name: str
    category: str
    summary: str
    competitor_fit: str
    trustedrouter_fit: str
    migration: str
    rows: tuple[tuple[str, str, str], ...]
    sources: tuple[ComparisonSource, ...]
    faq_items: tuple[tuple[str, str], ...]
    # Full article body, rendered as trusted HTML between the matrix and the
    # migration section. Written per competitor from researched, cited facts;
    # empty means the page renders without an article section.
    article_html: str = ""
    custom_page: bool = False

    @property
    def href(self) -> str:
        return f"/compare/{self.slug}"

    @property
    def title(self) -> str:
        return f"TrustedRouter vs {self.name}"

    @property
    def description(self) -> str:
        return (
            f"Compare TrustedRouter and {self.name} across APIs, deployment, routing, "
            "billing, observability, content handling, and verifiable privacy."
        )


def _rows(
    *,
    deployment: str,
    api: str,
    catalog: str,
    routing: str,
    observability: str,
    content: str,
    verification: str,
    billing: str,
) -> tuple[tuple[str, str, str], ...]:
    # The TrustedRouter column is shared across every comparison page so the
    # claims stay consistent and each one traces to the ground-truth pack:
    # BUSL-1.1 is source-available, not open source; there are no combined
    # route ids, only composable preferences; the 5.5% fee is on provider
    # cost with a $0.01/M floor. Per-page nuance belongs in article_html.
    return (
        (
            "Deployment",
            deployment,
            "Hosted control plane, source-available (BUSL-1.1), with an attested API path",
        ),
        ("API surface", api, "OpenAI Chat Completions and Responses plus Anthropic Messages"),
        ("Model access", catalog, "500+ models across ~50 providers: prepaid, BYOK, and direct"),
        (
            "Routing",
            routing,
            "Provider fallback plus auto, cheap, fast, free, ZDR, E2E, and EU routes "
            "with composable privacy and jurisdiction preferences",
        ),
        ("Observability", observability, "Metadata analytics and opt-in external broadcast"),
        (
            "Prompt content",
            content,
            "No durable prompt or output logs on realtime inference; batch is opt-in "
            "encrypted retention",
        ),
        (
            "Verification",
            verification,
            "Live gateway attestation on three clouds, bound to published source and "
            "release evidence",
        ),
        ("Billing", billing, "Prepaid at provider price + 5.5% ($0.01/M floor), or BYOK"),
    )


def _faq(name: str, competitor_fit: str, migration: str) -> tuple[tuple[str, str], ...]:
    return (
        (
            f"When should I choose {name}?",
            competitor_fit,
        ),
        (
            "When should I choose TrustedRouter?",
            "Choose TrustedRouter when one API must combine many providers with realtime "
            "inference that does not durably log prompt or output content, plus a live "
            "hardware-attested gateway build your team can verify.",
        ),
        (
            f"How hard is it to move from {name}?",
            migration,
        ),
    )


def _comparison(
    *,
    slug: str,
    name: str,
    category: str,
    summary: str,
    competitor_fit: str,
    trustedrouter_fit: str,
    migration: str,
    deployment: str,
    api: str,
    catalog: str,
    routing: str,
    observability: str,
    content: str,
    verification: str,
    billing: str,
    sources: tuple[ComparisonSource, ...],
    faq_items: tuple[tuple[str, str], ...] | None = None,
    article_html: str = "",
    custom_page: bool = False,
) -> CompetitorComparison:
    return CompetitorComparison(
        slug=slug,
        name=name,
        category=category,
        summary=summary,
        competitor_fit=competitor_fit,
        trustedrouter_fit=trustedrouter_fit,
        migration=migration,
        rows=_rows(
            deployment=deployment,
            api=api,
            catalog=catalog,
            routing=routing,
            observability=observability,
            content=content,
            verification=verification,
            billing=billing,
        ),
        sources=sources,
        faq_items=faq_items if faq_items is not None else _faq(name, competitor_fit, migration),
        article_html=article_html,
        custom_page=custom_page,
    )


COMPETITOR_COMPARISONS: tuple[CompetitorComparison, ...] = (
    _comparison(
        slug="openrouter",
        name="OpenRouter",
        category="Hosted model marketplace",
        summary=(
            "OpenRouter runs the largest hosted model marketplace: 413 live model ids as "
            "of August 2026, no markup on inference, and zero prompt logging by default. "
            "TrustedRouter keeps the same one-key migration shape and replaces policy "
            "trust with a hardware-attested, source-available gateway you can verify."
        ),
        competitor_fit=(
            "Choose OpenRouter when catalog breadth and price decide: 413 live model ids "
            "as of August 2026 (the homepage claims 80+ providers), day-one frontier "
            "releases, no markup on inference, and BYOK free below $25,000/month of "
            "list-price usage. Its zero-logging default and SOC 2 Type 2 trust center are "
            "genuinely strong for a hosted service."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when the gateway handling your prompts must be "
            "verifiable rather than promised: live hardware attestation on three clouds, "
            "a source-available prompt path, no durable prompt or output logs on realtime "
            "inference, and privacy-tier routes (zdr, e2e, eu) enforced per request."
        ),
        migration=(
            "OpenAI-compatible chat moves with a base URL and key change; messages, "
            "streaming, and :nitro/:floor suffixes carry over, and provider "
            "order/only/ignore preferences map. As of August 2026, 168 of OpenRouter's "
            "413 model ids resolve on TrustedRouter, so check yours on /models first. "
            "Presets, guardrails, workspace config, and OAuth key flows need rework; the "
            "guide is at /docs/migrate-from-openrouter."
        ),
        deployment="Hosted closed-source gateway on Cloudflare's edge; backend code not published",
        api="OpenAI-compatible chat plus images, video, audio, embeddings, platform endpoints",
        catalog="413 live model ids (2026-08-16); homepage claims 500+ models, 80+ providers",
        routing="Price-weighted load balancing, provider pinning, :nitro/:floor, Guardrails",
        observability="Metadata by default; Workspaces plus Datadog, Langfuse, Weave, S3 destinations",
        content="Zero prompt/completion logging by default; opt-in logging for a 1% discount",
        verification="SOC 2 Type 2; policy-based ZDR with machine-readable endpoint list",
        billing=(
            "No inference markup; 5.5% ($0.80 min) credit fee; BYOK free to $25k/mo list price"
        ),
        sources=(
            ComparisonSource(
                "OpenRouter FAQ (fees, zero-logging default)", "https://openrouter.ai/docs/faq"
            ),
            ComparisonSource(
                "OpenRouter BYOK pricing and allowance", "https://openrouter.ai/docs/use-cases/byok"
            ),
            ComparisonSource(
                "OpenRouter pricing (5.5% platform fee)", "https://openrouter.ai/pricing"
            ),
            ComparisonSource(
                "OpenRouter ZDR guide (zdr parameter, caching stance)",
                "https://openrouter.ai/docs/guides/features/zdr",
            ),
            ComparisonSource(
                "OpenRouter provider routing (default load balancing)",
                "https://openrouter.ai/docs/features/provider-routing",
            ),
            ComparisonSource(
                "OpenRouter trust center (SOC 2 Type 2)", "https://trust.openrouter.ai/"
            ),
            ComparisonSource(
                "OpenRouter live model feed (413 ids, 2026-08-16)",
                "https://openrouter.ai/api/v1/models",
            ),
            ComparisonSource(
                "TechCrunch on OpenRouter's $113M Series B",
                "https://techcrunch.com/2026/05/26/openrouter-more-than-doubles-valuation-to-1-3b-in-a-year/",
            ),
        ),
        faq_items=(
            (
                "Will my OpenRouter model ids work on TrustedRouter?",
                (
                    "Some need mapping. As of August 2026, 168 of the 413 ids on "
                    "OpenRouter's live feed resolve on TrustedRouter unchanged; canonical "
                    "vendor-prefixed ids mostly match, and an unknown id returns "
                    "MODEL_NOT_SUPPORTED rather than a guess. Every OpenRouter API path "
                    "also carries an explicit compatibility classification (real, "
                    "compatible-real, or stub) enforced in CI. Check your model list "
                    "against /models before switching."
                ),
            ),
            (
                "Is OpenRouter cheaper?",
                (
                    "Often, yes. OpenRouter adds no markup to inference and charges 5.5% "
                    "($0.80 minimum) only when you buy credits; BYOK is free below "
                    "$25,000/month of list-price usage. TrustedRouter charges provider "
                    "cost + 5.5% with a $0.01/M floor on prepaid usage. If lowest total "
                    "cost decides and policy-based privacy is acceptable, OpenRouter wins "
                    "on price for most workloads. Our margin pays for the attested "
                    "gateway and the no-durable-log realtime path."
                ),
            ),
            (
                "Both say they don't log prompts. What is actually different?",
                """The enforcement mechanism. OpenRouter's default is zero prompt/completion logging, and its ZDR routing follows providers' declared policies: a policy commitment from a closed-source service, backed by SOC 2 Type 2. TrustedRouter's realtime path keeps no durable prompt or output logs, and the claim is checkable: the gateway source is public, and live attestation endpoints on GCP, AWS, and Azure prove the running build matches the published code. Our attestation covers the gateway, not downstream model providers; those remain policy-tier claims except on e2e routes to confidential-compute providers.""",
            ),
        ),
        article_html="""<h2>What OpenRouter actually is</h2>
<p>OpenRouter, Inc. operates the largest hosted AI gateway: one OpenAI-compatible API in front of a marketplace its <a href="https://openrouter.ai/">homepage</a> counts at 500+ models and 80+ providers, served from Cloudflare's edge. The live model feed at <span class="mono">/api/v1/models</span> returned 413 model ids when we checked on August 16, 2026. The scale is real: TechCrunch reported roughly 25 trillion tokens routed per week in May 2026, the same month the company closed a $113M Series B led by CapitalG at about a $1.3 billion valuation. The gateway itself is closed source; the OpenRouterTeam GitHub org publishes Apache-2.0 SDKs, docs, and tooling, but no backend.</p>
<p>They are shipping fast. 2025-2026 added Workspaces (scoped keys, per-workspace BYOK), Guardrails (budget limits, ZDR enforcement, model restrictions), dedicated image, video, audio, and embedding endpoints, observability destinations (Datadog, Langfuse, Weave, S3), and a Terraform provider.</p>
<h2>Where OpenRouter wins</h2>
<ul>
<li><strong>Catalog and ecosystem.</strong> No gateway carries more: 413 live ids today, new frontier models typically available on day one, and a claimed 250k+ apps built on top. If you need a specific niche model, they probably have it.</li>
<li><strong>Price.</strong> Inference passes through at provider list price with <a href="https://openrouter.ai/docs/faq">no markup</a>. The 5.5% fee ($0.80 minimum) applies when you buy credits with a card; crypto purchases are 5%. BYOK costs nothing below $25,000/month of list-price inference, then 5%. A BYOK team under that allowance pays OpenRouter zero.</li>
<li><strong>Privacy defaults.</strong> The FAQ states zero logging of prompts and completions by default, even on errors. Logging is opt-in, traded for a 1% discount. A per-request <span class="mono">zdr</span> parameter routes only to zero-data-retention endpoints, backed by a machine-readable, auto-updated endpoint list, and provider training can be opted out in settings.</li>
<li><strong>Proven operations.</strong> Price-weighted load balancing skips providers with significant outages in the last 30 seconds, at trillion-token weekly scale. SOC 2 Type 2, with a public trust center hosting the report, a pentest, and a subprocessor list.</li>
</ul>
<h2>The three differences that decide it</h2>
<h3>1. How the no-logging claim is enforced</h3>
<p>OpenRouter's zero-logging default is a policy commitment from a closed-source service, audited through SOC 2. Their own <a href="https://openrouter.ai/docs/guides/features/zdr">ZDR guide</a> notes that in-memory caching of prompts is not considered retaining data, so implicit-caching endpoints stay ZDR-eligible. That is a definitional choice, and you cannot read the code it applies to.</p>
<p>TrustedRouter's equivalent claim is checkable. Every line that touches your prompt is public; the gateway and control plane are source-available under BUSL-1.1, and the SDKs are Apache-2.0 and MIT. Live attestation endpoints on GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers prove the running gateway matches the published build, and a drift workflow compares every published measurement against a live attestation daily and on every deploy. Realtime inference keeps no durable prompt or output logs; what we do keep is metadata: request ids, model, token counts, latency, cost, region, and an API-key hash. The opt-in Batch API is different, retaining enclave-encrypted artifacts for up to 30 days. Verify any of this at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> or read the mechanism on <a href="/security">/security</a>.</p>
<h3>2. Privacy-tier routing</h3>
<p>Both products route by privacy. OpenRouter's <span class="mono">zdr</span> parameter and account settings filter to providers whose declared policies qualify. TrustedRouter ships privacy tiers as first-class routes: <span class="mono">trustedrouter/zdr</span> enforces a contractual zero-data-retention floor, <span class="mono">trustedrouter/e2e</span> restricts to providers running confidential compute with provider-side end-to-end encryption (currently Tinfoil and Phala), and <span class="mono">trustedrouter/eu</span> pins EU-focused providers. Floors compose with per-request <span class="mono">provider.min_privacy</span> and jurisdiction preferences, and our catalog defaults every provider to "assume stored" until an explicit, cited flag earns a higher tier. Downstream enforcement is policy-based for both products; the differences are the conservative default and the e2e tier.</p>
<h3>3. The same 5.5%, on different bases</h3>
<p>Both companies publish a 5.5% figure. They are not the same fee. OpenRouter charges 5.5% when you purchase credits; inference then passes through at provider list price. TrustedRouter charges provider cost + 5.5% on prepaid usage, with a $0.01/M token floor: a $1.00/M provider price bills at $1.055/M, with exact per-model prices on <a href="/models">/models</a> and the formula on <a href="/pricing">/pricing</a>. On BYOK, OpenRouter is free below $25,000/month of list price; TrustedRouter supports BYOK but does not publish a separate BYOK fee. If lowest cost decides, OpenRouter usually wins. Our margin pays for the attested path.</p>
<h2>What we do not claim</h2>
<ul>
<li>Attestation covers our gateway, not the model providers behind it. Downstream handling rests on policy and contract tiers, except e2e routes where the provider's own confidential-compute and E2EE mechanisms apply.</li>
<li>Catalog parity. 168 of OpenRouter's 413 live model ids resolve on TrustedRouter as of August 2026; an unknown id returns <span class="mono">MODEL_NOT_SUPPORTED</span> rather than a guess. Every OpenRouter API path has an explicit classification (real, compatible-real, or stub) enforced in CI.</li>
<li>A long track record. Our repos went public in late April and early May 2026 and our benchmark history starts June 2026. OpenRouter was routing five trillion tokens a week as far back as late 2025.</li>
<li>Certifications. We publish a DPA and BAA but no SOC 2 or HIPAA certification today; OpenRouter holds SOC 2 Type 2, and its trust center lists no HIPAA BAA or ISO certification either.</li>
</ul>
<h2>Migration, honestly</h2>
<p>Plain chat completions move with a base URL and API key swap; the messages shape, streaming loop, and <span class="mono">:nitro</span>/<span class="mono">:floor</span> suffixes carry over, and <span class="mono">provider.order</span>/<span class="mono">only</span>/<span class="mono">ignore</span> preferences map. What does not carry: presets, Guardrails and Workspace configuration, stored BYOK keys, OAuth PKCE user-key flows, and the credits and analytics endpoints. One deliberate difference: <span class="mono">/generation/content</span> returns <span class="mono">content_not_stored</span>, because on realtime routes there is no stored content to return. The step-by-step guide is at <a href="/docs/migrate-from-openrouter">/docs/migrate-from-openrouter</a>.</p>""",
        custom_page=True,
    ),
    _comparison(
        slug="vercel-ai-gateway",
        name="Vercel AI Gateway",
        category="Hosted AI gateway",
        summary=(
            "Vercel AI Gateway routes 327 models across eight modalities with zero token "
            "markup and the deepest AI SDK integration available. TrustedRouter charges "
            "5.5% over provider cost and spends it on what a closed gateway cannot offer: "
            "a source-available, hardware-attested prompt path you can verify instead of "
            "trust."
        ),
        competitor_fit=(
            "Choose Vercel AI Gateway when zero token markup, AI SDK-default integration, "
            "or eight-modality coverage (realtime voice, speech, transcription, "
            "reranking) decides it. It works standalone with just an API key, and "
            "Enterprise invoicing removes even payment-processing fees."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when the gateway's own handling of prompts must be "
            "verifiable: published source, live attestation on three clouds, no durable "
            "prompt or output logs on realtime inference, and zdr/e2e/eu privacy routes "
            "for every customer at no surcharge."
        ),
        migration=(
            "Change the base URL and key for OpenAI-compatible or Anthropic-SDK callers, "
            "remap creator/model slugs, and translate providerOptions.gateway fields to "
            "provider preferences. AI SDK apps configure our provider package explicitly, "
            "since plain model strings default to Vercel's Gateway. Realtime sessions, "
            "reranking, OIDC-token auth, Gateway routing rules, and dashboard budgets and "
            "BYOK keys do not carry over."
        ),
        deployment=(
            "Proprietary hosted service run by Vercel on AWS; no self-host; needs a Vercel team"
        ),
        api=("AI SDK default provider; OpenAI Chat + Responses, Anthropic Messages, OpenResponses"),
        catalog=(
            "327 models from 35 creators, 8 modalities incl. video, realtime, speech (2026-08-16)"
        ),
        routing=(
            "Provider fallback/ordering/filters, latency+cost routing; BYOK retries on system creds"
        ),
        observability=(
            "Logs with optional content transcripts (30-day detail), Custom Reporting, OTel drains"
        ),
        content=(
            "Gateway ZDR by policy; provider retention ignored by default; ZDR routes "
            "Pro/Enterprise"
        ),
        verification=(
            "Platform SOC 2 Type 2 attestation + ISO 27001 cert; closed source, no "
            "independent gateway proof"
        ),
        billing=(
            "No token markup incl. BYOK; card fees on credits; team ZDR $0.10/1k; BYOK "
            "needs paid tier"
        ),
        sources=(
            ComparisonSource(
                "Vercel AI Gateway pricing (no-markup terms, add-on fees)",
                "https://vercel.com/docs/ai-gateway/pricing",
            ),
            ComparisonSource(
                "Vercel AI Gateway Zero Data Retention doc",
                "https://vercel.com/docs/ai-gateway/security-and-compliance/zdr",
            ),
            ComparisonSource(
                "Vercel disallow-prompt-training doc",
                "https://vercel.com/docs/ai-gateway/security-and-compliance/disallow-prompt-training",
            ),
            ComparisonSource(
                "Vercel AI Gateway logs and transcripts doc",
                "https://vercel.com/docs/ai-gateway/observability-and-spend/logs",
            ),
            ComparisonSource(
                "Vercel AI Gateway BYOK doc",
                "https://vercel.com/docs/ai-gateway/authentication-and-byok/byok",
            ),
            ComparisonSource(
                "Vercel AI Gateway GA announcement (2025-08-21)",
                "https://vercel.com/blog/ai-gateway-is-now-generally-available",
            ),
            ComparisonSource(
                "Vercel platform compliance (SOC 2, ISO 27001, HIPAA)",
                "https://vercel.com/docs/security/compliance",
            ),
            ComparisonSource(
                "Vercel AI Gateway public models endpoint", "https://ai-gateway.vercel.sh/v1/models"
            ),
        ),
        faq_items=(
            (
                "Vercel charges nothing on tokens. Why pay TrustedRouter's 5.5%?",
                (
                    "Vercel's zero token markup is real, documented, and includes BYOK; "
                    "if the lowest fee is the deciding criterion, choose Vercel. "
                    "TrustedRouter's 5.5% on provider cost (with a $0.01/M floor) buys a "
                    "different property: a gateway whose no-durable-prompt-log behavior "
                    "is backed by published source and live attestation on three clouds, "
                    "plus zdr, e2e, and eu privacy routes for every customer with no "
                    "surcharge."
                ),
            ),
            (
                ("Both gateways say they do not retain prompts. What is actually different?"),
                """The mechanism. Vercel's ZDR doc states a policy for a closed-source hosted service; you cannot check it from outside, and dashboard logs can capture full transcripts when content capture is available. TrustedRouter publishes the gateway source and live attestation endpoints on GCP, AWS Nitro, and Azure, so you can verify the running build matches the published code. On both platforms the claim stops at the gateway: downstream model providers are covered by policy, except TrustedRouter's e2e routes to confidential-compute providers.""",
            ),
            (
                "Can I keep using the AI SDK if I switch to TrustedRouter?",
                (
                    "Yes. The AI SDK is Apache-2.0 and provider-agnostic, and we maintain "
                    "an MIT-licensed AI SDK provider package. You must configure it "
                    "explicitly, because plain model strings default to Vercel's Gateway. "
                    "Realtime voice, speech, transcription, and reranking calls have no "
                    "TrustedRouter equivalent and would stay on Vercel."
                ),
            ),
        ),
        article_html="""<h2>What Vercel AI Gateway actually is</h2>
<p>Vercel AI Gateway is a proprietary hosted gateway run by Vercel, Inc. at <span class="mono">ai-gateway.vercel.sh</span>. It went <a href="https://vercel.com/blog/ai-gateway-is-now-generally-available">generally available on August 21, 2025</a> as the same routing layer that had powered v0.app. It exposes an OpenAI-compatible <span class="mono">/v1</span>, Anthropic Messages, and an OpenResponses-compatible endpoint, and it is the default provider for the Apache-2.0 AI SDK (26,000+ GitHub stars): a plain <span class="mono">creator/model</span> string in <span class="mono">generateText</span> routes through the Gateway. As of August 16, 2026, its public models endpoint lists 327 models from 35 creators across eight modalities: language, image, video, embeddings, speech, transcription, realtime, and reranking. Keys and credits live in a Vercel team account, but the Gateway works standalone. An API key and a base URL are enough; nothing has to deploy on Vercel. There is no self-host option and no public source for the gateway itself.</p>

<h2>Where Vercel is the right choice</h2>
<ul>
<li><strong>Zero token markup.</strong> <a href="https://vercel.com/docs/ai-gateway/pricing">The pricing doc</a> commits to no markup and no platform fee on tokens; you pay the provider's list price, and BYOK carries no Gateway fee either. Enterprise invoicing removes payment-processing fees entirely.</li>
<li><strong>Deepest AI SDK integration available.</strong> The Gateway is the SDK's default provider. If your app is built on <span class="mono">streamText</span> and the provider registry, no other gateway is less code.</li>
<li><strong>Modality breadth.</strong> Realtime voice, speech, transcription, and reranking sit behind the same key as chat models. Most gateways, ours included, do not cover all eight.</li>
<li><strong>Reliability and compliance.</strong> Automatic provider failover, BYOK-to-system-credential retry, a sub-20ms routing claim at GA, and <a href="https://vercel.com/docs/security/compliance">platform compliance</a>: a SOC 2 Type 2 attestation, ISO 27001:2022 certification, HIPAA BAAs with eligible Pro and Enterprise customers, PCI DSS SAQ AOCs, TISAX AL2.</li>
</ul>

<h2>The load-bearing difference: policy versus proof</h2>
<p>Vercel's <a href="https://vercel.com/docs/ai-gateway/security-and-compliance/zdr">ZDR page</a> states that the Gateway "does not retain prompts, outputs, or sensitive data." We think that policy is sincere. It is also unverifiable: the gateway is closed source with no self-host option, so no one outside Vercel can check it. Separately, the dashboard <a href="https://vercel.com/docs/ai-gateway/observability-and-spend/logs">Logs page</a> can capture full request and response transcripts when content capture is available.</p>
<p>TrustedRouter is built so the equivalent claim does not rest on policy. Ordinary synchronous and streaming inference does not write prompt or output bodies to persistent storage; what we keep is metadata: ids, model, tokens, latency, cost, region, key hash. The one exception is the opt-in Batch API, which retains enclave-encrypted artifacts for up to 30 days. The gateway source is public. It is source-available under BUSL-1.1, not OSI open source, and we say so plainly; every line that touches your prompt is inspectable. <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> publishes live attestation endpoints on GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers, plus a one-command verifier, so you can confirm the code serving <span class="mono">api.trustedrouter.com</span> matches the published release. Details are on <a href="/security">/security</a>.</p>

<h2>Retention-aware routing: default versus opt-in add-on</h2>
<p>By default the Vercel Gateway does not route based on provider data-retention policy. Filtering is opt-in and partly plan-gated: per-request <span class="mono">zeroDataRetention</span> is free on Pro and Enterprise, the team-wide ZDR toggle costs $0.10 per 1,000 requests, and a <a href="https://vercel.com/docs/ai-gateway/security-and-compliance/disallow-prompt-training">no-training filter</a> is free for all users. One gap is model-level, not Vercel-level: Vercel's ZDR doc discloses that <span class="mono">anthropic/claude-fable-5</span> supports ZDR through no provider, because Anthropic requires 30-day retention for that model — a constraint that binds any gateway routing it, ours included.</p>
<p>On TrustedRouter, privacy floors are plain model ids, available to every customer with no surcharge. <span class="mono">trustedrouter/zdr</span> restricts routing to providers with cited zero-data-retention terms, <span class="mono">trustedrouter/e2e</span> to confidential-compute providers with end-to-end encryption, and <span class="mono">trustedrouter/eu</span> to EU-focused providers; <span class="mono">provider.min_privacy</span> composes with any model. Catalog defaults are conservative: a provider is assumed to store data unless an explicit, cited flag says otherwise.</p>

<h2>Price: they are cheaper, and here are the bases</h2>
<p>The two fees sit on different bases, so state both exactly. Vercel bills tokens at the provider's list price with zero Gateway fee; payment-processing fees apply to credit purchases unless you invoice on Enterprise, and add-ons are metered. TrustedRouter bills provider cost plus 5.5% with a $0.01 per million token floor; video generation is the provider quote plus 20%; there are no seat or monthly fees. Per-route prices are on <a href="/models">/models</a> and the formula is on <a href="/pricing">/pricing</a>. On identical tokens, Vercel's fee is lower. The 5.5% pays for the attested gateway, the no-durable-log realtime path, and privacy-tier routing for every customer. Both sides support BYOK: Vercel's requires the paid tier and retries failed BYOK calls on system credentials billed to your credits; our pricing page does not publish a separate BYOK fee, so ask before assuming one.</p>

<h2>What we do not claim</h2>
<ul>
<li>Attestation covers our gateway build, not downstream model providers. Provider retention is a cited policy tier on both platforms; the exception is <span class="mono">trustedrouter/e2e</span> routes (tinfoil, phala), where the provider's own confidential-compute and E2EE mechanisms apply.</li>
<li>We publish no SOC 2 or HIPAA certification today. Vercel's platform holds SOC 2 Type 2 and ISO 27001:2022. If a certificate is a procurement gate, they clear it and we currently do not.</li>
<li>We are young. Our public repos date from late April 2026, benchmark history from June 2026, and detailed on-page uptime covers about 72 hours plus monthly rollups against a 99.99% availability target on <a href="/status">/status</a>.</li>
<li>Our catalog of roughly 550+ routes across ~49 providers centers on text, embeddings, and video. We do not match Vercel's realtime voice, speech, transcription, or reranking coverage.</li>
</ul>

<h2>Migration reality</h2>
<p>OpenAI-compatible and Anthropic-SDK callers move with a base URL and key change, then remap <span class="mono">creator/model</span> slugs to the ids on <a href="/models">/models</a>. Gateway-specific <span class="mono">providerOptions.gateway</span> fields do not carry over as-is: <span class="mono">only</span> maps to our <span class="mono">provider.only</span>, and <span class="mono">zeroDataRetention</span> maps to <span class="mono">provider.min_privacy</span> or the <span class="mono">trustedrouter/zdr</span> route. AI SDK apps need an explicit provider, because plain model strings default to Vercel's Gateway; our MIT-licensed AI SDK provider package handles that. Not carried over: realtime sessions, reranking, OIDC-token auth from Vercel deployments, Gateway routing rules, and dashboard budgets and BYOK keys, which are Vercel-side configuration you would recreate. We do not yet have a dedicated migrate-from-Vercel doc. Start with one streamed request and compare output, latency, and billed usage on both sides.</p>""",
        custom_page=True,
    ),
    _comparison(
        slug="litellm",
        name="LiteLLM",
        category="Self-hosted AI gateway",
        summary=(
            "LiteLLM is a self-hosted, MIT-core gateway claiming 140+ providers; you run "
            "it on your own infrastructure and pay providers directly. TrustedRouter is a "
            "hosted router at provider price + 5.5% whose gateway build you can verify by "
            "live attestation on three clouds."
        ),
        competitor_fit=(
            "Choose LiteLLM when prompts must never transit a vendor's network and you "
            "can operate the deployment: Postgres, Redis once you scale past one "
            "instance, upgrades, and on-call. The $0 MIT core ships virtual keys, teams, "
            "budgets, fallbacks, and Prometheus metrics, and its claimed 140+ provider "
            "catalog is the broadest in the self-hosted category."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want the routing layer without operating it "
            "and want the trust question answered with evidence: a gateway build attested "
            "live on GCP, AWS, and Azure, no durable prompt or output logs on realtime "
            "inference, and per-request privacy-tier routes (zdr, e2e, eu) at provider "
            "price + 5.5% prepaid."
        ),
        migration=(
            "Both expose OpenAI-compatible APIs: change the base URL and swap the LiteLLM "
            "virtual key for a TrustedRouter API key (both are ordinary Bearer tokens, "
            "not special headers), then remap your config.yaml model aliases to "
            "TrustedRouter model ids from /models. Per-team budgets, guardrails, semantic "
            "caching, and logging callbacks do not carry over; recreate what has a "
            "TrustedRouter equivalent or keep LiteLLM running beside it during cutover."
        ),
        deployment="Self-hosted only (Docker/K8s); Postgres required, Redis for multi-instance",
        api="OpenAI-compatible proxy plus native passthrough (e.g. Anthropic Messages)",
        catalog=(
            "Claims 140+ providers and 1,892 models; add OpenAI-compatible endpoints via "
            "one JSON file"
        ),
        routing="Load balancing, retries, fallbacks, cooldowns, per-key budgets, semantic caching",
        observability=(
            "Admin UI spend logs, free Prometheus /metrics, callbacks: Langfuse, OTEL, Datadog"
        ),
        content=(
            "Content logging off by default; metadata spend logs in Postgres; auto-delete "
            "is Enterprise"
        ),
        verification=(
            "Your deployment audit; recertifying SOC 2 Type II and ISO 27001 with Vanta (Mar 2026)"
        ),
        billing=(
            "$0 core; pay providers directly on your keys; Enterprise is custom annual pricing"
        ),
        sources=(
            ComparisonSource(
                "LiteLLM homepage (provider and model counts)", "https://www.litellm.ai/"
            ),
            ComparisonSource(
                "LiteLLM pricing (self-hosted only; $0 core; Enterprise)",
                "https://www.litellm.ai/pricing",
            ),
            ComparisonSource(
                "LiteLLM production best practices (Postgres/Redis, sizing)",
                "https://docs.litellm.ai/docs/proxy/prod",
            ),
            ComparisonSource(
                "LiteLLM data privacy and security doc",
                "https://docs.litellm.ai/docs/data_security",
            ),
            ComparisonSource(
                "LiteLLM UI logs doc (content logging off by default)",
                "https://docs.litellm.ai/docs/proxy/ui_logs",
            ),
            ComparisonSource(
                "LiteLLM Rust gateway announcement (June 2026)",
                "https://docs.litellm.ai/blog/litellm-rust-launch",
            ),
            ComparisonSource(
                "LiteLLM Vanta recertification post (March 2026)",
                "https://docs.litellm.ai/blog/vanta-compliance-recertification",
            ),
            ComparisonSource(
                "TechInformed on the March 2026 PyPI incident",
                "https://techinformed.com/litellm-says-it-will-recertify-with-vanta-after-pypi-malware-incident/",
            ),
        ),
        faq_items=(
            (
                "Can TrustedRouter match self-hosted data sovereignty?",
                (
                    "No. Self-hosted LiteLLM means your prompts never transit a vendor's "
                    "network, and no hosted service can match that structurally. Our "
                    "answer is different: prompts pass through a gateway whose running "
                    "build is attested on three clouds, and realtime inference keeps no "
                    "durable prompt or output logs. If the requirement is no third-party "
                    "network at all, LiteLLM wins it outright."
                ),
            ),
            (
                "Is TrustedRouter cheaper than a free gateway?",
                (
                    "The bases differ. LiteLLM software is $0 and you pay providers "
                    "directly, plus your own Postgres, Redis, and operations — their "
                    "production guide sizes workers at 1 vCPU and 4Gi each. TrustedRouter "
                    "prepaid bills the provider's token price + 5.5% with a $0.01/M "
                    "floor; BYOK is supported but no separate BYOK fee is published. At "
                    "small scale the 5.5% is usually less than the operator time; at "
                    "large scale, run the numbers."
                ),
            ),
            (
                "Do I lose model coverage moving off LiteLLM?",
                (
                    "Possibly. LiteLLM claims 140+ providers and 1,892 models, and adding "
                    "an OpenAI-compatible endpoint is a one-file JSON edit. TrustedRouter "
                    "has roughly 550 model routes across about 49 providers as of "
                    "2026-08-16. Check /models against the aliases in your config.yaml "
                    "before committing; a hybrid — LiteLLM for long-tail and internal "
                    "models, TrustedRouter for attested privacy-tier routes — is a "
                    "legitimate landing point."
                ),
            ),
        ),
        article_html="""<h2>What LiteLLM actually is</h2>
<p>LiteLLM is BerriAI's self-hosted AI gateway: a proxy server plus a Python SDK that put one OpenAI-compatible API in front of the widest claimed provider catalog in the self-hosted category. As of August 2026 the <a href="https://www.litellm.ai/">LiteLLM homepage</a> claims 140+ providers and 1,892 models (the GitHub README still says 100+), and adding another OpenAI-compatible endpoint is a single JSON edit. Adoption is real: 56,465 GitHub stars, a self-reported 240M+ Docker pulls, and NVIDIA, Netflix, and Okta among the named users.</p>
<p>Two facts define its shape. First, there is no hosted tier — the <a href="https://www.litellm.ai/pricing">pricing page</a> describes an exclusively self-hosted product, so using LiteLLM means operating it: Docker or Kubernetes in your infrastructure, PostgreSQL required in production, Redis once you run a second instance, workers sized at roughly 1 vCPU and 4Gi each per their <a href="https://docs.litellm.ai/docs/proxy/prod">production guide</a>. Second, the license is MIT for the core with a proprietary <span class="mono">enterprise/</span> carve-out; SSO, SCIM, audit logs, and automated spend-log deletion sit behind a custom-priced annual Enterprise tier.</p>
<p>The hot path is also mid-rewrite. In June 2026 BerriAI <a href="https://docs.litellm.ai/blog/litellm-rust-launch">announced a Rust core</a> benchmarked at about 15x the Python throughput (453 to 6,782 requests per second), rolling out route by route with a zero-breaking-change commitment through December 2026. That is serious performance investment — and a moving target for the rest of the year.</p>

<h2>Where LiteLLM is the right choice</h2>
<p>If your requirement is that prompts never transit a third party's network, self-hosting satisfies it structurally and no hosted gateway can. LiteLLM's data-security doc states it plainly: "No data or telemetry is stored on LiteLLM Servers when you self-host." The defaults are privacy-respecting too — prompt and response content logging is off unless you enable it, and spend logs record metadata only (tokens, cost, model, timing).</p>
<p>The $0 core is substantial: virtual keys, teams, per-key budgets and rate limits, fallbacks, load balancing, semantic caching, and a free Prometheus <span class="mono">/metrics</span> endpoint. Teams that need long-tail providers, or want internal fine-tuned deployments behind the same proxy as commercial APIs, get a catalog and extension path we do not match.</p>

<h2>Three differences that decide it</h2>
<h3>Who you trust, and how you check</h3>
<p>Self-hosted LiteLLM asks you to trust your own operations: your upgrade discipline, your CI, your supply chain. That model was stress-tested in March 2026, when malicious <span class="mono">litellm</span> 1.82.7 and 1.82.8 packages built to harvest credentials were live on PyPI for about 40 minutes after a CI compromise. BerriAI rotated credentials, brought in Mandiant, and <a href="https://docs.litellm.ai/blog/vanta-compliance-recertification">announced in March 2026 that it is recertifying SOC 2 Type II and ISO 27001 with Vanta</a>. TrustedRouter answers the trust question with a different mechanism: <span class="mono">api.trustedrouter.com</span> runs inside confidential-compute enclaves on GCP, AWS, and Azure, and each endpoint serves a live attestation binding the running build to published source and signed releases. Verify it yourself at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> — one script, no account.</p>
<h3>Where prompt bytes can end up</h3>
<p>LiteLLM's defaults are good, and they are configuration: content storage is one flag away (<span class="mono">store_prompts_in_spend_logs: true</span>), cached prompts and completions are stored unencrypted per their security FAQ, and automated spend-log deletion is Enterprise-gated. On TrustedRouter, realtime inference keeps no durable prompt or output logs at all; we retain operational metadata (ids, model, tokens, latency, cost, region, key hash). Batch is a separate opt-in mode with enclave-encrypted retention up to 30 days. Their guarantee lives in your config; ours lives in the shipped code path, and the attestation is how you check what shipped. Details on <a href="/security">/security</a>.</p>
<h3>Operations and money</h3>
<p>The bases differ, so here are both. LiteLLM software costs $0; you pay providers directly on your own keys, plus the Postgres/Redis fleet, upgrades, and on-call. TrustedRouter prepaid bills the provider's token price plus 5.5% with a $0.01 per million token floor; BYOK is supported, though we publish no separate BYOK fee. Exact per-route prices are on <a href="/models">/models</a>, the formula on <a href="/pricing">/pricing</a>. The fee buys provider failover and per-request privacy routing: <span class="mono">trustedrouter/zdr</span> restricts to providers with contractual or policy zero data retention, <span class="mono">trustedrouter/e2e</span> to confidential-compute providers (tinfoil, phala), <span class="mono">trustedrouter/eu</span> to an EU provider order, all composable with request preferences. Whether 5.5% beats an engineer's time running the fleet is your arithmetic; at low volume the ops cost usually dominates the $0 sticker, at high volume it may not.</p>

<h2>What we do not claim</h2>
<ul>
<li>Attestation covers our gateway, not model providers. Downstream providers are policy and contract tiers; only <span class="mono">trustedrouter/e2e</span> routes run on providers with their own confidential-compute and end-to-end-encryption posture, and those are the providers' mechanisms.</li>
<li>Our gateway and control plane are source-available (BUSL-1.1, converting to Apache-2.0 four years after each release), not open source. LiteLLM's MIT core is the more permissive license. Our SDKs are Apache-2.0 or MIT.</li>
<li>We are young: our public repos date from late April 2026 and public benchmark history from June 2026. LiteLLM has shipped since 2023.</li>
<li>We publish no SOC 2 or HIPAA certification today; our DPA, BAA, and subprocessor list are public legal documents.</li>
<li>Our catalog is roughly 550 model routes across about 49 providers as of 2026-08-16 — well short of LiteLLM's claimed 1,892 models.</li>
</ul>

<h2>Migration, honestly</h2>
<p>Both sides speak OpenAI format, so the first request is small: point <span class="mono">base_url</span> at TrustedRouter and swap the LiteLLM virtual key for a TrustedRouter API key — both are ordinary Bearer tokens, no special headers. The real work is names: LiteLLM model names are deployment-defined aliases in your <span class="mono">config.yaml</span>, and each alias needs mapping to a TrustedRouter id from <a href="/models">/models</a>. Fallback lists map to our <span class="mono">models[]</span> arrays and routes. What does not carry over: per-team virtual keys and budgets, guardrails, semantic caching, and logging callbacks need TrustedRouter equivalents or stay in your stack, and we have no dedicated LiteLLM migration guide yet (our only migration doc covers OpenRouter). A legitimate end state is hybrid: keep LiteLLM for long-tail and internal deployments, route the traffic that needs attestation and privacy tiers through us, and compare bills after a month.</p>""",
        custom_page=True,
    ),
    _comparison(
        slug="cloudflare-ai-gateway",
        name="Cloudflare AI Gateway",
        category="Hosted edge AI gateway",
        summary=(
            "Cloudflare AI Gateway is a closed-source control plane on Cloudflare's "
            "global edge whose core features are free, with caching, Dynamic Routing, and "
            "unified billing, merged with Workers AI in August 2026. TrustedRouter is a "
            "source-available gateway attested on three clouds that keeps realtime prompt "
            "content out of durable logs by default."
        ),
        competitor_fit=(
            "Choose Cloudflare AI Gateway when you already run on Cloudflare and want a "
            "free gateway: caching, rate limiting, analytics, and DLP scanning cost "
            "nothing on any plan, Dynamic Routing gives no-code A/B splits and "
            "budget-triggered fallbacks, and since August 2026 Workers AI shares the same "
            "control plane and billing."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when prompt handling needs proof rather than "
            "configuration: the gateway build is attested live on GCP, AWS, and Azure, "
            "realtime inference keeps prompt and output content out of durable logs by "
            "default, and privacy routes enforce a per-request floor (zdr, e2e) and "
            "trustedrouter/eu pins an EU-focused provider set."
        ),
        migration="""OpenAI-compatible clients swap the Cloudflare base URL and API token for the TrustedRouter base URL and key, then map provider-prefixed model ids; dynamic/{route} pseudo-models have no direct equivalent and must be rebuilt as routing preferences. Cloudflare-side config does not carry over: dynamic routes, caching rules, spend limits, Guardrails and DLP policies, and Logpush jobs stay behind, and prepaid Unified Billing credits do not transfer. BYOK keys in Secrets Store must be re-provisioned as direct provider keys or TrustedRouter BYOK.""",
        deployment=(
            "Closed-source hosted service on Cloudflare's global edge; core gateway free "
            "on all plans"
        ),
        api=(
            "REST /ai/run, OpenAI Chat and Responses, Anthropic Messages; old compat path "
            "deprecated"
        ),
        catalog=(
            "24 named providers including Workers AI, unified into one control plane on 2026-08-07"
        ),
        routing=(
            "Dynamic Routing: conditional branches, A/B splits, budget quotas, retries, rollback"
        ),
        observability="Persistent logs (100k free, 10M/gateway paid), Logpush export, User Insights",
        content="Logs prompts and responses by default; opt out per gateway or per-request header",
        verification="Corporate SOC 2 Type II and ISO 27001; gateway is closed source, no attestation",
        billing=(
            "Core free; Unified Billing +5% on credit purchases, 6 providers; BYOK via "
            "Secrets Store"
        ),
        sources=(
            ComparisonSource(
                "Cloudflare AI Gateway pricing",
                "https://developers.cloudflare.com/ai-gateway/reference/pricing/",
            ),
            ComparisonSource(
                "Cloudflare AI Gateway logging docs",
                "https://developers.cloudflare.com/ai-gateway/observability/logging/",
            ),
            ComparisonSource(
                "Unified Billing and Zero Data Retention docs",
                "https://developers.cloudflare.com/ai-gateway/features/unified-billing/",
            ),
            ComparisonSource(
                "Cloudflare AI Gateway providers list",
                "https://developers.cloudflare.com/ai-gateway/usage/providers/",
            ),
            ComparisonSource(
                "Dynamic Routing docs",
                "https://developers.cloudflare.com/ai-gateway/features/dynamic-routing/",
            ),
            ComparisonSource(
                "Workers AI and AI Gateway unification announcement",
                "https://blog.cloudflare.com/workers-ai-gateway-unification/",
            ),
            ComparisonSource(
                "Cloudflare SOC 2 trust hub",
                "https://www.cloudflare.com/trust-hub/compliance-resources/soc-2/",
            ),
        ),
        faq_items=(
            (
                "Isn't Cloudflare AI Gateway free while TrustedRouter charges a fee?",
                (
                    "Yes. Cloudflare's core gateway (caching, rate limiting, analytics, "
                    "logging, DLP) is free on every plan, and traffic on your own "
                    "provider keys carries no Cloudflare per-token fee; Unified Billing "
                    "adds 5% only when you purchase credits. TrustedRouter bills prepaid "
                    "text and embeddings at the provider's token price plus 5.5% with a "
                    "$0.01/M floor. If the free tier's defaults fit your privacy posture, "
                    "Cloudflare is cheaper; the 5.5% pays for an attested gateway path "
                    "with no durable prompt logs."
                ),
            ),
            (
                "Both offer zero data retention. How do the two ZDR modes differ?",
                """Cloudflare's ZDR (added November 2025) is opt-in and scoped: it routes Unified Billing traffic to provider endpoints that do not retain content, and Cloudflare's docs state it does not apply to BYOK; separately, gateway logging of prompts and responses is on by default until you turn it off. TrustedRouter's realtime path keeps prompt and output bodies out of durable storage for every request by default, and trustedrouter/zdr adds a per-request floor restricted to providers with cited zero-retention terms. Our attestation covers the gateway build, not downstream providers.""",
            ),
            (
                "We need SOC 2 for procurement. Which one passes review?",
                (
                    "Cloudflare. It has held SOC 2 Type II since 2019 and carries ISO "
                    "27001, 27701, and 27018 company-wide. As of August 2026 "
                    "TrustedRouter has no published SOC 2 or HIPAA certification; we "
                    "publish a DPA, BAA, and subprocessor list, plus live attestation "
                    "evidence at trust.trustedrouter.com. If a certification checkbox is "
                    "mandatory today, Cloudflare clears that bar and we do not."
                ),
            ),
        ),
        article_html="""<h2>What Cloudflare AI Gateway actually is</h2><p>Cloudflare AI Gateway is a hosted control plane that sits between your application and model providers, running on Cloudflare's global edge network. Cloudflare operates it; there is nothing to self-host, and the gateway's source is not published (peripheral SDK packages in <span class="mono">cloudflare/ai</span> are MIT). Integration is a base-URL change, and the core product is <a href="https://developers.cloudflare.com/ai-gateway/reference/pricing/">free on every plan</a>: analytics, caching, rate limiting, request logging, and DLP scanning cost nothing.</p><p>The product is moving fast. A REST API shipped on 2026-05-21 with a universal <span class="mono">/ai/run</span> endpoint plus OpenAI Chat, OpenAI Responses, and Anthropic Messages schemas. On 2026-08-07 Cloudflare merged Workers AI and AI Gateway into a single control plane with shared endpoints and billing. The <a href="https://developers.cloudflare.com/ai-gateway/usage/providers/">providers page</a> lists 24 named providers, Workers AI among them, including dedicated speech and media vendors such as Deepgram, ElevenLabs, and Fal.</p><h2>Where Cloudflare is the right choice</h2><p>Three things are genuinely strong. Price: the core gateway is free, and Unified Billing adds a 5% fee on credit purchases (a $100 purchase charges $105) with inference passed through at provider price. Routing: <a href="https://developers.cloudflare.com/ai-gateway/features/dynamic-routing/">Dynamic Routing</a> is a versioned visual editor with conditional branches, percentage splits for A/B tests, budget and rate-limit quotas that switch to fallbacks, and automatic retries on provider failures. Compliance: Cloudflare has held SOC 2 Type II since 2019 and carries ISO 27001, 27701, and 27018. If your stack already fronts through Cloudflare and procurement wants a large public company with a long audit history, it is a defensible default.</p><h2>Prompt logging is the fork in the road</h2><p>Cloudflare's <a href="https://developers.cloudflare.com/ai-gateway/observability/logging/">logging is on by default</a> and stores the user prompt and model response along with tokens, cost, and duration. Logs persist until you delete them or hit the storage cap: 100,000 logs total on free accounts, 10 million per gateway on Workers Paid ($5/month base). You can opt out per gateway or per request (<span class="mono">cf-aig-collect-log</span>). Since November 2025 there is also an opt-in <a href="https://developers.cloudflare.com/ai-gateway/features/unified-billing/">Zero Data Retention mode</a> that routes Unified Billing traffic to provider endpoints that do not retain content; Cloudflare's docs state it does not apply to BYOK.</p><p>TrustedRouter inverts the default. Ordinary synchronous and streaming inference does not write prompt or output content to persistent storage; what we keep is metadata: request ids, model and provider, token counts, latency, cost, region, and an API-key hash (<a href="/privacy">privacy page</a>). Batch is a separate opt-in mode with enclave-encrypted artifacts retained up to 30 days. Privacy is also routable per request: <span class="mono">trustedrouter/zdr</span> restricts to providers with cited zero-retention terms, <span class="mono">trustedrouter/e2e</span> restricts to confidential-compute providers with provider-side end-to-end encryption (currently tinfoil and phala), and <span class="mono">trustedrouter/eu</span> pins an EU-focused provider set. See <a href="/models">/models</a> for every route.</p><h2>Verification: what can you actually check?</h2><p>With Cloudflare, assurance is documentation and audits. Those are real, and the SOC 2 report is available under NDA, but the gateway's code is not published, so "logging is off" is a setting you take on trust.</p><p>Our gateway's source is public under BUSL-1.1 (source-available, converting to Apache-2.0 four years after each release; the SDKs are Apache-2.0 or MIT), and the running build is attested on three clouds: GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers. Anyone can fetch a live attestation from the endpoints on <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> and check the measurement against published release digests with one script. The boundary is precise and worth stating plainly: attestation covers the gateway build, not downstream model providers. Provider retention on <span class="mono">zdr</span> routes is a contractual and policy tier, cited per provider; on the <span class="mono">e2e</span> routes the provider's own confidential-compute and E2EE mechanisms apply. Details are on <a href="/security">/security</a>.</p><h2>Pricing, on stated bases</h2><p>The two fee numbers look similar and sit on different bases. Cloudflare charges 5% when you purchase Unified Billing credits, then passes inference through at provider price; the mode covers 6 providers and is limited to 200 requests per 60 seconds per gateway, and the free core gateway adds no fee to traffic on your own provider keys. TrustedRouter charges the provider's token price plus 5.5% on each prepaid request, with a $0.01 per million token floor on free and near-zero routes; video is the provider quote plus 20%; there is no monthly fee (<a href="/pricing">pricing</a>). BYOK is supported on both sides; we do not publish a separate BYOK fee, while Cloudflare's core gateway adds no fee to BYOK traffic — compare against your own quote.</p><h2>What we do not claim</h2><p>We are young. Our repos have been public since late April 2026 and public benchmark history starts June 2026; Cloudflare's audit trail goes back years. We publish a DPA, BAA, and subprocessor list, but as of August 2026 we have no published SOC 2 or HIPAA certification. Our published pricing covers text, embeddings, and video generation; if you rely on Cloudflare's speech vendors (Deepgram, ElevenLabs, Cartesia), check <a href="/models">/models</a> for equivalents before committing. Our availability number is a published 99.99% target with live burn rates on the <a href="/status">status page</a>, and our on-page uptime history is short.</p><h2>Moving over</h2><p>If you call Cloudflare's OpenAI-compatible endpoints, the change is the base URL, the auth (Cloudflare API token to TrustedRouter key), and model naming. Everything configured in the Cloudflare dashboard stays there: dynamic routes, caching rules, spend limits, Guardrails and DLP policies, and Logpush jobs have no importable equivalent, and prepaid credits do not transfer. The code change is small; the config rebuild is the real work. Start with one streamed request and compare output, latency, and billed usage before moving traffic.</p>""",
    ),
    _comparison(
        slug="portkey",
        name="Portkey",
        category="AI gateway and observability",
        summary=(
            "Portkey pairs a gateway with the most complete single-vendor observability "
            "stack in the category — and, since May 2026, Palo Alto Networks ownership; "
            "its hosted platform logs full request content by default to power that "
            "product. TrustedRouter routes realtime traffic with no durable prompt logs "
            "and a hardware-attested gateway build you can verify."
        ),
        competitor_fit=(
            "Choose Portkey when prompt-level logs, traces, guardrails, and prompt "
            "management are your operating model: it is the most complete single-vendor "
            "stack in this category, with a free tier, a $49/month Production plan, SOC 2 "
            "Type 2 and HIPAA at the Enterprise tier, a hybrid VPC deployment mode, and "
            "Palo Alto Networks behind it."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when prompt content must not persist anywhere on the "
            "gateway and you want that checkable rather than contractual: realtime "
            "routing with no durable prompt or output logs, privacy-tier routes (zdr, "
            "e2e, eu), and a gateway build verified against live attestation on three "
            "clouds."
        ),
        migration=(
            "Point your OpenAI-compatible client at the TrustedRouter base URL and "
            "replace x-portkey-* headers with one TrustedRouter key. Re-express Portkey "
            "Configs (fallbacks, load balancing, retries, caching) as TrustedRouter "
            "routing preferences where equivalents exist — provider ordering, models[] "
            "fallbacks, privacy floors, and export prompt templates from Prompt Studio "
            "first — they are hosted assets and do not carry over. Guardrail hooks and "
            "logged history do not migrate."
        ),
        deployment=(
            "Self-host MIT gateway, hosted SaaS, or hybrid: VPC data plane + hosted control plane"
        ),
        api=("OpenAI-compatible unified API via SDK or x-portkey-* headers; 40+ provider adapters"),
        catalog="Claims 1,600+ LLMs across 40+ providers; 50+ guardrails",
        routing=(
            "Fallbacks, load balancing, retries, conditional routing, simple + semantic caching"
        ),
        observability="Logs, traces, metrics, alerts, dashboards; plans meter recorded logs",
        content="Logs full content by default; org-wide metrics-only mode is Enterprise-only",
        verification="SOC 2 Type 2, ISO 27001, HIPAA audits; config + contract, no runtime attestation",
        billing=(
            "Free 10k logs/mo; $49 for 100k + $9/extra 100k; inference on your own provider keys"
        ),
        sources=(
            ComparisonSource(
                "Portkey pricing (tiers, log quotas, retention)", "https://portkey.ai/pricing"
            ),
            ComparisonSource(
                "Portkey logs and DO NOT TRACK documentation",
                "https://portkey.ai/docs/product/observability/logs",
            ),
            ComparisonSource(
                "Portkey request-logging modes (Enterprise Metrics Only)",
                "https://portkey.ai/docs/product/administration/configuring-request-logging",
            ),
            ComparisonSource(
                "Portkey hybrid private-cloud deployment doc",
                "https://portkey.ai/docs/product/enterprise-offering/private-cloud-deployments",
            ),
            ComparisonSource(
                "Portkey security and compliance doc",
                "https://portkey.ai/docs/product/enterprise-offering/security-portkey",
            ),
            ComparisonSource(
                "Portkey gateway repository (MIT)", "https://github.com/Portkey-AI/gateway"
            ),
            ComparisonSource(
                "Palo Alto Networks completes Portkey acquisition (2026-05-29)",
                "https://www.paloaltonetworks.com/company/press/2026/palo-alto-networks-completes-acquisition-of-portkey-to-secure-ai-agents",
            ),
            ComparisonSource(
                "TrustedRouter attestation trust page", "https://trust.trustedrouter.com"
            ),
        ),
        faq_items=(
            (
                (
                    "Can TrustedRouter replace Portkey's observability, guardrails, and "
                    "prompt management?"
                ),
                (
                    "Mostly no, and we want to be plain about it. TrustedRouter keeps "
                    "metadata only — ids, model, tokens, latency, cost, region — with "
                    "opt-in Broadcast export for content you choose to send to your own "
                    "destination. There is no hosted log viewer with prompt bodies, no "
                    "trace UI, no guardrails engine, no prompt studio. If prompt-level "
                    "debugging and guardrails are central to how your team works, Portkey "
                    "is the stronger product for that job."
                ),
            ),
            (
                ("Is Portkey's Metrics Only mode equivalent to TrustedRouter's no-log design?"),
                (
                    "No. Portkey's org-wide Metrics Only mode is an Enterprise-plan "
                    "feature; lower tiers log full content by default with a per-request "
                    "opt-out header, and switching modes does not purge already-logged "
                    "data. It is also a configuration you trust rather than verify. "
                    "TrustedRouter's realtime path never durably stores prompt or output "
                    "content on any plan, and the gateway build serving that promise is "
                    "hardware-attested on three clouds with a public verifier at "
                    "trust.trustedrouter.com."
                ),
            ),
            (
                "Does the Palo Alto Networks acquisition change the calculus?",
                (
                    "It depends on your stack. If you already run Palo Alto security "
                    "products, Portkey as the gateway core of Prisma AIRS is a real "
                    "integration advantage. If you rely on the open-source gateway, note "
                    "that as of August 16, 2026 the main branch has had no pushes since "
                    "May 25, 2026 and the last tagged release was January 2026; the "
                    "roadmap is now oriented around Prisma AIRS. Self-hosting the MIT "
                    "gateway remains viable today either way."
                ),
            ),
        ),
        article_html="""<h2>What Portkey is in 2026</h2>
<p>Portkey is a gateway built around an observability platform: request logs, traces, analytics, 50+ guardrails (their count), prompt management, governance, and an MCP gateway from one vendor. It ships in two parts. The <a href="https://github.com/Portkey-AI/gateway">gateway</a> is MIT-licensed TypeScript with 12,740 GitHub stars as of August 16, 2026; its README claims routing to 1,600+ LLMs across 40+ providers. The hosted platform — log store, dashboards, prompt studio, RBAC — is proprietary, and it is what <a href="https://portkey.ai/pricing">Portkey's pricing page</a> sells.</p>
<p>The most material fact about Portkey in 2026 is ownership. Palo Alto Networks announced intent to acquire on April 30, 2026 and <a href="https://www.paloaltonetworks.com/company/press/2026/palo-alto-networks-completes-acquisition-of-portkey-to-secure-ai-agents">completed the acquisition on May 29, 2026</a>; Portkey is now the foundational AI gateway of Prisma AIRS, Palo Alto's AI security platform. In March 2026 Portkey announced a $15M Series A and a fully open-source Gateway 2.0. As of August 16, 2026 that 2.0 work sits on a pre-release branch with no root LICENSE file, and the main branch has had no pushes since May 25, 2026. The commercial product is active; the post-acquisition open-source cadence is unproven.</p>
<p>Scale, company-reported in March 2026: 24,000+ organizations, 1 trillion+ tokens and 120M+ requests processed daily. We could not verify those figures independently, and we know of no reason to doubt them.</p>
<h2>Where Portkey is genuinely strong</h2>
<ul>
<li><strong>One vendor, whole stack.</strong> Teams that debug from prompt-level logs and traces, gate outputs with guardrails, and version prompts in a studio get all of it behind one API. TrustedRouter does none of that; our observability is metadata-only by design.</li>
<li><strong>Cheap entry.</strong> A free Developer tier (10k recorded logs per month) and a $49/month Production tier (100k logs, $9 per additional 100k) buy the working platform.</li>
<li><strong>Audited compliance.</strong> <a href="https://portkey.ai/docs/product/enterprise-offering/security-portkey">Portkey's security documentation</a> lists SOC 2 (Type 2 at Enterprise), ISO 27001, GDPR, and HIPAA with third-party audits. We publish a DPA, BAA, and subprocessor list, and no third-party certification today.</li>
<li><strong>Hybrid VPC deployment.</strong> Enterprise customers run the data plane — gateway and log store — inside their own network; <a href="https://portkey.ai/docs/product/enterprise-offering/private-cloud-deployments">Portkey's deployment docs</a> state LLM traffic stays inside your network boundary in that mode. The control plane stays Portkey-hosted, and configs, including provider API keys, sync to it.</li>
</ul>
<h2>What happens to your prompts</h2>
<p>This is the load-bearing difference, so we will be precise. Portkey's hosted observability works because it stores content: the <a href="https://portkey.ai/docs/product/observability/logs">logs documentation</a> describes a chronological view of every request processed through the gateway, request and response bodies included, retained 3 days on the free tier and 30 days on Production. Opting out is per-request (<span class="mono">x-portkey-debug: false</span>), and the org-wide Metrics Only mode is an <a href="https://portkey.ai/docs/product/administration/configuring-request-logging">Enterprise plan feature</a>. Switching modes is not retroactive — content already logged stays until support purges it on request.</p>
<p>TrustedRouter is built in the opposite order. Realtime inference never writes prompt or output content to durable storage, on any plan. What we keep is metadata: request and generation ids, model and provider, token counts, latency, status, cost, region, and an API-key hash (<a href="/privacy">privacy page</a>). Batch is a separate opt-in mode with enclave-encrypted retention up to 30 days. Teams that want content-level analytics can enable Broadcast to send selected content to their own destination; we do not retain a copy.</p>
<p>The second half of the difference is proof. Portkey's privacy posture is configuration plus contract; nothing in its docs attests that a given logging mode is in effect on the serving path. TrustedRouter publishes live hardware attestation for the gateway on three clouds — GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers — plus a verifier script that checks the running enclave against published source and release digests. Start at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> and <a href="/security">/security</a>.</p>
<h2>Fees on different bases</h2>
<p>Do not compare the numbers directly; the meters differ. Portkey prices recorded logs — free at 10k per month (the pricing page itself calls that tier unsuitable for production), $49/month for 100k, $9 per additional 100k, custom at Enterprise — while model inference bills separately through your own provider keys. TrustedRouter has no subscription: prepaid text and embeddings bill the provider's token price plus 5.5% with a $0.01 per million token floor, video is the provider quote plus 20%, and every route's price is listed on <a href="/models">/models</a> (<a href="/pricing">pricing details</a>). BYOK is supported; we do not publish a separate BYOK fee. One meter charges for observability volume, the other marks up tokens. Which is cheaper depends on your traffic shape.</p>
<h2>What we do not claim</h2>
<ul>
<li>Attestation covers our gateway, not model providers. Downstream handling is tracked policy and contract per provider, except <span class="mono">trustedrouter/e2e</span>, which routes only to providers running confidential compute with provider-side end-to-end encryption (Tinfoil, Phala) — their mechanisms, not our attestation.</li>
<li>Our repos are young: public since late April and early May 2026. Portkey's MIT gateway has 12,740 stars and 1,247 forks; ours has months of public history.</li>
<li>Our gateway and control plane are source-available under BUSL-1.1, converting to Apache-2.0 four years after each release; the SDKs are Apache-2.0 and MIT. Portkey's gateway on main carries the more permissive license. Every line of ours that touches your prompt is public.</li>
<li>No published SOC 2 or HIPAA certification today. If audited certification is a hard requirement now, Portkey Enterprise has it and we do not.</li>
</ul>
<h2>Migration reality</h2>
<p>Plain proxy usage moves in minutes: point the OpenAI SDK at the TrustedRouter base URL and replace <span class="mono">x-portkey-*</span> headers with one key. The real work is elsewhere. Portkey Configs — fallback, load-balancing, retry, and cache JSON — must be re-expressed as TrustedRouter routing preferences: provider ordering, <span class="mono">models[]</span> fallback arrays, and privacy floors. Prompt templates live in Portkey's hosted Prompt Studio and render through its API, so export them before you leave. Guardrail hooks have no TrustedRouter equivalent and need re-implementation in your application. Logged history stays behind under Portkey's retention windows. We have no Portkey-specific importer; our only dedicated migration guide today covers OpenRouter.</p>""",
    ),
    _comparison(
        slug="helicone",
        name="Helicone",
        category="LLM observability and gateway",
        summary=(
            "Helicone is an Apache-2.0 LLM observability platform with an AI gateway, in "
            "maintenance mode since Mintlify acquired it on March 3, 2026. TrustedRouter "
            "is an actively developed model router whose realtime path keeps no durable "
            "prompt logs and whose gateway build is attested live on three clouds."
        ),
        competitor_fit=(
            "Choose Helicone when request-level observability is the job: full-body logs, "
            "session traces, HQL, prompt management, and evals, with a real free tier and "
            "an Apache-2.0 monorepo you can self-host. Weigh that against maintenance "
            "mode: since the March 2026 Mintlify acquisition, feature development has "
            "stopped."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want one prepaid API across 550+ model routes "
            "(as of August 2026) where ordinary synchronous and streaming inference keeps "
            "no durable prompt or output logs, with a gateway build you can verify by "
            "live attestation on three clouds. You accept metadata-only observability in "
            "exchange."
        ),
        migration="""Cloud-gateway users point the OpenAI-compatible base URL at TrustedRouter, swap the API key, and remap model ids; classic passthrough users change per-provider base URLs and drop the Helicone-Auth and Helicone-* headers; async-logging users have nothing in the request path to change. What does not carry over: request logs, dashboards, HQL queries, prompt versions, and evals have no portable format, and TrustedRouter keeps metadata only, so export what you need before your plan's retention window (7 days to 3 months on non-Enterprise tiers) expires. Mintlify has said it will help Helicone customers migrate to another platform.""",
        deployment=(
            "Hosted (US/EU) or self-hosted Apache-2.0 platform; maintenance mode since Mar 2026"
        ),
        api=(
            "OpenAI-compatible cloud gateway, per-provider passthrough domains, or async log SDKs"
        ),
        catalog=(
            "100+ models via cloud gateway; pass-through billing credits or BYOK provider keys"
        ),
        routing="Load balancing, automatic provider fallback, caching, and custom rate limits",
        observability="Full-body request logs, sessions, users, HQL, prompts, datasets, evals, alerts",
        content="Bodies logged by default; omit headers skip storage but content still transits",
        verification=(
            "Apache-2.0 source and self-hosting; documented SOC 2; SOC 2 & HIPAA plan "
            "features on Team ($799/mo) and up"
        ),
        billing=(
            "Plans $0-$799/mo plus usage-based overage; credits priced at 0% markup (waitlist)"
        ),
        sources=(
            ComparisonSource(
                "Helicone: Joining Mintlify announcement (2026-03-03)",
                "https://www.helicone.ai/blog/joining-mintlify",
            ),
            ComparisonSource(
                "Mintlify acquisition announcement",
                "https://www.mintlify.com/blog/mintlify-acquires-helicone",
            ),
            ComparisonSource(
                "Helicone pricing (plans, overage, retention)", "https://www.helicone.ai/pricing"
            ),
            ComparisonSource(
                "Helicone omit-logs documentation",
                "https://docs.helicone.ai/features/advanced-usage/omit-logs",
            ),
            ComparisonSource(
                "Helicone AI Gateway overview", "https://docs.helicone.ai/gateway/overview"
            ),
            ComparisonSource(
                "Helicone credits: 0% markup pass-through billing",
                "https://www.helicone.ai/credits",
            ),
            ComparisonSource(
                "Helicone monorepo (Apache-2.0)", "https://github.com/Helicone/helicone"
            ),
        ),
        faq_items=(
            (
                "Is Helicone still safe to build on after the Mintlify acquisition?",
                """It is running, and running fine for existing users: Helicone says services stay live in maintenance mode, with security updates, new model support, and bug fixes continuing (the monorepo was still receiving commits in July 2026). Feature development has stopped, the changelog's last entry is November 26, 2025, and Mintlify has offered to help customers migrate to another platform. For an existing workload that is a manageable short-term risk. For new infrastructure it is hard to justify when the vendor's own acquirer is pointing at the exit.""",
            ),
            (
                "Does TrustedRouter replace Helicone's observability?",
                """No. Helicone's request-level tooling (full-body logs, session traces, per-user analytics, HQL, prompt management, datasets, evals) is better than anything we offer, and that is by design: TrustedRouter's realtime path keeps no durable prompt or output content, so our analytics are metadata only: tokens, cost, latency, models, providers, status. If you need full-body logging, run a logging tool on your side of the connection, or use our opt-in Broadcast feature to send selected content to a destination your workspace controls.""",
            ),
            (
                ("Both products let me keep prompts out of logs. What is actually different?"),
                """The default and the guarantee. Helicone logs bodies by default; its Helicone-Omit-Request and Helicone-Omit-Response headers stop storage, but Helicone's own docs note the omitted content is still sent to their backend, and keeping payloads out entirely requires async logging, which removes the gateway from the request path. On TrustedRouter, realtime inference does not durably log content in the first place, and you can verify the exact gateway code handling your prompts through live attestation endpoints on GCP, AWS, and Azure (trust.trustedrouter.com). The boundary: our attestation covers the gateway build, and downstream model providers are covered by cited retention policy tiers, except the trustedrouter/e2e routes to providers running their own confidential compute with end-to-end encryption.""",
            ),
        ),
        article_html="""<h2>What Helicone is in August 2026</h2>
<p>Helicone is an LLM observability platform with a gateway attached. The core is real open source: the main monorepo is <a href="https://github.com/Helicone/helicone">Apache-2.0 with about 6,000 GitHub stars</a>, and you can run it self-hosted or use the hosted US and EU regions. At acquisition the team reported 16,000 organizations and 14.2 trillion tokens processed over three years. That is proven scale, and the one-line integration that made it popular still works.</p>
<p>The acquisition is where any 2026 evaluation has to start. On March 3, 2026, <a href="https://www.mintlify.com/blog/mintlify-acquires-helicone">Mintlify acquired Helicone</a>. Helicone's <a href="https://www.helicone.ai/blog/joining-mintlify">own announcement</a> says services stay live "in maintenance mode": security updates, new model support, and bug fixes continue, and Mintlify offers to help customers migrate to another platform. Maintenance mode means feature development has stopped. The public record matches. The changelog's last entry is November 26, 2025, and the standalone Rust ai-gateway repo has been idle since November 21, 2025 — its final commit relicensed it from Apache-2.0 to GPL-3.0.</p>
<h2>Where Helicone is genuinely strong</h2>
<p>Request-level observability is Helicone's home turf, and it is better at it than we are. One integration line gets you full request and response bodies, session traces, per-user analytics, cost breakdowns, an SQL-like query language (HQL), prompt management, datasets, evals, and a playground. TrustedRouter keeps metadata-only analytics by design; we do not offer a comparable debugging surface.</p>
<p>The <a href="https://www.helicone.ai/pricing">pricing</a> is transparent and generous: a free tier of 10,000 requests per month, Pro at $79/month with unlimited seats, usage-based overage pricing, and startup and student discounts. The cloud gateway's <a href="https://www.helicone.ai/credits">pass-through billing credits are priced at 0% markup</a> with only payment processing fees, though as of August 2026 the credits page still gates access behind a waitlist. Because the platform is Apache-2.0, self-hosting is a genuine escape hatch, with on-prem deployment on the Enterprise plan.</p>
<h2>Three differences that decide it</h2>
<h3>Logging by default, or no durable logs</h3>
<p>Helicone's premise is capturing prompts and completions; bodies are logged by default, since that is what the dashboards, evals, and HQL run on. Per-request opt-outs exist (<span class="mono">Helicone-Omit-Request</span> and <span class="mono">Helicone-Omit-Response</span>), and they do stop storage, but Helicone's <a href="https://docs.helicone.ai/features/advanced-usage/omit-logs">own docs</a> note the content is still sent to their backend. Keeping payloads out entirely requires async logging with content tracing off, which takes the gateway out of the request path. Retention is plan-gated: 7 days free, 1 month at $79, 3 months at $799.</p>
<p>TrustedRouter inverts the default. Ordinary synchronous and streaming prompt paths do not touch persistent storage: no durable prompt or output logs. We retain operational metadata — request ids, model and provider, token counts, latency, cost, region, API-key hash. Batch is a separate opt-in mode with enclave-encrypted retention up to 30 days. Exact wording is on our <a href="/privacy">privacy page</a>.</p>
<h3>How you verify the privacy claim</h3>
<p>Helicone's trust model is source plus policy: Apache-2.0 code you can self-host, and documented SOC 2 compliance, with "SOC-2 &amp; HIPAA compliance" sold as a feature of the $799/month Team plan and above. TrustedRouter's is different in kind: <span class="mono">api.trustedrouter.com</span> runs inside trusted execution environments on GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers, and live attestation endpoints at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> bind the running gateway to published source and release digests. The boundary, stated plainly: attestation covers our gateway build. Downstream model providers are covered by cited, hand-audited retention policy tiers, except the <span class="mono">trustedrouter/e2e</span> routes, which only reach providers running their own confidential compute with end-to-end encryption. See <a href="/security">/security</a>.</p>
<h3>Fees on different bases</h3>
<p>Do not compare the fee numbers directly; the bases differ. Helicone charges for the platform: plan fees from $0 to $799/month plus usage-based per-request overage, while its gateway credits are priced at 0% markup on inference itself (waitlist-gated as of August 2026). TrustedRouter charges no plan or seat fee; prepaid text and embeddings bill the provider's token price plus 5.5%, with a $0.01 per million token floor. BYOK is supported, and we publish no separate BYOK fee. Per-route prices are listed on <a href="/models">/models</a> and full terms on <a href="/pricing">/pricing</a>.</p>
<h2>What we do not claim</h2>
<ul>
<li>Our gateway and control plane are source-available under BUSL-1.1, converting to Apache-2.0 four years after each release. Helicone's monorepo is OSI open source today. Every line that touches your prompt is public; our SDKs are Apache-2.0 or MIT.</li>
<li>We publish no SOC 2 or HIPAA certification as of August 2026. Helicone documents SOC 2 compliance.</li>
<li>Our repos have been public since late April 2026. Helicone shipped for three years and processed 14.2 trillion tokens before entering maintenance mode. Our track record is short; our <a href="/status">status page</a> and monthly <a href="/benchmarks/reports">benchmark reports</a> are where it accrues.</li>
<li>We do not replace request-level observability. If full-body logs, sessions, and evals are the job, we are the wrong tool on our own.</li>
</ul>""",
    ),
    _comparison(
        slug="requesty",
        name="Requesty",
        category="Hosted AI gateway",
        summary=(
            "Requesty is a hosted, closed-source gateway from a London seed-stage team: a "
            "flat 5% markup, spend budgets down to team, user, and key level, and EU "
            "residency on every plan. TrustedRouter differs on the prompt path: realtime "
            "inference keeps no durable prompt logs, on a source-available gateway whose "
            "running build is hardware-attested on three clouds."
        ),
        competitor_fit=(
            "Choose Requesty when team- and user-level spend budgets with webhook alerts, "
            "EU (Frankfurt) data residency on every plan, a flat 5% markup, or its free "
            "tier of 200 requests per day decide the purchase."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when the gateway itself must be verifiable: realtime "
            "inference keeps prompt and output bodies out of durable logs, the prompt "
            "path is source-available, and the running build serves live hardware "
            "attestations on GCP, AWS, and Azure that you can check rather than policy "
            "documents you must trust."
        ),
        migration="""Both gateways are OpenAI-compatible, so the wire change is small: swap the base URL (router.requesty.ai/v1 or router.eu.requesty.ai/v1) and the API key; Anthropic-SDK users swap the Messages base URL the same way. Model slugs need remapping to TrustedRouter ids. What does not carry over: Requesty routing policies, per-key spend caps and alerts, guardrail and PII rules, BYOK provider keys, and dashboards built on its analytics. Recreate routing with trustedrouter/auto, zdr, eu, or explicit provider preferences, and per-key spend limits on the new API keys. We publish no Requesty-specific migration doc; /docs/migrate-from-openrouter covers the same OpenAI-compatible mechanics.""",
        deployment="Managed SaaS only; EU (Frankfurt) every plan; US/APAC residency on enterprise",
        api="OpenAI-compatible v1 plus Anthropic-style base URL; separate EU endpoint",
        catalog="665 models, ~31 provider prefixes on live /v1/models (2026-08-16)",
        routing="Cost/latency/availability strategies, failover, load balancing, caching",
        observability="Real-time cost and latency dashboards, per-key tracking, alerts, audit logs",
        content="Logging on by default, 30-day EU retention; org-wide ZDR by written request",
        verification="Closed source; SOC 2 Type II and ISO 27001 in progress as of Aug 2026",
        billing="5% markup on model cost; free tier 200 req/day; BYOK for 4 providers",
        sources=(
            ComparisonSource(
                "Requesty pricing (5% markup, free tier)", "https://www.requesty.ai/pricing"
            ),
            ComparisonSource(
                "Requesty security page (logging FAQ, SOC 2 status)",
                "https://www.requesty.ai/security",
            ),
            ComparisonSource(
                "Requesty enterprise (RBAC, residency, ISO 27001 status)",
                "https://www.requesty.ai/enterprise",
            ),
            ComparisonSource(
                "Requesty privacy policy (retention, training scope)",
                "https://www.requesty.ai/privacy",
            ),
            ComparisonSource(
                "Requesty BYOK docs (four supported providers)",
                "https://docs.requesty.ai/features/bring-your-own-keys.md",
            ),
            ComparisonSource(
                "Requesty spend limits and budgets docs",
                "https://docs.requesty.ai/features/api-limits.md",
            ),
            ComparisonSource(
                "Requesty live model list (665 models)", "https://router.requesty.ai/v1/models"
            ),
            ComparisonSource(
                "Requesty $3M seed announcement (Sep 2025)",
                "https://www.requesty.ai/blog/requesty-raises-3m",
            ),
        ),
        faq_items=(
            (
                "Is Requesty cheaper than TrustedRouter?",
                (
                    "On the routing fee, yes. Both markups apply to the provider's token "
                    "price, so they compare directly: Requesty adds 5%, we add 5.5% with "
                    "a $0.01 per million token floor, and video on TrustedRouter carries "
                    "a 20% markup. Requesty also has a free tier of 200 requests per day "
                    "on free models, which we do not match. Exact per-route prompt and "
                    "completion prices are published on our /models page, so you can "
                    "compare a specific model before moving traffic."
                ),
            ),
            (
                "Both offer zero data retention. What actually differs?",
                """Defaults and evidence. On Requesty's self-serve plans, prompt and output logging is enabled by default with up to 30 days of encrypted EU retention; disabling it per key is self-serve, but organisation-wide ZDR requires a written request, and free-plan content may be used for Requesty's own training. On TrustedRouter, realtime inference never durably stores prompt or output bodies, and the gateway build enforcing that is hardware-attested on three clouds. Downstream provider retention is a policy question for both products, except on our trustedrouter/e2e routes, which are restricted to confidential-compute providers.""",
            ),
            (
                "Which is further along for an enterprise compliance review?",
                (
                    "Neither can hand you a SOC 2 report today. Requesty's SOC 2 Type II "
                    "is expected Q3 2026 and ISO 27001 is in progress, and its enterprise "
                    "plan offers SSO, four-level RBAC, guardrails with PII detection, and "
                    "audit logs, which is a broader governance surface than ours. We "
                    "publish a DPA, BAA, and subprocessor list, plus gateway attestation "
                    "evidence a technical reviewer can verify directly, but no published "
                    "certification either."
                ),
            ),
        ),
        article_html="""<h2>What Requesty is</h2>
<p>Requesty is a hosted LLM gateway run by Requesty Ltd, a London startup founded by Thibault Jaigu and Daniel Trugman. It raised a $3M seed led by 20VC in September 2025 and has shipped steadily since: an MCP gateway on the pay-as-you-go plan, coding-agent integrations, and open gateway-benchmark datasets published under CC BY 4.0 in April 2026. The product is an OpenAI-compatible endpoint at <span class="mono">router.requesty.ai/v1</span>, an Anthropic-style base URL, and a separate EU endpoint in Frankfurt (<span class="mono">router.eu.requesty.ai/v1</span>). Its public model list returned 665 models across roughly 31 provider prefixes when we fetched it on August 16, 2026. The homepage reports 70,000+ developers and 90+ billion tokens processed daily; those figures are self-reported. There is no self-hosted option, and the gateway source is not public &mdash; the <a href="https://github.com/requestyai">GitHub org</a> contains SDKs and CLI tooling under Apache-2.0 and MIT.</p>

<h2>Where Requesty is the better choice</h2>
<ul>
<li><strong>Spend controls.</strong> Hard per-key monthly caps with automatic cutoff, budgets per team, user, and key, alerts by email or webhook, and a <a href="https://docs.requesty.ai/features/api-limits.md">management API</a> to adjust limits programmatically. Our spend limits are per API key — daily, weekly, monthly, and lifetime caps with automatic cutoff and email alerts — so Requesty's edge here is team- and user-level budgets and webhook alerts, not the caps themselves.</li>
<li><strong>Simple, slightly cheaper pricing.</strong> A flat 5% markup on the provider's model cost, with a worked example on the <a href="https://www.requesty.ai/pricing">pricing page</a>: a model costing $10 per million tokens costs $10.50 through Requesty. No seat fees or minimums, a free tier of 200 requests per day on free models, and $10 of sign-up credits.</li>
<li><strong>EU residency on every plan.</strong> The Frankfurt endpoint is included at every tier, with UK and EU GDPR positioning and a DPA on request; the <a href="https://www.requesty.ai/enterprise">enterprise plan</a> adds US (Virginia) and APAC (Singapore) residency.</li>
<li><strong>An enterprise governance checklist.</strong> SSO (SAML/OIDC), four-level RBAC, audit logs, guardrails with PII detection, approved-model allowlists, and a 99.99% SLA claim on enterprise. We do not publish a comparable checklist today.</li>
</ul>

<h2>The logging default is the main divide</h2>
<p>Requesty's own security FAQ states that "prompt and output logging is enabled by default" on self-serve plans; content is retained encrypted in the EU for up to 30 days. Disabling logging per API key is self-serve, but organisation-wide zero data retention is enabled on written request rather than from the dashboard. On free-plan accounts, Requesty's privacy policy permits using content for its own training. The <a href="https://www.requesty.ai/security">security page</a> hero says content is never stored; the FAQ on the same page is the accurate statement.</p>
<p>TrustedRouter's realtime path has no equivalent default to turn off: ordinary synchronous and streaming inference does not write prompt or output bodies to persistent storage (<a href="/privacy">privacy</a>, <a href="/security">security</a>). What we keep is operational metadata: request ids, selected model and provider, token counts, latency, cost, region, and an API-key hash. Batch is a separate opt-in mode that retains enclave-encrypted artifacts for up to 30 days. Privacy is also routable: <span class="mono">trustedrouter/zdr</span> restricts to providers with a cited contractual zero-retention posture, <span class="mono">trustedrouter/e2e</span> restricts to confidential-compute providers with provider-side end-to-end encryption (currently Tinfoil and Phala), and <span class="mono">trustedrouter/eu</span> forces an EU-focused provider order. The pools are listed on <a href="/models">/models</a>.</p>

<h2>Trust by policy, trust by measurement</h2>
<p>Requesty asks you to trust documents. Those documents are in reasonable shape for a seed-stage company &mdash; GDPR posture, quarterly penetration testing, encryption in transit and at rest &mdash; but SOC 2 Type II and ISO 27001 are both in progress, with Type II expected Q3 2026 per its own security page, and there is no source code to audit.</p>
<p>Our answer is different in kind, and it has its own limits. Every line that touches your prompt is public: the gateway and control plane are source-available under BUSL-1.1 (converting to Apache-2.0 four years after each release), and the official SDKs are Apache-2.0 or MIT. The running build is verifiable: <span class="mono">api.trustedrouter.com</span> serves live hardware attestations from GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers, and a published verifier script checks the serving code against release digests (<a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>). The limit: attestation covers our gateway, not downstream model providers. Once a prompt leaves for OpenAI or Anthropic, retention there is a policy question for both products, except on our E2E routes, where the provider's own enclave and encryption mechanisms apply.</p>

<h2>Fees on the same basis</h2>
<p>Both markups apply to the provider's token price, so the numbers compare directly: Requesty adds 5%; we add 5.5% with a $0.01 per million token floor (<a href="/pricing">/pricing</a>). On the markup alone, Requesty is cheaper. Details that cut both ways: our floor means free-model routes are never billed below $0.01/M, video on TrustedRouter is the provider quote plus 20%, and Requesty's 200-requests-per-day free tier has no TrustedRouter equivalent. On BYOK, Requesty supports four providers (OpenAI, Anthropic, Google AI Studio, and xAI; Vertex is not yet supported) and its docs do not state a BYOK fee; we support BYOK across providers, and our pricing page also publishes no separate BYOK fee number.</p>

<h2>What we do not claim</h2>
<ul>
<li>We are young. Our public repos date from late April 2026 and our public benchmark history starts in June 2026. Requesty's seed closed in September 2025; neither company has a long operating track record.</li>
<li>We hold no published SOC 2 or HIPAA certification. We publish a DPA, BAA, and subprocessor list, and attestation evidence a reviewer can check directly, but on certifications neither product can hand you a current SOC 2 report today.</li>
<li>Their live catalog is larger: 665 models to our 561 catalog entries (a count that includes meta routes such as <span class="mono">auto</span> and <span class="mono">zdr</span>), as of August 16, 2026. We reference more upstream providers (about 49 to their 31).</li>
<li>We do not match Requesty's team- and user-level budgets, its webhook spend alerts, or its enterprise governance checklist of SSO, RBAC tiers, and PII guardrails.</li>
</ul>""",
    ),
    _comparison(
        slug="aws-bedrock",
        name="Amazon Bedrock",
        category="Cloud model platform",
        summary=(
            "Amazon Bedrock is AWS's managed model platform: ~120 serverless models "
            "across 18 providers, strong default privacy, and no gateway markup on your "
            "AWS bill. TrustedRouter is cloud-neutral, with a hardware-attested gateway "
            "and per-request privacy-tier routing across ~49 providers."
        ),
        competitor_fit=(
            "Choose Bedrock when your workloads, identity, and compliance evidence "
            "already live on AWS: IAM/SCP governance, PrivateLink, HIPAA eligibility "
            "(excluding the Fable and Mythos models), FedRAMP, spend that counts toward "
            "AWS commitments, and both Anthropic and OpenAI frontier models with no "
            "gateway markup."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you need one key across roughly 49 providers on "
            "any cloud, a gateway whose no-durable-prompt-logs behavior is checkable "
            "through live TEE attestation instead of policy alone, and per-request "
            "privacy routing (zdr, e2e, eu) that travels with the call."
        ),
        migration=(
            "Apps on Bedrock's OpenAI-compatible endpoints move with a base URL, key, and "
            "model-id change. Converse or InvokeModel apps need a request-shape rewrite "
            "first. IAM/SCP enforcement, Guardrails, Knowledge Bases, PrivateLink, "
            "CloudWatch wiring, and AWS-commitment spend do not carry over; Responses "
            "state stored with store=true stays in the AWS serving region."
        ),
        deployment=(
            "AWS regional managed service; IAM/SCP governance, PrivateLink, KMS, FIPS endpoints"
        ),
        api=(
            "Converse/InvokeModel plus OpenAI-compatible and Anthropic APIs, split across "
            "2 endpoints"
        ),
        catalog=(
            "~120 serverless models across 18 providers incl. GPT-5.x and Claude; 100+ Marketplace"
        ),
        routing=(
            "Cross-region inference profiles (global ≈10% cheaper) and intelligent prompt routing"
        ),
        observability=(
            "CloudWatch/CloudTrail; prompt logging opt-in, default off, absent on mantle endpoint"
        ),
        content=(
            "No prompt storage by default; GPT-5.x flagged traffic and Fable/Mythos retained ≤30d"
        ),
        verification=(
            "IAM, audit logs, SOC 1/2/3, FedRAMP, HIPAA-eligible; policy trust, no "
            "runtime attestation"
        ),
        billing="AWS metering at listed model prices, no markup; batch/flex −50%, priority +75%",
        sources=(
            ComparisonSource(
                "Bedrock data retention modes",
                "https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html",
            ),
            ComparisonSource(
                "Bedrock abuse detection and default no-storage",
                "https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html",
            ),
            ComparisonSource(
                "Bedrock runtime vs mantle endpoint comparison",
                "https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html",
            ),
            ComparisonSource(
                "Bedrock models at a glance",
                "https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html",
            ),
            ComparisonSource("Amazon Bedrock pricing", "https://aws.amazon.com/bedrock/pricing/"),
            ComparisonSource(
                "AWS HIPAA eligible services list",
                "https://aws.amazon.com/compliance/hipaa-eligible-services-reference/",
            ),
            ComparisonSource(
                "GPT-5.6 Luna/Terra price reduction (AWS What's New)",
                "https://aws.amazon.com/about-aws/whats-new/2026/07/openai-gpt-terra-luna-pricing-bedrock/",
            ),
            ComparisonSource(
                "Claude Fable 5 on AWS launch blog",
                "https://aws.amazon.com/blogs/aws/anthropic-claude-fable-5-on-aws-mythos-class-capabilities-with-built-in-safeguards-now-available/",
            ),
        ),
        faq_items=(
            (
                ("Bedrock already does not store prompts by default. What does attestation add?"),
                """Bedrock's default posture is excellent and we say so: no input/output storage by default, zero operator access, and model providers architecturally cut off from prompts. That posture is enforced by AWS policy and verified by auditors. TrustedRouter's no-durable-prompt-logs claim is verified differently: the gateway runs in enclaves on three clouds, and anyone can check the running build against published source from trust.trustedrouter.com without waiting on an audit cycle. The attestation covers our gateway only; it does not extend to downstream model providers.""",
            ),
            (
                "Which catalog is bigger, and what is each one missing?",
                (
                    "Bedrock lists ~120 serverless models across 18 providers plus 100+ "
                    "Marketplace models, and it is the only non-first-party host of both "
                    "Anthropic and OpenAI frontier models; it carries no Google Gemini, "
                    "only open-weight Gemma. TrustedRouter lists 550+ model routes across "
                    "~49 providers (as of August 2026), including Google routes. Coverage "
                    "gaps exist on both sides, so check /models for the specific SKUs you "
                    "need before committing."
                ),
            ),
            (
                "What breaks when we move off Bedrock?",
                (
                    "OpenAI-compatible call sites just re-point; Converse and InvokeModel "
                    "call sites need rewriting to OpenAI-compatible shapes. You lose "
                    "IAM/SCP enforcement, Guardrails, Knowledge Bases and Agents, "
                    "PrivateLink, CloudWatch/CloudTrail wiring, and spend that counted "
                    "toward AWS commitments, so plan replacements before cutover. "
                    "Responses state stored with store=true stays in the AWS serving "
                    "region; export it first. Our only step-by-step migration doc today "
                    "covers OpenRouter, not Bedrock."
                ),
            ),
        ),
        article_html="""<h2>What Amazon Bedrock actually is</h2>
<p>Amazon Bedrock is AWS's fully managed foundation-model service. AWS copies each provider's model into AWS-controlled deployment accounts — one per provider per region — and the providers have no access to those accounts, so they cannot see customer prompts or completions. As of August 2026 the serverless catalog is roughly 120 models across 18 providers, including Anthropic's Claude line through Fable 5 and OpenAI's proprietary GPT-5.x family (GA June and July 2026), with 100+ more models available through the Bedrock Marketplace. Bedrock is currently the only place other than the first parties where Anthropic and OpenAI frontier models sit behind one billing relationship.</p>
<p>There are two inference endpoints, and the split matters when you write code. <span class="mono">bedrock-runtime</span> carries the AWS-native Converse and InvokeModel APIs, OpenAI-compatible Chat Completions and Responses, Anthropic Messages, Guardrails, and cross-region inference profiles. <span class="mono">bedrock-mantle</span> carries the OpenAI-compatible and Anthropic APIs with server-side tools, background inference, and Projects, and lacks Guardrails, cross-region profiles, and model invocation logging. Authentication is IAM SigV4 or plain API keys that work directly with the OpenAI SDK.</p>

<h2>Where Bedrock is the right choice</h2>
<p>Bedrock's default privacy posture is exceptionally strong for a hosted model platform. Model inputs and outputs are not stored by default, no AWS operator can read them, customer content is not used to train base models, and the 2026 <a href="https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html">Data Retention API</a> adds a <span class="mono">none</span> mode, enforceable org-wide through SCPs, in which any model that requires retention becomes unavailable rather than retaining. Compliance depth is real: HIPAA-eligible (excluding the Fable and Mythos models), FedRAMP Class C and Class D including GovCloud, SOC 1/2/3, FIPS-validated endpoints, and PrivateLink.</p>
<p>Pricing is a genuine strength too. There is no gateway markup: you pay AWS's <a href="https://aws.amazon.com/bedrock/pricing/">listed per-token price</a>, metered on your AWS bill, counting toward committed spend. AWS passed OpenAI's July 30, 2026 cuts through — GPT-5.6 Luna dropped 80% to $0.20 input / $1.20 output per million tokens — and batch and flex tiers each take 50% off, with global cross-region routing about 10% cheaper than geographic. If your infrastructure, identity, and compliance program already live on AWS, Bedrock is a strong default and this page will not talk you out of it.</p>

<h2>Three differences that decide it</h2>
<p><strong>Verification kind.</strong> Bedrock's no-storage posture is policy backed by audits: AWS documentation, SOC reports, contract terms. TrustedRouter makes the equivalent claim mechanically checkable: <span class="mono">api.trustedrouter.com</span> runs inside hardware enclaves on three clouds (GCP Confidential Space, AWS Nitro Enclaves, Azure Confidential Containers), and anyone can verify that the running build matches the published source and release digests from <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>. Realtime inference does not write prompt or output content to durable storage, and every line of gateway code that touches your prompt is public.</p>
<p><strong>Carve-outs versus routes.</strong> Bedrock's zero-retention default now has flagship exceptions: classifier-flagged GPT-5.x traffic is retained up to 30 days, and Claude Fable 5 and Mythos 5 require retention with data sharing to Anthropic and potential human review — AWS's launch blog says plainly that once you opt in to data retention, "your data will leave AWS's data and security boundary." Bedrock manages this with account-level retention modes, and eligible customers can request full zero-data-retention for retention-requiring models through their AWS account team. TrustedRouter manages it with routing: <span class="mono">trustedrouter/zdr</span> restricts a request to providers with a cited zero-data-retention posture, <span class="mono">trustedrouter/e2e</span> restricts to confidential-compute providers with end-to-end encryption (tinfoil, phala), and <span class="mono">trustedrouter/eu</span> pins the EU provider set. Privacy floors compose with any request, so the constraint travels with the call rather than the account.</p>
<p><strong>Cloud neutrality, with the fee stated.</strong> Bedrock is one cloud: IAM identity, per-region model availability, and no Google Gemini (only open-weight Gemma). TrustedRouter routes 550+ model routes across roughly 49 providers (as of August 2026) with one key, including Google routes. The pricing bases differ, so here are both: Bedrock adds no fee over its listed model prices; TrustedRouter prepaid bills the provider's token price plus 5.5%, with a $0.01 per million token floor (<a href="/pricing">pricing</a>). Where Bedrock's listed price matches the provider's own — AWS matched OpenAI's first-party cuts, for example — Bedrock is cheaper by our 5.5% fee. Base prices are not identical everywhere: Bedrock lists legacy Claude 3.5 Sonnet at a 2x extended-access surcharge, so compare the exact SKUs you run. BYOK is supported; we do not publish a separate BYOK fee.</p>

<h2>What we do not claim</h2>
<ul>
<li>Our attestation covers the gateway and stops there. Downstream model providers are covered by cited policy tiers, except the E2E routes (tinfoil, phala), where the provider's own confidential-compute and encryption mechanisms apply.</li>
<li>We publish no SOC 2 or HIPAA certification. If either is a hard requirement today, Bedrock wins this comparison outright.</li>
<li>Our repos are young: public since late April 2026, with benchmark history from June 2026. AWS's operating track record is measured in decades.</li>
<li>The gateway and control plane are source-available under BUSL-1.1, converting to Apache-2.0 four years after each release; the SDKs are Apache-2.0 or MIT. We do not call the platform open source.</li>
</ul>

<h2>Migration reality</h2>
<p>If your app already uses Bedrock's OpenAI-compatible Chat Completions or Responses APIs, moving is mechanical: change the base URL and API key, then remap Bedrock model ids (such as <span class="mono">anthropic.claude-...</span> or inference-profile ARNs) to catalog names on <a href="/models">/models</a>. Apps built on Converse or InvokeModel need a request-shape rewrite first; those are AWS-native shapes no gateway speaks.</p>
<p>What does not carry over: IAM and SCP enforcement, Guardrails, Knowledge Bases and Agents, PrivateLink, CloudWatch and CloudTrail wiring, and spend that counted toward AWS commitments all stay behind. Stateful Responses conversations created with <span class="mono">store=true</span> are pinned to the AWS region that served them, so export or reset that state before cutover. Start with one streamed request, compare output, latency, and billed usage, and read <a href="/security">/security</a> and the <a href="/status">status page</a> before routing anything sensitive.</p>""",
    ),
    _comparison(
        slug="azure-ai-foundry",
        name="Microsoft Azure AI Foundry",
        category="Cloud model platform",
        summary=(
            "Microsoft Foundry is Azure's first-party AI platform: 1,900+ models "
            "including both GPT and Claude frontier lines, a GA cross-vendor model "
            "router, and deep Azure governance. TrustedRouter is a cloud-neutral gateway "
            "across ~49 providers with no durable prompt logs on realtime inference and a "
            "hardware-attested prompt path you can verify."
        ),
        competitor_fit=(
            "Choose Microsoft Foundry when your platform is already Azure and its "
            "controls bind: Entra ID and Azure Policy governance, FedRAMP High or HIPAA "
            "BAA obligations, PTU reservations and committed spend, or the need for both "
            "OpenAI GPT and Anthropic Claude frontier models behind one Azure bill with a "
            "GA cross-vendor router."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want one API across roughly 49 providers "
            "rather than one cloud's catalog, realtime inference that keeps no durable "
            "prompt or output logs by default rather than by approval-gated exemption, "
            "privacy and region routes (zdr, e2e, eu), and a gateway whose running build "
            "you can verify by hardware attestation on GCP, AWS, and Azure."
        ),
        migration=(
            "OpenAI-shaped Chat Completions and Responses code moves with a base URL, "
            "key, and deployment-name-to-model-id swap; Claude workloads keep the "
            "Anthropic Messages shape, which TrustedRouter serves natively. Server-side "
            "state (Assistants threads, stored completions, vector stores), Azure "
            "content-filter annotations, and Azure-native tooling like Foundry Agent "
            "Service, PTU reservations, and Azure Policy do not carry over."
        ),
        deployment=(
            "Azure resource deployments: Global, Data Zone (US/EU), regional, PTU, and Batch types"
        ),
        api="Azure OpenAI Chat + Responses at /openai/v1/; Claude via Anthropic Messages API",
        catalog=(
            "1,900+ models incl. GPT and Claude frontier lines (Claude under Marketplace terms)"
        ),
        routing=(
            "GA trained router across OpenAI, Anthropic, xAI, DeepSeek, Meta; subsets + failover"
        ),
        observability="Azure Monitor metrics, cost analysis, Foundry tracing, evaluations, dashboards",
        content=(
            "Abuse monitoring on by default; flagged prompts stored for review, no documented cap"
        ),
        verification=(
            "Product Terms + Azure audits; ContentLogging:false flag verifiable after approval"
        ),
        billing="Azure meters + $0.14/1M-input router markup; Claude billed via Marketplace CCUs",
        sources=(
            ComparisonSource(
                "Data, privacy, and security for Foundry Models sold by Azure",
                "https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/data-privacy",
            ),
            ComparisonSource(
                "Modified abuse monitoring eligibility (Limited Access)",
                "https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/limited-access",
            ),
            ComparisonSource(
                "Foundry model router concepts (GA, cross-vendor pool)",
                "https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-router",
            ),
            ComparisonSource(
                "What is Microsoft Foundry (catalog size, naming history)",
                "https://learn.microsoft.com/en-us/azure/foundry/what-is-foundry",
            ),
            ComparisonSource(
                "Claude in Microsoft Foundry GA announcement (CCU billing)",
                "https://azure.microsoft.com/en-us/blog/claude-in-microsoft-foundry-is-now-generally-available/",
            ),
            ComparisonSource(
                "Azure Retail Prices API: Model Router meters",
                "https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices",
            ),
            ComparisonSource(
                "Azure services in FedRAMP audit scope",
                "https://learn.microsoft.com/en-us/azure/azure-government/compliance/azure-services-in-fedramp-auditscope",
            ),
            ComparisonSource(
                "Docs commit removing the 30-day retention statement (2026-05-18)",
                "https://github.com/MicrosoftDocs/azure-ai-docs/commit/768d08d61e",
            ),
        ),
        faq_items=(
            (
                "Can we turn off Azure's abuse-monitoring storage?",
                (
                    "Only through modified abuse monitoring, which Microsoft limits to "
                    "customers managed by a Microsoft account team or in eligible "
                    "programs; self-service customers cannot opt out. Once approved, the "
                    "off state is verifiable: the resource JSON shows ContentLogging: "
                    '"false", which is better verification of an opt-out than most '
                    "providers offer. On TrustedRouter, no durable prompt or output logs "
                    "on realtime inference is the default for every account, with no "
                    "approval step."
                ),
            ),
            (
                ("Foundry has both GPT and Claude plus a GA router. Why add another gateway?"),
                (
                    "If your needs stop at those vendors on Azure, you may not need one — "
                    "Foundry's dual-frontier catalog and cross-vendor router are real "
                    "advantages and we say so plainly. TrustedRouter routes across "
                    "roughly 49 providers as of August 2026, including "
                    "confidential-compute hosts like Tinfoil and Phala, adds enforced "
                    "privacy and region routes (trustedrouter/zdr, e2e, eu), and replaces "
                    "policy trust in the gateway with hardware attestation you can check "
                    "at trust.trustedrouter.com."
                ),
            ),
            (
                "How do the fees actually compare?",
                (
                    "They sit on different bases, so no single number answers it. Foundry "
                    "charges a $0.14 per million input token router markup (Global; "
                    "$0.154 Data Zone) on top of Azure model rates, with Data Zone about "
                    "10% higher, Priority Processing at 2x, and Batch at half price; "
                    "Claude bills separately in Marketplace CCUs. TrustedRouter charges "
                    "the provider's token price plus 5.5% with a $0.01/M floor, listed "
                    "per model on /models. Run your own model mix through both; neither "
                    "is uniformly cheaper."
                ),
            ),
        ),
        article_html="""<h2>What Microsoft Foundry actually is</h2>
<p>Microsoft Foundry is Microsoft's first-party AI platform on Azure, renamed twice in two years: Azure AI Studio (2023), Azure AI Foundry (November 2024), Microsoft Foundry (Ignite, November 18, 2025). One Azure resource combines a catalog of <a href="https://learn.microsoft.com/en-us/azure/foundry/what-is-foundry">more than 1,900 models</a>, the Foundry Agent Service, evaluations, tracing, and governance through Entra ID and Azure Policy. Deployments come in Global, Data Zone (US or EU processing boundary), regional, Provisioned (PTU), and Batch types.</p>
<p>Two commercial regimes share the platform. "Models sold by Azure," including the Azure OpenAI line, are hosted by Microsoft, and the <a href="https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/data-privacy">data-privacy documentation</a> commits that prompts and outputs are not available to OpenAI or other model providers. Claude models, generally available since <a href="https://azure.microsoft.com/en-us/blog/claude-in-microsoft-foundry-is-now-generally-available/">June 29, 2026</a>, are Azure Marketplace offerings under a separate regime: Anthropic acts as an independent data processor for prompts and outputs, and usage bills in Claude Consumption Units.</p>

<h2>Where Foundry is the right choice</h2>
<p>Foundry is the only cloud platform with both OpenAI GPT and Anthropic Claude frontier models behind one procurement surface, and its trained <a href="https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-router">model router</a> reached GA at Ignite 2025, routing across OpenAI, Anthropic, xAI, DeepSeek, and Meta models (27 in the current pool) with subsets and automatic failover. The compliance portfolio is deeper than anything we offer: Azure OpenAI sits in FedRAMP High audit scope, including Azure Government DoD impact levels, Microsoft signs a HIPAA BAA through the Product Terms, and Azure's ISO and SOC audit portfolio is among the largest of any cloud. Cost machinery: PTU reservations, Batch at 50% off, committed spend, one Azure bill. If your platform is already Azure and those constraints bind, Foundry is the sane default.</p>

<h2>The logging models, precisely</h2>
<p>This is the load-bearing difference. Azure-sold models run abuse monitoring by default: when automated review (which stores nothing) is inconclusive, flagged prompts and completions go to a per-geography store for human review by Microsoft employees. The historical cap was explicit — an archived 2024 legal page said Azure OpenAI "stores all prompts and generated content securely for up to thirty (30) days" — yet Microsoft removed that language, and the last 30-day mention left the docs in a <a href="https://github.com/MicrosoftDocs/azure-ai-docs/commit/768d08d61e">commit on May 18, 2026</a>. As of August 2026, no maximum retention window is documented for that store. The opt-out, <a href="https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/limited-access">modified abuse monitoring</a>, is "available only to customers and partners managed by a Microsoft account team" or eligible programs; self-service customers cannot turn storage off. One genuine credit: after approval the resource JSON exposes <span class="mono">"ContentLogging": "false"</span>, a machine-readable off switch most providers do not offer.</p>
<p>TrustedRouter's default is the inverse. Ordinary synchronous and streaming inference never logs prompt or output content. We retain metadata: request ids, model and provider, token counts, latency, cost, region, API-key hash. Batch is a separate opt-in mode with enclave-encrypted retention up to 30 days (<a href="/docs/batch">details</a>). There is no approval process because there is no storage to turn off.</p>

<h2>Trust by contract, trust by attestation</h2>
<p>Foundry's privacy commitments are contractual: the Product Terms, the DPA, and Azure's audit evidence. The router documentation states "It does not store your prompts," and you rely on Microsoft's word plus its auditors. TrustedRouter's gateway runs inside hardware enclaves on three clouds — GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers — with live attestation endpoints and a verifier script anyone can run from <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>. The attested build is bound to published source and release digests, and every line that touches your prompt is public: the gateway is source-available under BUSL-1.1, the SDKs Apache-2.0 or MIT.</p>
<p>We state the boundary plainly: attestation covers our gateway, never the downstream model providers. Providers are covered by cited policy and contract tiers — <span class="mono">trustedrouter/zdr</span> restricts routing to endpoints with contractual or policy zero-data-retention — while <span class="mono">trustedrouter/e2e</span> restricts routing to providers running confidential compute with provider-side end-to-end encryption, which are the providers' own mechanisms. Microsoft's structural commitment that Azure-hosted models keep prompts away from OpenAI entirely is documented in its data-privacy terms, and it is a property our attestation cannot give you.</p>

<h2>Fees on different bases</h2>
<p>Do not equate the numbers directly. Foundry's model router adds a markup of $0.14 per million input tokens on Global deployments ($0.154 Data Zone) on top of the underlying Azure model rates. As of August 2026, gpt-5.6-sol Global Standard is $5.00/M input and $30.00/M output; Data Zone runs about 10% higher, Priority Processing doubles it, Batch halves it, and Claude bills separately in CCUs. The <a href="https://azure.microsoft.com/en-us/pricing/details/azure-openai/">public pricing page</a> renders placeholders; the real numbers live in the Retail Prices API. TrustedRouter's prepaid text and embedding pricing is the provider's token price plus 5.5% with a $0.01/M floor, listed per model on <a href="/models">/models</a> and explained on <a href="/pricing">/pricing</a>; BYOK is supported, and our pricing page does not publish a separate BYOK fee. One is a flat per-input-token fee plus cloud list prices; the other is a percentage of provider price. Run your own model mix through both.</p>

<h2>What we do not claim</h2>
<p>Our repos are young: public since late April 2026, with benchmark history from June 2026. We publish a DPA and BAA but hold no published SOC 2 or HIPAA certification and no FedRAMP authorization — if those are hard requirements, Foundry wins today. Our catalog is 561 model entries (including our meta route ids) across roughly 49 providers as of August 16, 2026: far smaller in raw count than Foundry's 1,900+.</p>

<h2>Moving a workload</h2>
<p>OpenAI-shaped code moves by pointing the client at <span class="mono">api.trustedrouter.com</span>, swapping the key, and replacing Azure deployment names with catalog model ids; older code also drops the <span class="mono">api-version</span> query parameter. Claude-on-Foundry workloads already speak the Anthropic Messages shape, which we serve natively. What does not carry over: server-side state (Responses history, Assistants threads, stored completions, files and vector stores must be exported and rebuilt), Azure content-filter annotations in responses, and Azure-native machinery such as Foundry Agent Service tools, On Your Data, PTU reservations, and Azure Policy governance. Model-router deployments map to <span class="mono">trustedrouter/auto</span> plus provider preferences.</p>""",
    ),
    _comparison(
        slug="google-vertex-ai",
        name="Google Vertex AI",
        category="Cloud model platform",
        summary=(
            "Vertex AI — renamed the Gemini Enterprise Agent Platform in April 2026 — is "
            "Google Cloud's managed platform for Gemini, partner, and open models, with "
            "deep compliance and a documented but opt-out-based path to zero data "
            "retention. TrustedRouter is a cross-provider gateway whose default realtime "
            "path keeps no durable prompt logs and runs in a hardware-attested build you "
            "can verify."
        ),
        competitor_fit=(
            "Choose Vertex AI (Agent Platform) when committed Google Cloud spend, FedRAMP "
            "High or HIPAA scope, Provisioned Throughput on Gemini, grounding in Google "
            "Search or Maps, or on-prem deployment via Google Distributed Cloud decides "
            "the architecture — and a contractual zero-data-retention checklist meets "
            "your privacy bar."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when one API must fail over across Google, Anthropic, "
            "OpenAI, and roughly 46 other providers, when realtime prompts must never "
            "touch durable storage by default rather than after an opt-out checklist, and "
            "when you want to verify the gateway build by remote attestation instead of "
            "accepting policy."
        ),
        migration=(
            "Vertex's OpenAI-compatible endpoint covers Gemini and select self-deployed "
            "models only, and its documented auth uses one-hour Google OAuth tokens; "
            "moving means a static TrustedRouter key, one base URL, and model-id renames. "
            "Partner-model call sites (Claude on Vertex) use provider-native APIs and "
            "need request-shape rewrites either way. Grounding and Provisioned Throughput "
            "have no TrustedRouter equivalent; Vertex's stored context caches map only to "
            "pass-through provider prompt caching on our side."
        ),
        deployment=(
            "Managed Google Cloud service; Global + regional endpoints; on-prem via "
            "Distributed Cloud"
        ),
        api=(
            "Gen AI SDKs; OpenAI-compatible Chat Completions for Gemini + select self-deploys only"
        ),
        catalog=(
            "200+ curated models: Gemini 3.x, Imagen 4, Veo 3.1, Claude Opus 5, DeepSeek, "
            "open models"
        ),
        routing="Global endpoint, service tiers, Provisioned Throughput; no cross-vendor failover",
        observability=(
            "Cloud Logging/Monitoring, audit logs; optional BigQuery request logging (off "
            "by default)"
        ),
        content=(
            "No-training terms; 24h cache default; abuse logs up to 90d; ZDR via opt-out checklist"
        ),
        verification="IAM, FedRAMP High, HIPAA/BAA, CMEK; no attestation on managed Gemini inference",
        billing=(
            "Source token prices; three service tiers, promo expiry dates, GSU commits, spend CUDs"
        ),
        sources=(
            ComparisonSource(
                "Gemini data governance and zero-data-retention checklist",
                "https://docs.cloud.google.com/vertex-ai/generative-ai/docs/data-governance",
            ),
            ComparisonSource(
                "Vertex abuse-monitoring prompt logging (up to 90 days)",
                "https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/abuse-monitoring",
            ),
            ComparisonSource(
                "Vertex generative AI pricing",
                "https://cloud.google.com/vertex-ai/generative-ai/pricing",
            ),
            ComparisonSource(
                "OpenAI-compatible Chat Completions for Gemini",
                "https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/call-gemini-using-openai-library",
            ),
            ComparisonSource(
                "Vertex AI to Agent Platform name changes",
                "https://docs.cloud.google.com/gemini-enterprise-agent-platform/vertex-ai-name-changes",
            ),
            ComparisonSource(
                "Model Garden catalog (200+ models)", "https://cloud.google.com/model-garden"
            ),
            ComparisonSource(
                "Google confidential computing announcements, June 2026",
                "https://cloud.google.com/blog/products/identity-security/verifiable-trust-in-the-ai-era-whats-new-in-confidential-computing",
            ),
            ComparisonSource(
                "Google Cloud HIPAA-included products",
                "https://cloud.google.com/security/compliance/hipaa",
            ),
        ),
        faq_items=(
            (
                "Is Gemini cheaper on Vertex AI or through TrustedRouter?",
                """Vertex, by exactly our fee. TrustedRouter prepaid bills the provider's token price plus 5.5% with a $0.01/M floor, so Gemini 2.5 Pro input (up to 200K-token context) is $1.25/M direct on Vertex and about $1.32/M through us. Vertex's batch discount (~50%), context-cache storage pricing, and Provisioned Throughput commit pricing have no TrustedRouter equivalent — if you run only Gemini at scale, Vertex wins on price. BYOK is supported on our side; the pricing page does not publish a separate BYOK fee.""",
            ),
            (
                "Can Vertex AI actually do zero data retention?",
                """Yes, with work. Google documents the checklist: disable the default 24-hour in-memory cache per project, request an abuse-logging exception (Master Agreement customers are exempt by default), set store=false on the Interactions API, and avoid Search/Maps grounding retention. It may still be unavailable on some Advanced AI models — Claude Fable 5 and Mythos 5 on Vertex carry prompt and response retention of up to 30 days plus mandatory sharing with Anthropic. TrustedRouter's realtime default is no durable prompt or output logs, with only operational metadata retained.""",
            ),
            (
                "Does TrustedRouter's attestation cover the model answering my prompt?",
                (
                    "No. Attestation proves the gateway at api.trustedrouter.com runs the "
                    "published source-available build; downstream providers are covered "
                    "by cited retention-policy tiers, and only the trustedrouter/e2e "
                    "route restricts to providers running their own confidential compute "
                    "with end-to-end encryption (Tinfoil, Phala). Vertex's managed Gemini "
                    "endpoint documents no attestation option; Google's confidential-GPU "
                    "infrastructure applies when you self-deploy models on Confidential "
                    "VMs or GKE."
                ),
            ),
        ),
        article_html="""<h2>What Vertex AI is today</h2>
<p>Since April 22, 2026, Vertex AI has been the Gemini Enterprise Agent Platform: roughly 50 renames at Cloud Next 2026, docs re-homed to docs.cloud.google.com, APIs unchanged, no forced migration. We use the old name because most readers still do.</p>
<p>It is Google Cloud's proprietary managed platform for first-party models (Gemini 3.x, Imagen 4, Veo 3.1), partner models (Anthropic Claude, Mistral, DeepSeek, Qwen, and others), and open models, plus MLOps and agent tooling. <a href="https://cloud.google.com/model-garden">Model Garden</a> lists a curated set of 200+ models as of August 2026, and the cadence is fast: Gemini 3.7 Flash went GA August 13, 2026; Claude Opus 5 arrived July 24, 2026. Deployment spans the managed API (Global plus regional endpoints), self-deployed GPU endpoints, and on-prem installs via Google Distributed Cloud.</p>
<h2>Where it is genuinely strong</h2>
<ul>
<li><strong>Source pricing and capacity engineering on Google models.</strong> Gemini 2.5 Pro is <a href="https://cloud.google.com/vertex-ai/generative-ai/pricing">$1.25/M input and $10.00/M output</a> at up to 200K-token context ($2.50/$15.00 above), batch runs about 50% off, and cached input drops to $0.125/M. Provisioned Throughput sells committed capacity from $2.7397 per GSU-hour on a one-year Global commit. No gateway replicates those economics on Google's own models, including us.</li>
<li><strong>Compliance depth.</strong> FedRAMP High authorization for Generative AI on Vertex, HIPAA coverage with a BAA, VPC Service Controls, CMEK, data residency, and per-model IAM consent gating.</li>
<li><strong>A real contractual baseline.</strong> Google's Service Specific Terms training restriction: customer data is not used to train or fine-tune models without prior permission, GA and pre-GA alike.</li>
<li><strong>Confidential-GPU infrastructure for self-deployed models.</strong> Confidential Space with NVIDIA H100 GPUs went GA and Confidential G4 VMs with Blackwell GPUs entered preview in June 2026, plus open-source Prompt Encryption SDKs.</li>
</ul>
<h2>The retention model, line by line</h2>
<p>This is the load-bearing difference. The <a href="https://docs.cloud.google.com/vertex-ai/generative-ai/docs/data-governance">data-governance doc</a> describes a real, auditable path to zero data retention. It is a checklist, not a default:</p>
<ul>
<li>In-memory prompt caching is on by default for Gemini (24-hour TTL, project-isolated); disabling it takes a per-project <span class="mono">cacheConfig</span> change.</li>
<li>If safety classifiers flag activity, prompts can be logged up to 90 days under <a href="https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/abuse-monitoring">abuse monitoring</a>. Master Agreement customers are exempt by default; others must file an exception request.</li>
<li>Designated "Advanced AI" models log all prompts and responses for up to 30 days, and opt-out "may not be possible". For Claude Fable 5 and Mythos 5 on Vertex, prompts and responses are retained up to 30 days, and sharing with Anthropic is mandatory.</li>
<li>Grounding with Google Search stores prompt-derived queries up to 3 days and cannot be disabled; Maps grounding stores 30 days. The Interactions API defaults to <span class="mono">store=true</span>.</li>
</ul>
<p>TrustedRouter's realtime default needs no checklist: ordinary synchronous and streaming prompt paths do not touch persistent storage. We keep operational metadata — request ids, model and provider, token counts, latency, cost, region, API-key hash — and nothing of the prompt or output. Batch is a separate opt-in mode with enclave-encrypted retention up to 30 days. Details on <a href="/privacy">/privacy</a>.</p>
<h2>Verify vs trust</h2>
<p>Google documents no customer-verifiable TEE or attestation for the managed Gemini endpoint; the guarantee there is contractual. Its confidential-computing work applies when you self-deploy on Confidential VMs or GKE.</p>
<p>TrustedRouter's gateway runs inside TEEs on three clouds — GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers — with live attestation endpoints and a one-command verifier at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>. The gateway and control plane are source-available (BUSL-1.1, converting to Apache-2.0 four years after each release), so every line that touches your prompt is public, and the attestation binds the running build to published source and release digests.</p>
<p>The boundary: our attestation covers our gateway, not the model providers behind it. Downstream handling is tracked as cited policy tiers per provider — Google Vertex itself sits in the default candidate order of our <span class="mono">trustedrouter/zdr</span> route — and only <span class="mono">trustedrouter/e2e</span> restricts to providers (Tinfoil, Phala) running provider-side confidential compute with end-to-end encryption, where the provider's own enclave claims apply.</p>
<h2>Pricing, with the bases stated</h2>
<p>The two fee bases differ, so read carefully. Vertex bills the source token price, with real complexity: three service tiers, Global-versus-regional premiums, and promotional prices with expiry dates. Gemini 3.7/3.6 Flash's intro $0.75/$3.75 per 1M doubles January 1, 2027, and Claude Sonnet 5's $2.00/$10.00 promo ends August 31, 2026.</p>
<p>TrustedRouter prepaid bills the provider's token price plus 5.5%, with a $0.01/M floor; video is the provider quote plus 20%; there are no seat or subscription fees. So Gemini through us costs 5.5% more than direct Vertex — Gemini 2.5 Pro input at about $1.32/M instead of $1.25/M — and the fee buys cross-provider failover, privacy-tier routing, and the attested prompt path. BYOK is supported; our pricing page does not publish a separate BYOK fee. Exact per-route prices: <a href="/models">/models</a> and <a href="/pricing">/pricing</a>.</p>
<h2>What we do not claim</h2>
<ul>
<li>Our attestation stops at the gateway. We do not attest Google's, Anthropic's, or anyone else's model servers.</li>
<li>We are young. Our public repos date from late April 2026 and public benchmark history starts June 2026; Google's operating record is measured in decades.</li>
<li>We publish a DPA, BAA, and subprocessor list, but no SOC 2 or HIPAA certification today. If FedRAMP High or certified HIPAA scope is a hard requirement, Vertex clears bars we do not.</li>
<li>We have no equivalent of Search or Maps grounding, Provisioned Throughput commitments, context-cache storage pricing, or an on-prem install.</li>
</ul>
<h2>Migration reality</h2>
<p>Vertex exposes an <a href="https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/call-gemini-using-openai-library">OpenAI-compatible Chat Completions surface</a>, but only for Gemini and select self-deployed Model Garden models; partner models such as Claude use provider-native APIs there, so those call sites need request rewrites either way. Its documented OpenAI-client auth uses one-hour Google OAuth access tokens refreshed programmatically; moving to TrustedRouter means a static key and one base URL, and adds Anthropic-style Messages alongside Chat Completions and Responses.</p>
<p>Two cautions from Google's docs: unsupported parameters on the Vertex OpenAI surface are dropped without an error, so a passed setting may never have been active, and Gemini-specific features that ride in <span class="mono">extra_body</span> need re-testing on any gateway. We publish a step-by-step migration doc only for OpenRouter today; coming from Vertex, start at <a href="/docs">/docs</a> and the per-route prices on <a href="/models">/models</a>.</p>""",
    ),
    _comparison(
        slug="tinfoil",
        name="Tinfoil",
        category="Confidential AI inference",
        summary=(
            "Tinfoil runs open-source models inside hardware enclaves it operates and "
            "lets clients cryptographically verify the running code, GPUs, and model "
            "weights: attestation that reaches deeper than ours, across roughly a dozen "
            "models. TrustedRouter attests its gateway, routes across ~550 models on ~49 "
            "providers, and includes Tinfoil itself in its confidential-compute E2E pool."
        ),
        competitor_fit=(
            "Choose Tinfoil when open-source models cover the workload, or you can bring "
            "your own weights via Containers, and you want zero retention enforced by "
            "enclave hardware on every request, with SOC 2 Type II and verification down "
            "to the GPU and model weights."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you need closed models from Anthropic, OpenAI, and "
            "Google alongside open ones — roughly 550 routes across ~49 providers behind "
            "one attested gateway — with an opt-in ZDR routing floor (trustedrouter/zdr) "
            "and Tinfoil-class enclave routes still reachable via trustedrouter/e2e."
        ),
        migration=(
            "Both APIs are OpenAI-compatible: swap the base URL and key. Tinfoil's "
            "client-side enclave verification and EHBP body encryption do not carry over, "
            "and Tinfoil model ids map by hand to TrustedRouter catalog ids — or to "
            "trustedrouter/e2e to keep enclave-hosted execution on Tinfoil or Phala."
        ),
        deployment="Hosted enclaves it operates: AMD SEV-SNP VMs, NVIDIA Hopper/Blackwell CC GPUs",
        api="OpenAI-compatible API; SDKs verify enclave attestation; EHBP body encryption",
        catalog="6 chat models, ~12 total (Aug 2026); open-source only — no GPT-5 or Claude",
        routing="One attested router load-balancing its own model enclaves; no other providers",
        observability="Usage dashboards, teams, Admin API; no prompt or content logs by design",
        content="Zero retention enforced by enclave architecture, default on every request",
        verification=(
            "Client verifies router enclave; router transitively checks model enclaves, "
            "GPUs, weights"
        ),
        billing=(
            "Per-token (GPT-OSS 120B $0.15/$0.60 per 1M); Chat $20/mo; Containers $20/mo + usage"
        ),
        sources=(
            ComparisonSource(
                "Tinfoil pricing (Chat, API, Containers tabs)", "https://tinfoil.sh/pricing"
            ),
            ComparisonSource(
                "Tinfoil attestation architecture",
                "https://docs.tinfoil.sh/verification/attestation-architecture",
            ),
            ComparisonSource(
                "Verification in Tinfoil (client verifies router enclave)",
                "https://docs.tinfoil.sh/verification/verification-in-tinfoil",
            ),
            ComparisonSource(
                "Tinfoil security and privacy FAQ", "https://tinfoil.sh/security-and-privacy-faq"
            ),
            ComparisonSource("Tinfoil chat model catalog", "https://docs.tinfoil.sh/models/chat"),
            ComparisonSource(
                "Tinfoil changelog (model and API changes)",
                "https://docs.tinfoil.sh/resources/changelog",
            ),
            ComparisonSource("Tinfoil technology overview", "https://tinfoil.sh/technology"),
        ),
        faq_items=(
            (
                "Is Tinfoil's privacy guarantee stronger than TrustedRouter's?",
                """At the model runtime, yes. Tinfoil executes models inside SEV-SNP enclaves with confidential NVIDIA GPUs and enforces zero retention in hardware by default. TrustedRouter's attestation covers our gateway; downstream zero retention on most routes is a cited contractual or policy claim. Our trustedrouter/e2e route narrows routing to confidential-compute providers (currently Tinfoil and Phala), but those enclaves are the provider's mechanism, not ours. If open-source models fit and the whole path must be hardware-enforced, Tinfoil is the stronger guarantee.""",
            ),
            (
                "Can I run Claude or GPT-5 on Tinfoil?",
                (
                    "No. Tinfoil's homepage states that closed-source models like GPT-5 "
                    "and Claude cannot run there; as of August 2026 the API catalog is 6 "
                    "chat models, all open source. Tinfoil Containers does let you run "
                    "your own weights, including proprietary fine-tunes, in an enclave. "
                    "TrustedRouter routes to Anthropic, OpenAI, and Google under cited "
                    "policy ZDR tiers, which is the main reason to put us in front."
                ),
            ),
            (
                "Can I reach Tinfoil's enclaves through TrustedRouter?",
                (
                    "Yes. Tinfoil is one of our upstream providers, and trustedrouter/e2e "
                    "restricts routing to Tinfoil and Phala. You get our failover, one "
                    "API across every provider, and unified billing at provider price + "
                    "5.5% with a $0.01/M floor. You give up Tinfoil's client-side "
                    "verification: through our gateway you verify our attestation, and "
                    "the Tinfoil-side enclave posture is their published mechanism, "
                    "outside our attested boundary."
                ),
            ),
        ),
        article_html="""<h2>What Tinfoil actually is</h2>
<p>Tinfoil Inc. is a San Francisco company, founded in 2024, YC Spring 2025 batch, about five people per its YC profile. It runs open-source models inside hardware secure enclaves it operates: AMD SEV-SNP confidential VMs paired with NVIDIA Hopper and Blackwell GPUs in confidential-compute mode. Since July 2025 every inference request passes through a unified router at <span class="mono">inference.tinfoil.sh</span>, itself an attested enclave whose source is public under AGPL-3.0.</p>
<p>Three products. Private Chat is a consumer app at $20/month with a 3M-tokens/hour fair-use cap. The Private Inference API is OpenAI-compatible, billed per token. Tinfoil Containers (GA March 2026) runs any Docker image inside an enclave for $20/month plus usage, with H200 GPUs at $2,000/month and B200 at $5,000/month. As of August 2026 the API catalog is 6 chat models (Kimi K3, GLM-5.2, DeepSeek V4 Flash, Gemma 4 31B, GPT-OSS 120B, Llama 3.3 70B) plus vision, audio, embedding, and safety models, roughly a dozen models total. Open-source models only: Tinfoil's homepage says plainly that closed models like GPT-5 and Claude cannot run there. Containers is the escape hatch: bring your own weights, including proprietary fine-tunes.</p>

<h2>Where Tinfoil is genuinely strong</h2>
<p>Tinfoil verifies further down the stack than any gateway we compare against, ourselves included. Client SDKs check the router enclave's attestation against measurements published through GitHub and Sigstore; the router then verifies each downstream model enclave, including NVIDIA GPU attestation and model weights on dm-verity read-only volumes checked against Sigstore bundles. The chain proves which code and which weights served a request. Zero retention is architectural and the default on every request; per their security FAQ, prompts and completions live only inside enclaves and are never written to disk or logged. The enclave code is open source (AGPL-3.0), the SDKs Apache-2.0, and the company holds a SOC 2 Type II report covering security, availability, and confidentiality. Pricing is competitive despite confidential-compute overhead: GPT-OSS 120B costs $0.15 input / $0.60 output per million tokens, with prompt-caching discounts since July 2026.</p>
<p>A precision note our earlier page got wrong: the client directly verifies the router enclave only. Model-enclave, GPU, and weight checks happen transitively inside that router; Tinfoil's docs say client SDKs "only need to verify the first enclave in this chain." End-to-end trust therefore rests on the attested router doing the downstream checks. We had described it as client-side verification of everything. Corrected.</p>

<h2>How TrustedRouter differs</h2>
<p><strong>Depth versus breadth.</strong> Tinfoil attests the model runtime. TrustedRouter attests the gateway: <span class="mono">api.trustedrouter.com</span> runs a published workload inside a TEE, verifiable live on three clouds (GCP Confidential Space, AWS Nitro Enclaves, Azure Confidential Containers) at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>, with a runnable verifier script. Behind that gateway sit roughly 550 model routes across about 49 providers as of August 2026, including the closed models from Anthropic, OpenAI, and Google that Tinfoil cannot host. Downstream providers are covered by cited policy and contract tiers, not by our attestation. Tinfoil proves more about a narrow catalog; we prove the prompt path into a broad one.</p>
<p><strong>Tinfoil is in our catalog.</strong> Tinfoil is one of our upstream providers. The <span class="mono">trustedrouter/e2e</span> route (alias <span class="mono">confidential</span>) restricts routing to providers running confidential compute with provider-side end-to-end encryption; today that pool is Tinfoil and Phala. The <span class="mono">trustedrouter/zdr</span> route enforces a contractual zero-data-retention floor across a wider pool that also includes Anthropic, OpenAI, and Google. One policy can therefore prefer enclave-hosted routes and fall back to policy-ZDR closed models when the open catalog cannot handle a task. Two caveats: on those routes the enclave posture is Tinfoil's or Phala's mechanism, outside our attestation boundary, and through our gateway you do not run Tinfoil's client-side verification.</p>
<p><strong>Logging and pricing bases.</strong> Realtime inference keeps no durable prompt or output logs; we retain metadata (request ids, model, token counts, latency, cost, region, API-key hash). Batch is separate and opt-in, with enclave-encrypted retention up to 30 days (<a href="/privacy">privacy</a>). On price, the bases differ, so no single fee comparison is honest: TrustedRouter charges the upstream provider's token price plus 5.5% with a $0.01/M floor (<a href="/pricing">pricing</a>), while Tinfoil sets flat per-token prices for models it hosts itself. Compare final per-million prices on <a href="/models">/models</a> against <a href="https://tinfoil.sh/pricing">tinfoil.sh/pricing</a>.</p>

<h2>What we do not claim</h2>
<ul>
<li>Our attestation covers the gateway build, not model providers. On non-E2E routes, downstream zero retention is a contractual or policy claim with cited catalog flags, defaulting conservatively to assume-stored.</li>
<li>Our gateway and control plane are source-available under BUSL-1.1, each release converting to Apache-2.0 four years after publication; the SDKs are Apache-2.0 or MIT. Tinfoil's enclave stack is AGPL-3.0 open source, a real difference in their favor. In both products, the security-critical code that touches your prompt is public.</li>
<li>We publish no SOC 2 or HIPAA certification today. Tinfoil has SOC 2 Type II; HIPAA in progress per their security FAQ.</li>
<li>We are young: public repos since late April 2026, public benchmark reports since June 2026, short on-page uptime history (about 72 hours detailed, plus monthly rollups). Tinfoil was founded in 2024 with about five people. Vendor-risk reviewers should weigh early-stage risk on both sides of this page.</li>
</ul>

<h2>Migration reality</h2>
<p>Both APIs are OpenAI-compatible, so moving either direction is a base URL and key change. From Tinfoil to TrustedRouter, three things do not carry over: Tinfoil's client-side enclave verification (our SDKs verify our gateway attestation instead, a narrower check), the EHBP encrypted-body protocol, and Tinfoil model ids such as <span class="mono">kimi-k3</span>, which you map to catalog ids or to <span class="mono">trustedrouter/e2e</span> to keep enclave-hosted execution. Our only step-by-step migration doc today covers OpenRouter, so plan the id mapping by hand. In the other direction, Tinfoil has no closed models, so Claude or GPT traffic has nowhere to land there. Relevant to either plan: Tinfoil has shipped two breaking API changes since July 2025 (router unification, July 2025; mandatory EHBP, October 2025, SDKs v0.10.0+), and its catalog churns monthly; Kimi K2.6 and DeepSeek V4 Pro were removed in July 2026, with Kimi K3 and DeepSeek V4 Flash added in August. Budget for model-id maintenance either way, and see our <a href="/security">security page</a> for what our side lets you verify.</p>""",
    ),
    _comparison(
        slug="tensorzero",
        name="TensorZero",
        category="Self-hosted LLMOps stack",
        summary=(
            "TensorZero was an Apache-2.0 LLMOps stack — Rust gateway, observability, "
            "evaluations, and a real optimization loop — until the project was archived "
            "on June 12, 2026 and left unmaintained. TrustedRouter is a maintained, "
            "source-available managed gateway with attested execution and no durable "
            "prompt logs on realtime inference."
        ),
        competitor_fit=(
            "Choose TensorZero only if you are prepared to own a fork: the Apache-2.0 "
            "stack still runs, all data stays in your own database, and its optimization "
            "loop (SFT, DPO, GEPA, gateway-run A/B tests) has no drop-in replacement in "
            "any plain gateway, TrustedRouter included. The repository has been archived "
            "and read-only since June 12, 2026, so security patches and new provider "
            "support are on you."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter for a maintained gateway: one API over 550+ model "
            "routes across ~49 providers (as of 2026-08-16), realtime inference with no "
            "durable prompt or output logs, privacy-tier routing (zdr, e2e, eu), and a "
            "gateway build you can verify against live attestation on three clouds."
        ),
        migration=(
            "Apps on TensorZero's OpenAI-compatible endpoint swap the base URL and key, "
            "drop tensorzero:: extra-body parameters, and map function names to concrete "
            "model ids. Native /inference callers move prompt templates out of gateway "
            "TOML into application code. Episodes, feedback, and the SFT/DPO/GEPA "
            "optimization recipes have no TrustedRouter equivalent; historical data "
            "exports from your own ClickHouse or Postgres with standard SQL."
        ),
        deployment="Self-hosted Docker/Helm stack; repo archived and read-only since June 12, 2026",
        api=(
            "Native /inference (functions, variants, episodes) plus an OpenAI-compatible endpoint"
        ),
        catalog=(
            "~19 provider integrations on your keys; frozen June 2026 bar the generic OpenAI path"
        ),
        routing=(
            "Retries, fallbacks, caching, rate limits, adaptive A/B tests, best-of-N/mixture-of-N"
        ),
        observability=(
            "Inferences and feedback in your ClickHouse or Postgres; optional, can be disabled"
        ),
        content="Stays in your DB; pseudonymous usage analytics on by default until disabled",
        verification="Apache-2.0 source you audit, build, and run; no attestation layer",
        billing="Free (Apache-2.0); your infrastructure plus direct provider billing, no markup",
        sources=(
            ComparisonSource("TensorZero homepage wind-down notice", "https://www.tensorzero.com/"),
            ComparisonSource(
                "TensorZero GitHub repository (archived June 12, 2026)",
                "https://github.com/tensorzero/tensorzero",
            ),
            ComparisonSource(
                "TensorZero CEO wind-down statement on Hacker News",
                "https://news.ycombinator.com/item?id=48518120",
            ),
            ComparisonSource(
                "TensorZero gateway benchmarks (repo docs)",
                "https://raw.githubusercontent.com/tensorzero/tensorzero/main/docs/gateway/benchmarks.mdx",
            ),
            ComparisonSource(
                "TensorZero configuration reference: usage-analytics default",
                "https://raw.githubusercontent.com/tensorzero/tensorzero/main/docs/gateway/configuration-reference.mdx",
            ),
            ComparisonSource(
                "TrustedRouter pricing: 5.5% fee and $0.01/M floor",
                "https://trustedrouter.com/pricing",
            ),
            ComparisonSource(
                "TrustedRouter trust page: attestation endpoints and verifier",
                "https://trust.trustedrouter.com",
            ),
        ),
        faq_items=(
            (
                "Can we keep running TensorZero in production?",
                (
                    "Mechanically, yes. Apache-2.0 permits forking and self-hosting "
                    "indefinitely, and the final 2026.6.0 release (June 4, 2026) still "
                    "works. But the repository has been archived and read-only since June "
                    "12, 2026: no security patches, no new provider integrations, no "
                    "support. Budget for maintaining a fork, and set "
                    "disable_pseudonymous_usage_analytics to true — the default-on "
                    "analytics still tries to send usage data to the wound-down company's "
                    "endpoint."
                ),
            ),
            (
                ("Does TrustedRouter replace TensorZero's optimization and experimentation loop?"),
                (
                    "No. TensorZero's structured data model, feedback API, fine-tuning "
                    "and prompt-optimization recipes (SFT, DPO, GEPA, DICL), and "
                    "gateway-run adaptive A/B tests have no TrustedRouter equivalent. We "
                    "offer routing, failover, and small-scale evals (/docs/evals). Teams "
                    "that depend on that loop should plan to keep the archived stack for "
                    "it or rebuild the workflow on other tooling."
                ),
            ),
            (
                "What does each one cost?",
                (
                    "TensorZero is free software: you pay for your own infrastructure "
                    "(gateway, ClickHouse or Postgres, UI, optional Valkey) and bill "
                    "providers directly on your keys, with no markup. TrustedRouter "
                    "prepaid inference is the provider's token price plus 5.5% with a "
                    "$0.01 per million token floor; BYOK is supported but no separate "
                    "BYOK fee is published. The bases differ — self-run infrastructure "
                    "and ops time versus a managed fee on provider cost — so compare "
                    "against your own operations budget."
                ),
            ),
        ),
        article_html="""<h2>Start with the wind-down</h2>
<p>TensorZero's GitHub repository was <a href="https://github.com/tensorzero/tensorzero">archived by its owner on June 12, 2026</a> and is read-only. The homepage now carries a single notice: still available on GitHub, no longer maintained. Co-founder and CEO Gabriel Bianconi <a href="https://news.ycombinator.com/item?id=48518120">wrote on Hacker News</a> that the team wound the project down and is returning remaining capital to investors, after raising a $7.3M seed in 2024 and spending less than half of it. The final release, 2026.6.0, shipped on June 4, 2026 — eight days before the archive. The docs site is gone; its URLs now redirect to raw files inside the archived repository.</p>
<p>That changes what this page compares: a maintained managed gateway on one side, and an Apache-2.0 codebase that still runs but that nobody maintains on the other.</p>

<h2>What TensorZero got right</h2>
<p>TensorZero shipped a full self-hosted LLMOps stack: a Rust gateway with a unified API across roughly 19 documented provider integrations, observability that stored inferences and feedback in your own ClickHouse or Postgres, built-in evaluations, optimization recipes — supervised fine-tuning, DPO, GEPA prompt optimization, dynamic in-context learning, best-of-N and mixture-of-N — and gateway-run experimentation with adaptive A/B tests that hold variant assignment consistent within a multi-step episode.</p>
<p>The engineering was serious. The team published a benchmark showing 0.94&nbsp;ms p99 gateway overhead at 10,000 QPS on a single c7i.xlarge; the caveats (self-run, mock provider, observability disabled) are in their own notes. Adoption was real: 11,723 GitHub stars (checked August 2026), and a self-reported README claim of fueling about 1% of global LLM API spend. The structured data model was the genuine differentiator among open-source gateways: inferences plus downstream feedback in your database made dataset curation, fine-tuning, prompt optimization, and gateway-run A/B testing one continuous loop. No plain proxy — TrustedRouter included — replaces that loop.</p>

<h2>The cost of running archived software</h2>
<p>Apache-2.0 survives the archive: forking and self-hosting remain fully permitted. What you take on is everything upstream used to do — security patches, dependency CVEs, and provider-API drift, with the provider catalog frozen at June 2026. New providers and model APIs go through the generic OpenAI-compatible integration or your own fork. Two specifics deserve attention. Pseudonymous usage analytics is on by default (<span class="mono">disable_pseudonymous_usage_analytics</span> defaults to <span class="mono">false</span>) and still tries to send usage data to the wound-down company's endpoint, so set it to <span class="mono">true</span>. And Autopilot, the planned managed layer, never left private beta.</p>

<h2>Where TrustedRouter differs</h2>
<p><strong>Someone operates it.</strong> TrustedRouter is a hosted gateway: one base URL, OpenAI Chat Completions and Responses plus Anthropic-style Messages, and a maintained catalog of 550+ model routes across roughly 49 providers as of August 16, 2026 (<a href="/models">live list</a>). Provider onboarding, failover, and API drift are our problem, not a fork you carry.</p>
<p><strong>The trust model is proof, not possession.</strong> TensorZero's privacy answer is possession: prompts stay on your machines, in your database, and observability can be switched off entirely. That is a strong answer, and self-hosting is the right call when policy requires it. TrustedRouter is hosted, so we owe evidence instead: the gateway runs inside TEEs on GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers, with live attestation endpoints and a verifier script published at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> that check the running build against published source digests. Realtime inference keeps no durable prompt or output logs; we retain operational metadata (request ids, model, token counts, latency, cost, region, API-key hash). The gateway and control plane are source-available under BUSL-1.1, converting to Apache-2.0 four years after each release. That is not OSI open source, and we do not call it that; it does mean every line that touches your prompt is public and inspectable.</p>
<p><strong>Privacy is routable.</strong> TensorZero routed for reliability and experiments. TrustedRouter also routes on privacy posture: <span class="mono">trustedrouter/zdr</span> restricts to providers with contractual or policy zero-data-retention commitments, <span class="mono">trustedrouter/e2e</span> to confidential-compute providers with provider-side end-to-end encryption (currently Tinfoil and Phala), <span class="mono">trustedrouter/eu</span> to an EU-focused provider order, and <span class="mono">provider.min_privacy</span> composes with any model id.</p>

<h2>Pricing, with bases stated</h2>
<p>The bases differ, so no single number settles it. TensorZero has no fees at all: Apache-2.0 software, your infrastructure (gateway, ClickHouse or Postgres, UI, optionally Valkey), and direct provider billing on your own keys. TrustedRouter prepaid inference bills the provider's token price plus 5.5%, with a $0.01 per million token floor; video is the provider quote plus 20%; there are no seat or subscription fees (<a href="/pricing">pricing</a>). BYOK is supported, and the pricing page does not publish a separate BYOK fee. If your ops time is free and your team is willing to maintain a fork, TensorZero is cheaper. If it is not, the 5.5% buys the operations, the catalog, and the attestation above.</p>

<h2>What we do not claim</h2>
<p>Our attestation covers the gateway, not the model providers behind it. Downstream handling is policy and contract, tracked per provider in our catalog tiers — except <span class="mono">e2e</span> routes, where the provider's own confidential-compute and encryption mechanisms apply, and those are the provider's claims, not our attestation. Our repositories are young: public since late April 2026, with benchmark history from June 2026. We publish a DPA, BAA, and subprocessor list, but no SOC 2 or HIPAA certification today (<a href="/security">security</a>). And nothing here replaces TensorZero's optimization loop: we have no fine-tuning recipes, no feedback API, no gateway-run A/B tests. We support <a href="/docs/evals">small evals</a>; that is a fraction of the flywheel.</p>

<h2>Migration in practice</h2>
<p>If your application used TensorZero's OpenAI-compatible endpoint, migration is a base-URL and key swap, plus deleting <span class="mono">tensorzero::</span> extra-body parameters and mapping function names to concrete model ids. If it used the native <span class="mono">/inference</span> API, prompt templates and schemas move out of gateway TOML into your application code, and calls are rewritten as chat completions. Historical data is the easy part — it already sits in your ClickHouse or Postgres in documented schemas. The hard part is honest: episodes, feedback, and the optimization recipes have no TrustedRouter equivalent, so teams that depend on them either keep the archived stack running for that workflow or rebuild it on other tooling.</p>""",
    ),
    _comparison(
        slug="bifrost",
        name="Bifrost",
        category="Self-hosted AI gateway",
        summary=(
            "Bifrost is Maxim AI's Apache-2.0 Go gateway: you deploy and operate it "
            "yourself, pay providers directly on your own keys, and there is no hosted "
            "option. TrustedRouter is the hosted alternative: a hardware-attested "
            "gateway, no durable prompt logs on realtime inference, and one prepaid key "
            "across 550+ model routes."
        ),
        competitor_fit=(
            "Choose Bifrost when you can operate your own gateway and want prompts inside "
            "your perimeter: the Apache-2.0 core ships failover, weighted load balancing, "
            "virtual keys with budgets, semantic caching, and an MCP gateway at no "
            "license cost; the compiled Go binary keeps per-request overhead in the "
            "microsecond range on their published tests; and air-gapped deployment (an "
            "Enterprise option) has no hosted equivalent."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want the gateway operated and verified for "
            "you: realtime inference with no durable prompt or output logs, a gateway "
            "build hardware-attested on GCP, AWS, and Azure that you can check at "
            "trust.trustedrouter.com, and one prepaid key across 550+ model routes at "
            "provider price + 5.5% ($0.01/M floor), including zdr, e2e, and eu privacy "
            "routes."
        ),
        migration=(
            "For OpenAI-format clients it is a base URL and key change; clients on "
            "Bifrost's Anthropic-native endpoint can use our Anthropic-style Messages "
            "surface, while Google GenAI-native clients need code changes. Provider keys "
            "retire (prepaid) or move to BYOK. Bifrost virtual keys, budgets, routing "
            "rules, semantic-cache settings, MCP configs, and plugin hooks must be "
            "recreated or dropped, and self-hosted telemetry history should be exported "
            "first."
        ),
        deployment="Self-hosted only (binary, Docker, Helm, or Go package); no managed offering",
        api="Native OpenAI, Anthropic, and Google GenAI endpoints; LangChain/LiteLLM drop-in",
        catalog="~21 providers (README: 23+), 1,000+ models, on your own provider keys",
        routing=(
            "Failover, weighted LB, custom rules, semantic cache; adaptive LB/HA are Enterprise"
        ),
        observability="Built-in UI, Prometheus, OTel; Datadog/BigQuery export is Enterprise",
        content="Gateway mode logs full request/response to SQLite by default; flag to disable",
        verification="Apache-2.0 source you audit; runtime assurance is your own deployment's",
        billing="Software free; provider spend direct at provider price; Enterprise quote-based",
        sources=(
            ComparisonSource(
                "Bifrost GitHub repository (Apache-2.0, stars, providers)",
                "https://github.com/maximhq/bifrost",
            ),
            ComparisonSource(
                "Bifrost pricing: free OSS tier vs quote-based Enterprise",
                "https://www.getmaxim.ai/bifrost/pricing",
            ),
            ComparisonSource(
                "Bifrost docs: gateway-mode content logging defaults",
                "https://docs.getbifrost.ai/features/observability/default",
            ),
            ComparisonSource(
                "Bifrost docs: drop-in replacement for OpenAI/Anthropic/GenAI SDKs",
                "https://docs.getbifrost.ai/features/drop-in-replacement",
            ),
            ComparisonSource(
                "Bifrost vs LiteLLM benchmarks (self-published)",
                "https://www.getmaxim.ai/bifrost/resources/benchmarks",
            ),
            ComparisonSource("Bifrost documentation root", "https://docs.getbifrost.ai"),
        ),
        faq_items=(
            (
                "Is Bifrost faster than TrustedRouter?",
                """For gateway overhead inside your own network, probably yes: a compiled Go binary running next to your app is a hop no hosted gateway can remove, and that is a real architectural advantage. Bifrost's specific numbers (11 µs internal overhead, 54x p99 over LiteLLM) are self-published and run on small AWS instances — the 54x comparison on a 2-vCPU t3.medium, the 11 µs overhead on a 4-vCPU t3.xlarge; no head-to-head with TrustedRouter exists. We publish per-region latency and TTFT on /status and frozen monthly reports on /benchmarks/reports so you can check our side.""",
            ),
            (
                "Which handles prompt content more safely?",
                """They protect against different things. A self-hosted Bifrost with disable_content_logging set keeps prompts entirely in your perimeter, the strongest boundary available if you run it well; note the flag defaults to false, so out of the box gateway mode writes full request and response content to local SQLite. TrustedRouter protects you from us: realtime inference keeps no durable prompt or output logs, and the gateway build is hardware-attested on three clouds so you verify the running code instead of trusting a policy. Our attestation covers the gateway, not the model providers behind it.""",
            ),
            (
                "What does each actually cost?",
                """Different bases, so do not compare the percentages directly. Bifrost's software is free (Apache-2.0); you pay every provider directly at the provider's price on your own keys, plus infrastructure and the engineering time to run it, and clustering, SSO, guardrails, audit logs, and RBAC sit in a quote-priced Enterprise tier with no public numbers. TrustedRouter has no seat or subscription fee: prepaid inference bills at the provider's token price plus 5.5% with a $0.01 per million token floor, and BYOK is supported without a published separate fee. Whether 5.5% beats the cost of operating a gateway depends on your volume and your team.""",
            ),
        ),
        article_html="""<h2>What Bifrost is</h2>
<p>Bifrost is an AI gateway written in Go by Maxim AI (legal entity H3 Labs Inc.). The core is Apache-2.0 with no carve-outs (<a href="https://raw.githubusercontent.com/maximhq/bifrost/main/LICENSE">LICENSE</a>). You run it yourself: a single binary started with <span class="mono">npx -y @maximhq/bifrost</span>, a Docker image, a Helm chart, or embedded in your application as a Go package. There is no hosted Bifrost. The paid Enterprise tier is a licensed feature set plus support for your own deployment (VPC, on-premise, or air-gapped), with quote-based pricing and no public dollar amounts.</p>
<p>It launched mid-2025 and moves fast. As of August 16, 2026 the <a href="https://github.com/maximhq/bifrost">repo</a> has roughly 7,300 stars, the latest release (transports v1.6.11) is two days old, and the <a href="https://docs.getbifrost.ai">docs</a> list about 21 providers, with the README claiming 23+ providers and 1,000+ models. It exposes native <span class="mono">/openai</span> and <span class="mono">/anthropic</span> endpoints plus a Google GenAI surface, with documented drop-in replacement for those SDKs, LangChain, and the LiteLLM SDK.</p>
<h2>Where Bifrost is the right call</h2>
<p>The free core is unusually complete. Automatic failover, weighted load balancing, virtual keys with budgets and rate limits, custom routing rules, semantic caching, an MCP gateway with tool governance, and a built-in observability UI all ship under Apache-2.0 — several of these are paid features in competing gateways.</p>
<p>Performance is the headline pitch. Their self-published benchmark reports 11 &micro;s internal overhead at 5,000 RPS and a 54x p99 advantage over LiteLLM at 500 RPS. Treat the numbers with care: they are undated, the LiteLLM comparison ran on 2-vCPU instances where Python gateways degrade worst, the 11 &micro;s figure comes from a larger 4-vCPU t3.xlarge (the 2-vCPU box measured 59 &micro;s), and the headline multiplier drifts between 40x, 50x, and 54x across their own pages, with no independent replication found. The architectural point survives the caveats — a compiled Go binary avoids the failure class those tests target, and they publish a <a href="https://www.getmaxim.ai/bifrost/resources/benchmarks">benchmark page</a> and a run-your-own guide.</p>
<p>Self-hosting is also the strongest data boundary available. Bifrost inside your perimeter, with content logging turned off, puts no third party in the request path. No hosted gateway can match that, ours included.</p>
<h2>Three differences that decide it</h2>
<h3>Who runs the gateway, and how you verify it</h3>
<p>With Bifrost, assurance is your own audit plus your own operations: read the Apache-2.0 source, deploy it, keep it patched and available. Clustering and high availability are Enterprise, so HA is quote-priced or engineered yourself. TrustedRouter is hosted, and you cannot inspect our servers, so we attest instead: the gateway runs published builds inside TEEs on GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers, each with a live attestation endpoint (<span class="mono">api</span>, <span class="mono">api-aws</span>, <span class="mono">api-azure</span>.trustedrouter.com) and a verifier script — details at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> and <a href="/security">/security</a>. The boundary is precise: attestation covers our gateway build and nothing downstream. Model providers are covered by cited retention-policy tiers, except on <span class="mono">trustedrouter/e2e</span> routes (tinfoil, phala), where the provider's own confidential-compute and end-to-end encryption posture applies — the provider's mechanism, not our attestation.</p>
<h3>What happens to prompt content by default</h3>
<p>Bifrost's gateway mode enables its logging plugin by default and writes full request and response content to a local SQLite store. A <span class="mono">disable_content_logging</span> flag exists, and it defaults to false — the behavior is documented in <a href="https://docs.getbifrost.ai/features/observability/default">their observability docs</a>. It stays on your own disk rather than with a vendor, but teams expecting a pass-through proxy should flip the flag first. On TrustedRouter, realtime inference keeps no durable prompt or output logs; we retain operational metadata only (request ids, model, token counts, latency, cost, region, API-key hash) — see <a href="/privacy">/privacy</a>. The opt-in Batch API is the exception: enclave-encrypted artifacts retained up to 30 days.</p>
<h3>Provider keys, catalog, and what routing costs</h3>
<p>The fee bases differ, so keep them apart. Bifrost software costs nothing to license; you hold a key for each provider, pay each provider directly at their price, and carry the infrastructure and operations cost, with Enterprise features quote-priced. TrustedRouter is one prepaid key across 550+ model routes and roughly 49 providers (as of August 2026), billed at the provider's token price plus 5.5% with a $0.01 per million token floor; video is the provider quote plus 20%. Per-route prices are on <a href="/models">/models</a>, the full schedule on <a href="/pricing">/pricing</a>. BYOK is supported; we do not publish a separate BYOK fee. Routing also carries privacy tiers Bifrost does not model: <span class="mono">trustedrouter/zdr</span> restricts to providers with contractual zero data retention, <span class="mono">trustedrouter/e2e</span> to confidential-compute providers with provider-side encryption, and <span class="mono">trustedrouter/eu</span> to an EU provider order, all composable with per-request preferences.</p>
<h2>What we do not claim</h2>
<ul>
<li>Our gateway and control plane are not open source. They are source-available under BUSL-1.1, converting to Apache-2.0 four years after each release; the SDKs are Apache-2.0 or MIT. On licensing, Bifrost's core is more open than ours today.</li>
<li>Our attestation does not extend to model providers, and community <span class="mono">user-*</span> routes leave the attested boundary entirely.</li>
<li>We are younger than they are. Our repos have been public since late April 2026 and our benchmark history starts June 2026. Judge our record by the <a href="/status">status page</a> and the frozen monthly <a href="/benchmarks/reports">benchmark reports</a>, not by our word.</li>
<li>We publish a DPA, BAA, and subprocessor list but claim no SOC 2 or HIPAA certification today. Maxim AI displays SOC 2 and ISO 27001 badges for its company and hosted platform; a self-hosted Bifrost deployment's compliance is the operator's own in either case.</li>
</ul>
<h2>Migration reality</h2>
<p>For OpenAI-format clients the move is a base URL and API key change — Bifrost's own drop-in story running in reverse. Clients on Bifrost's <span class="mono">/anthropic</span> endpoint can target our Anthropic-style Messages surface; Google GenAI-native clients need code changes, because we expose OpenAI and Anthropic formats. Provider keys re-home: retire them on prepaid, or keep committed-spend discounts through BYOK. Bifrost virtual keys, per-team budgets and rate limits, routing rules, semantic-cache settings, MCP tool configs, and plugin hooks have no automatic mapping — recreate what has an equivalent (MCP: <a href="/docs/mcp">/docs/mcp</a>); plugins do not carry over. Export Prometheus and SQLite/Postgres log history before switching; our observability is metadata analytics built from traffic routed through us. You shed gateway operations: upgrades, scaling, and the HA engineering Bifrost prices into Enterprise.</p>""",
    ),
    _comparison(
        slug="not-diamond",
        name="Not Diamond",
        category="Intelligent model router",
        summary=(
            "Not Diamond predicts the best model per request — since August 2026 chiefly "
            "for coding agents, via a local proxy that executes through your existing "
            "gateway for $0.05 per million tokens routed. TrustedRouter is the execution "
            "layer itself: about 550 model routes, provider failover, prepaid billing, "
            "and an attested gateway with no durable prompt logs on realtime inference."
        ),
        competitor_fit=(
            "Choose Not Diamond when learned per-prompt selection is the point: custom "
            "routers trained on your own scored evals in about an hour, per-step model "
            "and reasoning-effort routing for coding agents, and a design that keeps "
            "payloads out of the routing vendor's hands while you keep your existing "
            "gateway and provider contracts."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you need the execution layer: one "
            "OpenAI-compatible API over about 550 model routes and roughly 49 providers, "
            "provider failover, prepaid billing at provider price + 5.5% ($0.01/M floor), "
            "ZDR and E2E privacy routes plus an EU jurisdiction route, and a gateway "
            "build you can verify by live attestation on three clouds."
        ),
        migration="""Decide first whether you are replacing Not Diamond or pairing it with a gateway — it executes through one by design. To consolidate: legacy API users delete the select_model call and client-side execution branching and point an OpenAI-compatible client at the TrustedRouter base URL; Not Diamond Code users remove the local proxy from the harness config. A custom router trained on your evals does not carry over — our routes are policy pools, not learned predictors — so re-run the same task evals before shifting production traffic.""",
        deployment=(
            "Local proxy beside your coding agent; executes through your existing gateway "
            "or provider"
        ),
        api=(
            "select_model API plus new Apache-2.0 Python/TS SDKs; 2024-era SDKs archived late 2025"
        ),
        catalog=(
            "~80 candidate models across 13 providers (May 2026 docs); you supply provider access"
        ),
        routing="Learned per-prompt selection plus custom routers trained on your scored evals",
        observability="Session IDs, savings/usage dashboard; org-wide analytics on enterprise tier",
        content=(
            "Code proxy routes on derived metadata; hosted content generally retained ~90 "
            "days where retention is enabled"
        ),
        verification=(
            "SOC 2 and ISO 27001 claimed in Aug 2026 launch post; no public trust portal found"
        ),
        billing="$0.05 per 1M tokens routed; inference billed by your own providers or gateway",
        sources=(
            ComparisonSource(
                "Not Diamond pricing ($0.05/M routed)", "https://www.notdiamond.ai/pricing"
            ),
            ComparisonSource(
                "Not Diamond Code launch post (Aug 4, 2026)",
                "https://www.notdiamond.ai/blog/not-diamond-code-intelligent-model-routing-for-coding-agents",
            ),
            ComparisonSource(
                "Not Diamond: Model Routing vs Gateways (Jun 2026)",
                "https://www.notdiamond.ai/blog/model-routing-vs-gateways-breaking-down-the-difference",
            ),
            ComparisonSource(
                "Not Diamond custom router training docs",
                "https://docs.notdiamond.ai/docs/router-training-quickstart",
            ),
            ComparisonSource(
                "Not Diamond supported models docs", "https://docs.notdiamond.ai/docs/llm-models"
            ),
            ComparisonSource("Not Diamond privacy policy", "https://www.notdiamond.ai/privacy"),
            ComparisonSource(
                "Fortune on the model-router market (Aug 9, 2026)",
                "https://fortune.com/2026/08/09/why-every-company-wants-an-ai-model-router-right-now/",
            ),
        ),
        faq_items=(
            (
                "Can we run Not Diamond Code on top of TrustedRouter?",
                (
                    "Architecturally yes. Not Diamond describes Code as harness- and "
                    "gateway-agnostic: the local proxy picks the model, then executes "
                    "through your existing gateway, and TrustedRouter exposes "
                    "OpenAI-compatible chat at one base URL. Neither company publishes a "
                    "tested integration for this specific pairing, so run a small eval "
                    "first. You would pay both fees: $0.05 per million tokens routed to "
                    "Not Diamond, and provider price + 5.5% to us for execution."
                ),
            ),
            (
                "Does TrustedRouter offer learned per-prompt routing like Not Diamond?",
                (
                    "No. Our routes (auto, free, cheap, fast, zdr, e2e, eu) are policy "
                    "pools with composable preferences and provider failover; nothing in "
                    "our stack trains a predictor on your evals. If squeezing "
                    "quality-per-dollar out of a fixed candidate set with a router "
                    "trained on your own scored examples is the goal, Not Diamond is "
                    "genuinely better at that. What our routes enforce instead is "
                    "execution-side guarantees: privacy floors and an attested gateway "
                    "path."
                ),
            ),
            (
                "Which vendor handles our prompts more carefully?",
                """They answer different questions. In privacy-preserving mode, Not Diamond says it never receives your payload — a structurally strong design — though where retention is enabled its hosted services generally keep content about 90 days, API logs up to 18 months, and no payload-processing opt-out for hosted model services. TrustedRouter does receive prompts, because we execute the call, and proves handling instead: no durable prompt or output logs on realtime inference, inside a gateway build attested on three clouds at trust.trustedrouter.com.""",
            ),
        ),
        article_html="""<h2>What Not Diamond is</h2>
<p>Not Diamond predicts which model should handle each request; execution stays on infrastructure you already run. Its June 2026 <a href="https://www.notdiamond.ai/blog/model-routing-vs-gateways-breaking-down-the-difference">explainer on routers versus gateways</a> places the product between your agent and your gateway. Not Diamond is San Francisco based, roughly 15 people, on a $2.3M seed raised in July 2024 with angels including Jeff Dean and Tom Preston-Werner. Fortune's August 9, 2026 coverage of the router market names SAP among its enterprise clients.</p>
<p>There are two product generations. The 2024-2025 model-selection API returns a recommendation from a <span class="mono">select_model</span> call and your client executes the request itself, with optional fuzzy hashing so the recommendation engine never receives raw query text. Since August 4, 2026 the flagship is <a href="https://www.notdiamond.ai/blog/not-diamond-code-intelligent-model-routing-for-coding-agents">Not Diamond Code</a>: a local proxy beside your coding harness that sends derived request metadata to Not Diamond's optimization service, gets back a model and reasoning-effort choice, and executes through your existing gateway or provider. The <a href="https://www.notdiamond.ai/pricing">pricing page</a> lists $0.05 per million tokens routed, a stated 100-150ms of added latency per recommendation, and claimed inference savings of at least 20-40%. Not Diamond does not resell execution; inference is billed by your providers.</p>
<h2>Where it is the right choice</h2>
<p>Learned per-prompt selection is Not Diamond's specialty, productized end to end. You upload a CSV of prompts, candidate-model responses, and evaluation scores (15 to 10,000 samples) and get a custom router back within about an hour. Their RoRF router is open source under MIT. For coding agents, Not Diamond Code routes each step of a session, choosing both the model and the reasoning effort from request complexity and conversation history.</p>
<p>The architecture also keeps payloads out of Not Diamond's hands. With privacy-preserving routing enabled, Not Diamond Code decides from locally computed features and derived metadata; the launch post says routing works "without ever requiring agent payloads, inputs, or outputs, to leave a developer's local machine." The legacy API went further by never executing calls at all. And because Code rides on your existing gateway and provider contracts, the execution side has no lock-in: you keep negotiated rates and can drop the router without re-plumbing inference.</p>
<h2>The load-bearing difference: selection versus execution</h2>
<p>TrustedRouter is the layer Not Diamond assumes you already have. We execute the request: one OpenAI-compatible API over about 550 model routes across roughly 49 providers as of August 16, 2026 (see <a href="/models">/models</a>), with provider failover, prepaid billing, and BYOK. Not Diamond's docs list about 80 candidate models across 13 providers as of May 2026, and you hold the credentials for each provider it might select.</p>
<p>On the selection axis, Not Diamond's routing is the more sophisticated. TrustedRouter routes are policy pools with composable preferences: <span class="mono">trustedrouter/auto</span>, <span class="mono">free</span>, <span class="mono">cheap</span>, <span class="mono">fast</span>, <span class="mono">zdr</span>, <span class="mono">e2e</span>, and <span class="mono">eu</span>, plus provider ordering, privacy floors, and jurisdiction pins. Nothing in our stack trains a per-prompt predictor on your evals. What we add instead is enforcement: <span class="mono">trustedrouter/zdr</span> refuses providers below a contractual zero-data-retention tier, and <span class="mono">trustedrouter/e2e</span> routes only to confidential-compute providers with provider-side end-to-end encryption (currently tinfoil and phala).</p>
<p>The fees sit on different bases and cannot be equated. Not Diamond charges a flat $0.05 per million tokens routed, on top of whatever your gateway and providers bill for inference. TrustedRouter's prepaid fee is 5.5% of the provider's token price with a $0.01 per million token floor, and that is the whole bill: at a $3.00/M provider price you pay $3.165/M, execution included. One is a selection fee, the other an execution markup; per-model numbers are on <a href="/pricing">/pricing</a>.</p>
<h2>Privacy: two models, both real</h2>
<p>Not Diamond's strongest privacy argument is structural: in privacy-preserving mode your payload never reaches them. The caveats live in their <a href="https://www.notdiamond.ai/privacy">privacy policy</a>: hosted-service input and output content is retained about 90 days by default, API logs up to 18 months, and for hosted model services opt-out from payload processing is not available. We could not establish whether privacy-preserving routing is on by default.</p>
<p>TrustedRouter sees your prompt, because we execute the call, so we prove what happens to it instead. Realtime inference keeps no durable prompt or output logs; what we retain is metadata: request ids, model and provider, token counts, latency, cost, region, and API-key hash (full list on <a href="/privacy">/privacy</a>). The gateway build serving <span class="mono">api.trustedrouter.com</span> is attested on GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers; anyone can run the verifier against <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>, and <a href="/security">/security</a> documents the model. The attestation boundary stops at our gateway: downstream model providers are covered by cited policy tiers, except the E2E routes, where the provider's own confidential-compute mechanisms apply.</p>
<h2>What we do not claim</h2>
<p>Our repos are young: public since late April 2026, with benchmark history from June 2026 — a short operating record. The 99.99% figure on <a href="/status">/status</a> is a published availability target with live burn rates, not a certified SLA. We publish a DPA, BAA, and subprocessor list, but no SOC 2 or HIPAA certification today; Not Diamond claims SOC 2 compliance and ISO 27001 certification in its August 2026 launch post, self-reported, with no public trust portal we could find. And our gateway and control plane are source-available under BUSL-1.1, converting to Apache-2.0 four years after each release. Every line that touches your prompt is public, but it is not OSI open source; the SDKs are Apache-2.0 or MIT.</p>
<h2>Migration, or coexistence</h2>
<p>Not Diamond routes through a gateway by design, so the first question is whether you are replacing it or pairing it. Not Diamond describes Code as gateway-agnostic, so TrustedRouter can serve as the gateway underneath it — though neither company publishes a tested integration. If you consolidate on TrustedRouter alone: legacy API users delete the <span class="mono">select_model</span> call and the client-side execution branching, then point one OpenAI-compatible client at our base URL; Not Diamond Code users remove the local proxy from the harness configuration. What does not carry over is the learned selection itself. A custom router trained on your scored evals has no TrustedRouter equivalent, and our policy routes will choose differently, so re-run the same task-specific evals before moving production traffic; see <a href="/docs/evals">/docs/evals</a>.</p>""",
    ),
    _comparison(
        slug="martian",
        name="Martian",
        category="Hosted AI gateway",
        summary=(
            "Martian invented the commercial LLM router in 2023, then pivoted: today it "
            "is an interpretability research lab whose hosted gateway serves 289 models "
            "(as of August 2026) with explicit model selection and pass-through pricing. "
            "TrustedRouter is a routing gateway with hardware-attested execution on three "
            "clouds and no durable prompt logs on realtime inference."
        ),
        competitor_fit=(
            "Choose Martian for pass-through pricing at provider list rates "
            "(spot-verified across six models, August 2026), a public SOC 2 Type II and "
            "ISO 27001:2022 trust center, and OpenAI Chat Completions, Responses, and "
            "Anthropic Messages compatibility with coding-agent integration docs, when "
            "explicit model selection is all your application needs."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you need actual routing, including auto, cheap, "
            "fast, ZDR, E2E, and EU routes with provider failover, plus a written "
            "no-durable-prompt-logs commitment on realtime inference, enforced by a "
            "gateway whose running build you can verify against live attestations on "
            "three clouds."
        ),
        migration=(
            "Both sides speak OpenAI Chat Completions, Responses, and Anthropic Messages, "
            "so every Martian surface has a direct TrustedRouter target: swap the base "
            "URL and key, then map creator/model-name ids to TrustedRouter ids. The "
            "martian_metadata parameter and per-model reliability tiers do not carry "
            "over, and there is no automatic-routing behavior to replicate."
        ),
        deployment="Hosted proprietary gateway run by Martian Learning, Inc.; nothing to self-host",
        api="OpenAI Chat Completions and Responses, plus Anthropic Messages",
        catalog=(
            "289 models across 49 provider prefixes (2026-08-16); no embeddings or image "
            "endpoints documented"
        ),
        routing=(
            "Explicit model selection only; the 2023 automatic router is no longer a public product"
        ),
        observability=(
            "Dashboard usage and request history; martian_metadata tags; no log export documented"
        ),
        content=(
            "ToS grants AI-training rights over Customer Materials; no published "
            "retention window, opt-out, or ZDR"
        ),
        verification=(
            "SOC 2 Type II and ISO 27001:2022 on a public Vanta trust center; pen test under NDA"
        ),
        billing="Pass-through at provider list prices (spot-verified); no published gateway fee",
        sources=(
            ComparisonSource(
                "Martian Gateway quickstart (base URL, explicit model choice)",
                "https://gateway-docs.withmartian.com/quickstart",
            ),
            ComparisonSource(
                "Martian Gateway endpoints (Chat Completions, Responses, Messages)",
                "https://gateway-docs.withmartian.com/api-reference/endpoints",
            ),
            ComparisonSource(
                "Live Martian model catalog API", "https://api.withmartian.com/v1/models"
            ),
            ComparisonSource(
                "Martian Terms of Service (Jan 15, 2026)",
                "https://withmartian.com/terms-of-service",
            ),
            ComparisonSource(
                "Martian trust center (SOC 2 Type II, ISO 27001:2022)",
                "https://trust.withmartian.com",
            ),
            ComparisonSource(
                "RouterBench paper (arXiv 2403.12031)", "https://arxiv.org/abs/2403.12031"
            ),
            ComparisonSource(
                "TechCrunch launch coverage (Nov 15, 2023)",
                "https://techcrunch.com/2023/11/15/martians-tool-automatically-switches-between-llms-to-reduce-costs/",
            ),
            ComparisonSource(
                "Accenture investment press release (Sep 17, 2024)",
                "https://newsroom.accenture.com/news/2024/accenture-invests-in-martian-to-bring-dynamic-routing-of-large-language-queries-and-more-effective-ai-systems-to-clients",
            ),
        ),
        faq_items=(
            (
                "Does Martian still route prompts to the best model automatically?",
                (
                    "No. As of August 2026 the gateway docs describe explicit model "
                    "selection only, the live catalog contains no auto or router model "
                    "ids, and the old router site no longer resolves. The routing "
                    "research is real (RouterBench, over 405k inference outcomes), and "
                    "routing-style cost optimization now appears at sister lab Thesean "
                    "AI. On TrustedRouter, trustedrouter/auto, cheap, fast, zdr, e2e, and "
                    "eu are live routes with provider failover."
                ),
            ),
            (
                "Who has the stronger compliance certifications?",
                (
                    "Martian. It lists SOC 2 Type II and ISO 27001:2022 on a public Vanta "
                    "trust center, with penetration test reports and a DPA available on "
                    "request, and a published subprocessor list. TrustedRouter publishes "
                    "a DPA, BAA, and subprocessor list, and has no published SOC 2 or "
                    "HIPAA certification today; our verification story is live hardware "
                    "attestation of the gateway build, which is a different kind of "
                    "evidence, not a substitute for a certification requirement."
                ),
            ),
            (
                "What happens to prompt content on each service?",
                """Martian's terms of service (updated January 15, 2026) grant it a license to use Customer Materials to train and improve its AI models — deidentification is required only under a separate aggregate-data clause — with no public opt-out, stated retention window, or ZDR option, and the gateway docs have no data-handling page. TrustedRouter's realtime inference keeps no durable prompt or output logs; operational metadata (ids, model, tokens, latency, cost, region, key hash) is retained; batch is a separate opt-in mode with enclave-encrypted retention up to 30 days. The gateway enforcing this is attested at trust.trustedrouter.com.""",
            ),
        ),
        article_html="""<h2>What Martian is in 2026</h2><p>Martian Learning, Inc. launched the first commercial LLM router out of stealth in November 2023, founded in 2022 by UPenn LLM researchers Shriyash Upadhyay and Etan Ginsberg. Its <a href="https://arxiv.org/abs/2403.12031">RouterBench</a> paper (March 2024, over 405k inference outcomes) is still a standard reference for routing evaluation, and the code is MIT-licensed. The company's public identity has since split in two. The homepage now presents an interpretability research lab: active MIT-licensed benchmarks (ARES and code-review-benchmark both saw pushes in August 2026) and a $1M interpretability prize (submissions currently closed). The commercial product is the <a href="https://gateway-docs.withmartian.com/quickstart">Martian Gateway</a>, a hosted multi-provider API at <span class="mono">api.withmartian.com/v1</span> whose catalog dates from about October 2025.</p><p>The fact most evaluators miss: the automatic router is no longer part of the public product. The gateway docs describe explicit model selection only, the live catalog contains no auto or router ids, and the old router site no longer resolves. Routing-style cost optimization now appears at a sister lab, Thesean AI. Third-party reviews of "the Martian router" quoting $20/month describe a product whose pages are dead.</p><h2>Where Martian is strong</h2><p>Three things hold up under checking. First, pricing: gateway model prices are pass-through at provider list rates. We spot-checked six models against provider price sheets on August 16, 2026 — <span class="mono">anthropic/claude-sonnet-4-5</span> at $3.00/$15.00 per million tokens, <span class="mono">openai/gpt-5</span> at $1.25/$10.00, <span class="mono">google/gemini-2.5-flash</span> at $0.30/$2.50 — all exact matches, with prices refreshed every five minutes.</p><p>Second, compliance. Martian runs a public <a href="https://trust.withmartian.com">Vanta trust center</a> listing SOC 2 Type II and ISO 27001:2022, penetration test reports and a DPA available on request, and a published subprocessor list with change notifications. That is a serious posture for a company that raised a $9M seed in 2023 and took an Accenture Ventures investment in 2024.</p><p>Third, API breadth: OpenAI Chat Completions, the OpenAI Responses API, and Anthropic Messages, with streaming and tool calling, plus integration docs for Claude Code, Cursor, Codex, and Cline. The live catalog held 289 models across 49 provider prefixes on August 16, 2026, each with per-model pricing, cache pricing, and a reliability tier.</p><h2>Three differences that decide it</h2><p><strong>Routing exists here.</strong> TrustedRouter ships <span class="mono">trustedrouter/auto</span>, <span class="mono">cheap</span>, <span class="mono">fast</span>, and <span class="mono">free</span> routes plus privacy-tier routes: <span class="mono">trustedrouter/zdr</span> restricts to providers with contractual or policy zero data retention, <span class="mono">trustedrouter/e2e</span> to confidential-compute providers (tinfoil, phala), and <span class="mono">trustedrouter/eu</span> to EU-focused providers, all with provider failover. The Martian gateway routes nothing automatically; you name the model.</p><p><strong>Prompt handling is written down and checkable.</strong> Martian's terms of service (updated January 15, 2026) grant it a license to use Customer Materials to train and improve its AI models — deidentification is required only under a separate aggregate-data clause — with no public opt-out, no stated retention window, no ZDR option, and no data-handling page in the gateway docs. Our <a href="/privacy">privacy policy</a> commits realtime inference to no durable prompt or output logs; operational metadata (ids, model, tokens, latency, cost, region, key hash) is retained; batch is a separate opt-in mode with enclave-encrypted retention up to 30 days. The gateway that enforces this runs inside TEEs on GCP, AWS, and Azure, with live attestation endpoints at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> binding the running build to published source and release digests.</p><p><strong>The prompt path is public.</strong> The Martian Gateway is a proprietary hosted service with no source repository. Every line of TrustedRouter code that touches your prompt is public: source-available under BUSL-1.1, converting to Apache-2.0 four years after each release, with Apache-2.0 or MIT SDKs.</p><p>On price, the bases differ, so state them plainly. Martian passes through provider list prices and publishes no gateway fee at all; how the gateway is monetized could not be established from public materials. TrustedRouter bills <a href="/pricing">provider cost plus 5.5%</a> with a $0.01 per million token floor, itemized per model on <a href="/models">/models</a>. Per token, Martian is cheaper today. Whether an undisclosed business model stays that way is a question to put to them.</p><h2>What we do not claim</h2><p>Our attestation covers the gateway build, not downstream model providers; provider privacy postures are policy and contract tiers, except E2E routes where the provider's own confidential-compute mechanisms apply. Our public repos date from late April 2026 and public benchmark history from June 2026, so our operating track record is short; Martian has been a company since 2022. We publish a <a href="/security">security page</a>, DPA, BAA, and subprocessor list, and hold no published SOC 2 or HIPAA certification today. On certifications, Martian is ahead.</p><h2>Migration reality</h2><p>Both sides speak all three API shapes, so every Martian surface has a direct target: change the base URL and key, then map <span class="mono">creator/model-name</span> ids to TrustedRouter ids on <a href="/models">/models</a> (about 550 model routes across 49 providers as of August 16, 2026). Two things do not carry over: the proprietary <span class="mono">martian_metadata</span> parameter, and Martian's per-model reliability tiers, for which the nearest equivalents are the provider health table on <a href="/status">/status</a> and our monthly frozen benchmark reports. There is no automatic-routing behavior to replicate; adopting <span class="mono">trustedrouter/auto</span> or a privacy route afterward is optional and one line.</p>""",
    ),
    _comparison(
        slug="kong-ai-gateway",
        name="Kong AI Gateway",
        category="API gateway with OSS core and enterprise AI tier",
        summary=(
            "Kong AI Gateway runs AI plugins on the Apache-2.0 Kong Gateway core or on "
            "Konnect, governing LLM, MCP, and A2A traffic with your own provider keys. "
            "TrustedRouter is a hosted model marketplace with a hardware-attested prompt "
            "path and no durable prompt logs on realtime inference."
        ),
        competitor_fit=(
            "Choose Kong when you already run Kong or Konnect, when prompts must stay on "
            "data planes you operate, or when you need gateway-level MCP and A2A "
            "governance: OAuth 2.1, tool-level ACLs, and token exchange over agent "
            "traffic."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want one key across roughly 49 providers "
            "billed at provider cost + 5.5% per token, with privacy you can verify: a "
            "gateway attested on three clouds and realtime inference that keeps no "
            "durable prompt or output logs."
        ),
        migration=(
            "Kong's AI ingress is already OpenAI-format, so clients repoint the base URL "
            "and swap Kong consumer keys for TrustedRouter keys; Kong model aliases remap "
            "to ids on /models. Gateway-side policy does not carry over: semantic caches, "
            "prompt guards, RAG injection, and MCP ACLs must be recreated or dropped."
        ),
        deployment=(
            "Self-hosted OSS/Enterprise Kong Gateway, or Konnect SaaS with Kong-run data planes"
        ),
        api=("OpenAI-format ai-proxy over routes you define; native Anthropic SDK support in 3.13"),
        catalog=(
            "15+ providers via ai-proxy, BYO keys; no hosted marketplace or unified token billing"
        ),
        routing="7 LB algorithms, fallback, circuit breakers in enterprise-only AI Proxy Advanced",
        observability=(
            "Token, cost, and latency per model via Prometheus/OTel; Grafana and Konnect dashboards"
        ),
        content=(
            "Payload logging off by default; prompts stay on self-run data planes in hybrid mode"
        ),
        verification=(
            "SOC 2 + PCI DSS 4.0 on Dedicated Cloud Gateways; enterprise AI plugins closed source"
        ),
        billing=(
            "Konnect Plus: $100/mo per LLM model (max 5) + plane fees; provider bills stay yours"
        ),
        sources=(
            ComparisonSource(
                "Kong AI Gateway docs hub", "https://developer.konghq.com/ai-gateway/"
            ),
            ComparisonSource(
                "AI Proxy config reference (logging defaults)",
                "https://developer.konghq.com/plugins/ai-proxy/reference/",
            ),
            ComparisonSource(
                "AI Proxy Advanced plugin (enterprise tier)",
                "https://developer.konghq.com/plugins/ai-proxy-advanced/",
            ),
            ComparisonSource(
                "Konnect pricing incl. AI Gateway per-model rate", "https://konghq.com/pricing"
            ),
            ComparisonSource(
                "AI Gateway 3.14 release (Agent Gateway, A2A)",
                "https://konghq.com/blog/product-releases/kong-ai-gateway-3-14",
            ),
            ComparisonSource(
                "Konnect telemetry and networking",
                "https://developer.konghq.com/konnect-platform/network/",
            ),
            ComparisonSource(
                "Dedicated Cloud Gateways SOC 2 / PCI DSS 4.0",
                "https://konghq.com/products/kong-konnect/features/dedicated-cloud-gateways",
            ),
            ComparisonSource(
                "Kong 3.6 announcement: six OSS AI plugins",
                "https://konghq.com/blog/product-releases/announcing-kong-ai-gateway",
            ),
        ),
        faq_items=(
            (
                ("We already run Kong for API traffic. Is there a reason to add TrustedRouter?"),
                """Staying on Kong is defensible: if you have Konnect Enterprise and a platform team, the marginal cost of AI routes on an existing estate is low, and self-run data planes keep prompts on your infrastructure. TrustedRouter adds what Kong does not have: a hosted marketplace with one key across roughly 49 providers, per-token billing at provider cost + 5.5%, and an attested gateway with no durable prompt or output logs on realtime inference. Some teams keep Kong for API traffic and point only AI routes at TrustedRouter as an upstream.""",
            ),
            (
                "Which has the stronger privacy story?",
                """They use different mechanisms. Self-hosted Kong keeps prompts on machines you run, with payload logging off by default; if you can operate that, it is a genuinely strong posture, though the enterprise AI plugins are closed source and there is no build attestation. TrustedRouter is hosted: realtime inference keeps no durable prompt or output logs, and the gateway build is attested on GCP, AWS, and Azure against public source. Our attestation stops at the gateway; downstream providers are covered by policy-tier claims, except trustedrouter/e2e routes to confidential-compute providers.""",
            ),
            (
                "What does each actually cost?",
                """Kong: six basic AI plugins are free and Apache-2.0 if you self-host; Konnect Plus adds $100/month per unique LLM model (max five) plus control-plane fees of $25 to $500/month and $200 per extra million requests; Enterprise is custom, billed annually. Model usage bills separately with your providers. TrustedRouter: provider cost + 5.5% per token with a $0.01/M floor and no subscription; BYOK is supported, with no separate published BYOK fee. The bases differ, a per-model subscription versus a per-token percentage, so run your own volumes.""",
            ),
        ),
        article_html="""<h2>What Kong AI Gateway actually is</h2>
<p>Kong AI Gateway is a set of AI plugins that run on Kong Gateway, the Apache-2.0 API gateway with roughly 44,000 GitHub stars and active development as of August 2026, and on Konnect, Kong's hosted control plane. You can self-host, run your own data planes under Konnect in hybrid mode, or let Kong operate Serverless and Dedicated Cloud Gateways. In every mode you bring your own provider accounts and keys: there is no model marketplace and no unified token billing. The <a href="https://developer.konghq.com/plugins/ai-proxy/">ai-proxy plugin</a> speaks OpenAI-format to 15+ providers, including OpenAI, Azure OpenAI, Anthropic, Bedrock, Vertex AI, Mistral, and self-hosted vLLM or Ollama.</p>
<p>The release pace is fast. From October 2025 to April 2026, versions 3.12 through 3.14 added an MCP proxy with OAuth 2.1 resource-server flows, tool-level ACLs, circuit breakers, dynamic model routing keyed on the request body, and an Agent Gateway governing A2A traffic with RFC 8693 token exchange. Kong governs LLM, MCP, and A2A traffic more broadly than any other gateway we compare against.</p>
<h2>Where Kong is genuinely strong</h2>
<p>Three things stand out. First, operational maturity: platform teams already know how to run Kong, and a deep catalog of existing plugins for authentication, rate limiting, and transforms composes directly with AI routes. Second, the default privacy posture for self-hosters is strong: prompts flow through data planes you run, payload logging is off by default (<span class="mono">log_payloads: false</span>), and Kong documents that Konnect control-plane telemetry carries service-level metrics only, with no customer data. Third, compliance surface: Dedicated Cloud Gateways carry SOC 2 and PCI DSS 4.0, and Konnect offers six control-plane geos (AU, EU, ME, US, IN, SG). If your requirement is that prompts never leave infrastructure you operate, and you have the platform team to run it, self-hosted Kong is a legitimate answer.</p>
<h2>The line between free and enterprise</h2>
<p>Exactly six AI plugins ship in the Apache-2.0 repo: ai-proxy, ai-prompt-guard, ai-prompt-template, ai-prompt-decorator, and the request and response transformers. The differentiating features sit in the proprietary <span class="mono">ai_gateway_enterprise</span> tier: AI Proxy Advanced (the seven load-balancing algorithms, cross-provider fallback, circuit breakers), semantic caching, semantic prompt guarding, RAG injection, and PII sanitization. Free ai-proxy routes one model per route. The semantic features also need infrastructure you provision, meaning Redis/Valkey or Postgres with pgvector plus an embeddings model, and the PII sanitizer requires a separate Docker service pulled from Kong's private registry.</p>
<p>On price, the bases differ from ours, so compare carefully. Konnect Plus (as of August 2026, <a href="https://konghq.com/pricing">konghq.com/pricing</a>) meters AI Gateway at $100 per month per unique LLM model, capped at five, on top of control-plane fees of $25 to $500 per month by gateway type and $200 per additional million API requests. Beyond five models you are into custom-priced Enterprise contracts, billed annually. Your model usage bills separately with your providers at your negotiated rates. <a href="/pricing">TrustedRouter pricing</a> for text and embeddings is a per-token fee: provider cost + 5.5% with a $0.01 per million token floor, and no seat or subscription fees. A per-model subscription and a per-token percentage are not comparable without your traffic shape; run your own volumes through both.</p>
<h2>What TrustedRouter does differently</h2>
<p>The trust model is the load-bearing difference. Kong's answer to who sees your prompts is deployment topology: run the data plane yourself and the question mostly disappears — though the enterprise AI plugins that touch prompts, such as the PII sanitizer and semantic cache, are closed source, so you cannot audit them, and there is no build attestation of what is running. Our answer is evidence on a hosted path: the gateway serving <span class="mono">api.trustedrouter.com</span> runs inside TEEs on GCP, AWS, and Azure, each publishing a live attestation endpoint that binds the running build to published source and release digests. A verifier script and Sigstore signatures are at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>, and the boundary is documented at <a href="/security">/security</a>. Realtime inference keeps no durable prompt or output logs; what we retain is metadata — ids, model, token counts, latency, cost, region, key hash — listed at <a href="/privacy">/privacy</a>.</p>
<p>The second difference is the marketplace. One key covers 550+ model routes across roughly 49 providers (as of August 2026, live at <a href="/models">/models</a>) with prepaid billing, so there are no per-provider accounts to open. Privacy is routable: <span class="mono">trustedrouter/zdr</span> restricts to providers with contractual or policy zero-data-retention, <span class="mono">trustedrouter/e2e</span> to confidential-compute providers with end-to-end encryption, <span class="mono">trustedrouter/eu</span> to EU-focused providers, and per-request preferences compose with any of them.</p>
<h2>What we do not claim</h2>
<p>Our attestation covers our gateway, not the model providers behind it. Downstream handling rests on contractual and policy commitments we track per provider, except on <span class="mono">trustedrouter/e2e</span> routes, where the provider's own confidential-compute and E2EE mechanisms apply. Kong's core is genuinely open source under Apache-2.0; our gateway and control plane are source-available under BUSL-1.1, converting to Apache-2.0 four years after each release, with Apache-2.0 and MIT SDKs. Every line that touches your prompt is public, but the platform is not OSI open source. We are young: our repos went public between late April and early May 2026 and published benchmark history starts in June 2026. We have no published SOC 2 or HIPAA certification today; Kong's Dedicated Cloud Gateways do carry SOC 2 and PCI DSS 4.0. And Kong's MCP and A2A governance — tool-level ACLs, token exchange, agent-traffic policy — goes deeper than anything we ship; we support MCP (<a href="/docs/mcp">docs</a>) but do not offer gateway-level agent-protocol governance.</p>
<h2>Moving between them</h2>
<p>Kong's AI ingress is already OpenAI-format on routes you defined, so client code mostly repoints the base URL and swaps Kong consumer credentials for TrustedRouter keys; model aliases configured in Kong plugins remap to ids on <a href="/models">/models</a>. Gateway-side policy does not travel: semantic caches, prompt guards, RAG injection, per-consumer AI rate limits, and MCP ACLs must be recreated on our side or consciously dropped. A common middle path keeps Kong for non-AI API traffic and points only the AI routes at TrustedRouter as an upstream. Start with one streamed request against <span class="mono">trustedrouter/zdr</span> and compare output, latency, provider selection, and billed usage before moving volume.</p>""",
    ),
    _comparison(
        slug="lmrouter",
        name="LMRouter",
        category="Open-source API router",
        summary=(
            "LMRouter is an MIT-licensed, self-hostable API router with zero inference "
            "markup and a hosted service whose repo has been dormant since September 2025 "
            "and whose model catalog froze in February 2026. TrustedRouter is an actively "
            "maintained gateway with hardware-attested execution, privacy-tier routing, "
            "and a 561-entry catalog (count includes meta routes) as of August 2026."
        ),
        competitor_fit=(
            "Choose LMRouter to self-host an MIT-licensed router under your own provider "
            "keys, or when 0% inference markup and free BYOK passthrough matter more than "
            "routing logic — and its ~8-provider catalog, frozen since February 2026, "
            "still covers the models you need."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you need current models with provider failover, "
            "privacy-tier routes (zdr, e2e, eu), and a hosted gateway you can verify by "
            "live attestation on three clouds rather than by reading source you cannot "
            "confirm is running."
        ),
        migration=(
            "Change the base URL, key, and model IDs: LMRouter uses OpenRouter-style "
            "vendor/model slugs, and canonical vendor-prefixed ids mostly resolve on "
            "TrustedRouter; a genuinely unknown id returns MODEL_NOT_SUPPORTED rather "
            "than guessing. Anthropic-native clients point at our Messages endpoint. One "
            "real caveat: LMRouter's Responses API keeps conversation state in its "
            "temporary cache, so re-test stateful flows after the move."
        ),
        deployment="MIT open source; self-host (TypeScript/Hono) or hosted at api.lmrouter.com",
        api=(
            "OpenAI Chat Completions and Responses, Anthropic Messages, images, audio, embeddings"
        ),
        catalog="65 entries (incl. aliases) across ~8 providers; frozen since Feb 2026; no video",
        routing="Manual model selection; no documented fallback, retries, or load balancing",
        observability="Usage logs for billing; no analytics or observability features documented",
        content=(
            "Transient pass-through; temporary cache for Responses state; no training on content"
        ),
        verification="Readable MIT source; no certifications, trust page, DPA, or named legal entity",
        billing="0% markup; 4.8% + $0.35 per top-up; credits expire in 1 year; BYOK free",
        sources=(
            ComparisonSource(
                "LMRouter GitHub repo (MIT license, commit history)",
                "https://github.com/LMRouter/lmrouter",
            ),
            ComparisonSource(
                "LMRouter pricing (fees, credit expiry)", "https://docs.lmrouter.com/pricing"
            ),
            ComparisonSource("LMRouter privacy policy", "https://docs.lmrouter.com/privacy"),
            ComparisonSource("LMRouter terms of service", "https://docs.lmrouter.com/terms"),
            ComparisonSource(
                "LMRouter live model list (65 entries, 2026-08-16)",
                "https://api.lmrouter.com/openai/v1/models",
            ),
            ComparisonSource(
                "TrustedRouter attestation endpoints", "https://trust.trustedrouter.com"
            ),
        ),
        faq_items=(
            (
                "Is LMRouter more open than TrustedRouter?",
                """Yes, in license terms. The entire LMRouter router is MIT-licensed and self-hostable with a documented local setup. Our gateway and control plane are source-available under BUSL-1.1 (each release converts to Apache-2.0 after four years); only our SDKs and integrations are Apache-2.0 or MIT. What we offer instead is verifiability of the hosted service: live attestation on three clouds that the running gateway matches the published source. If your requirement is to run the router yourself under an OSI license, LMRouter wins that point.""",
            ),
            (
                "Which service costs less in fees?",
                """The fees sit on different bases. LMRouter adds 0% to inference and charges 4.8% + $0.35 when you buy credits; unused credits expire after one year and may be removed without prior notice. TrustedRouter has no monthly plan or seat fees and bills prepaid inference at the provider's token price + 5.5% with a $0.01/M floor. For top-ups of $50 or more, LMRouter's total fee load is lower (4.8% + $0.35 works out below 5.5%); for smaller top-ups the flat $0.35 tips its effective fee above ours. Its BYOK passthrough is free, while we do not publish a separate BYOK fee. What you are paying our 5.5% for is failover, privacy-tier routing, current models, and the attested prompt path.""",
            ),
            (
                "Can I get recent models through LMRouter?",
                (
                    "Not through the hosted service today. Its model-catalog repo's last "
                    "commit was February 7, 2026, and the live API listed 65 entries "
                    "across ~8 providers on August 16, 2026 — models released after early "
                    "February 2026 are absent. Self-hosters can add models themselves via "
                    "LMRouter's per-model YAML configs. TrustedRouter's catalog listed "
                    "561 entries (including our meta routes) across 49 providers on the "
                    "same date, with per-model prices on /models."
                ),
            ),
        ),
        article_html="""<h2>What LMRouter is</h2><p>LMRouter is an MIT-licensed AI API router written in TypeScript (Hono on Node.js), created in July 2025. It ships in two forms: a self-hostable server you clone and configure with your own provider keys, and a hosted service at <span class="mono">api.lmrouter.com</span> with a unified key and prepaid credits. The API surface is wide for a small project: OpenAI-compatible Chat Completions, Responses, image generation and editing, audio, and embeddings, plus an Anthropic-compatible Messages endpoint. The <a href="https://github.com/LMRouter/lmrouter">GitHub repository</a> carries an MIT license covering the whole router, server included.</p><p>It is effectively a one-person project. A single author wrote 146 of the repo's 148 commits, and the <a href="https://docs.lmrouter.com/terms">terms of service</a> name "LMRouter Contributors" as the provider under California law. No company entity, address, or registration is disclosed anywhere on the site or docs.</p><h2>Where LMRouter genuinely wins</h2><p>Three things deserve plain credit. First, openness: LMRouter is OSI open source and self-hostable. Our gateway and control plane are source-available under BUSL-1.1, which is a weaker openness claim. If your requirement is "fork it and run it under MIT," LMRouter meets it and we do not.</p><p>Second, fees. LMRouter adds no markup to model prices ("We do not mark up the price of the models you use"), charges 4.8% + $0.35 only when you buy credits, and passes BYOK traffic through free (<a href="https://docs.lmrouter.com/pricing">pricing</a>). That is one of the cleanest fee structures in this category.</p><p>Third, a plainly written privacy policy: prompt content is "a transient pass-through," never used for training, and not stored long-term except temporary caching for stateful features such as Responses-API conversation state (<a href="https://docs.lmrouter.com/privacy">privacy policy</a>).</p><h2>The state of the project, with dates</h2><p>The hosted API works: on August 16, 2026, <span class="mono">GET /openai/v1/models</span> returned 200 with 65 model entries. Development has been dormant:</p><ul><li>The main repo's last push was September 23, 2025 — about eleven months before this page's verification date.</li><li>The model-catalog repo's last commit was February 7, 2026. Models released after early February 2026 are not in the hosted catalog.</li><li>No GitHub release has ever been published.</li><li>The live catalog's 65 entries (including duplicate aliases such as <span class="mono">claude-3-5-haiku-20241022</span> and <span class="mono">anthropic/claude-3.5-haiku</span>) span about 8 providers. The homepage says "hundreds of AI models"; the live list does not support that. There are no video models — the README marks video as coming soon.</li></ul><p>None of this proves the service will vanish. It does mean an evaluator should price in continuity risk: one maintainer, no legal entity, no releases, a catalog frozen for six months, and prepaid credits that expire after one year and "may be removed from your account without prior notice."</p><h2>Three differences that decide this comparison</h2><h3>Routing logic vs. an aggregator</h3><p>LMRouter is an aggregator: you pick a model ID and it forwards the call. No fallbacks, load balancing, retries, or cost-aware routing appear anywhere in its 22-page docs tree. TrustedRouter routes: provider failover and fallback model lists are built into the routing layer, and meta ids select candidate pools — <span class="mono">trustedrouter/auto</span>, <span class="mono">cheap</span>, and <span class="mono">fast</span>, plus privacy-tier routes. <span class="mono">trustedrouter/zdr</span> restricts to providers with contractual zero-data-retention postures; <span class="mono">trustedrouter/e2e</span> restricts to confidential-compute providers with provider-side end-to-end encryption (currently Tinfoil and Phala); <span class="mono">trustedrouter/eu</span> pins EU-focused providers. Privacy and jurisdiction preferences compose per request. The full list is on <a href="/models">/models</a>.</p><h3>Verification: readable source vs. attested running build</h3><p>LMRouter's trust model is that you can read the source and, if you self-host, run it yourself. That is real. When you use the hosted service, though, nothing demonstrates that the code at api.lmrouter.com is the code on GitHub, and there are no published certifications, no trust page, and no DPA. TrustedRouter's hosted gateway publishes hardware attestation on three clouds — GCP Confidential Space, AWS Nitro Enclaves, and Azure Confidential Containers — with live endpoints anyone can use to verify that the serving build matches published source and release digests: <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a> and <a href="/security">/security</a>. Realtime inference keeps no durable prompt or output logs; operational metadata (ids, model, tokens, latency, cost, region, key hash) is retained.</p><h3>Catalog currency and operations</h3><p>As of August 16, 2026, TrustedRouter's catalog lists 561 model entries (a count that includes our own meta ids) across 49 upstream providers, with exact prices on <a href="/models">/models</a>. Our status page publishes a 99.99% availability target with live error-budget burn rates on 5-minute to 24-hour windows, and we freeze monthly benchmark reports — July 2026 covered 274,753 samples across 44 providers and 353 models (<a href="/benchmarks/reports">/benchmarks/reports</a>). LMRouter documents usage logs for billing and no analytics or observability features.</p><h2>Fees, on their actual bases</h2><p>The two fee models sit on different bases, so comparing the percentages directly is wrong. LMRouter charges 4.8% + $0.35 per credit purchase and 0% on inference. TrustedRouter's fee sits on inference: prepaid usage bills at the provider's token price + 5.5%, with a $0.01 per million token floor — a $1.00/M provider price becomes $1.055/M (<a href="/pricing">/pricing</a>). For top-ups of $50 or more LMRouter's total fee load is lower; below $50 the flat $0.35 makes its effective fee the higher one. LMRouter's BYOK passthrough is free; we support BYOK but do not publish a separate BYOK fee. The offsetting LMRouter caveat is the one-year credit expiry.</p><h2>What we do not claim</h2><ul><li>Our attestation covers the gateway build, not downstream model providers. Providers are covered by cited policy and contract tiers; the <span class="mono">e2e</span> routes add provider-side confidential compute, and those are the provider's mechanisms, not our attestation.</li><li>Our gateway is not open source. It is source-available (BUSL-1.1, converting to Apache-2.0 four years after each release); the SDKs are Apache-2.0 or MIT. Every line that touches your prompt is public and inspectable.</li><li>We are young: our public repos date from late April 2026 and our benchmark history starts June 2026. LMRouter's repo is nine months older, though inactive.</li><li>We publish no SOC 2 or HIPAA certification today. Neither does LMRouter.</li></ul>""",
    ),
)


COMPETITOR_COMPARISON_BY_SLUG: dict[str, CompetitorComparison] = {
    comparison.slug: comparison for comparison in COMPETITOR_COMPARISONS
}


def competitor_comparison(slug: str) -> CompetitorComparison | None:
    return COMPETITOR_COMPARISON_BY_SLUG.get(slug.strip().lower())


def related_comparisons(
    comparison: CompetitorComparison,
    *,
    limit: int = 4,
) -> tuple[CompetitorComparison, ...]:
    same_category = [
        row
        for row in COMPETITOR_COMPARISONS
        if row.slug != comparison.slug and row.category == comparison.category
    ]
    others = [
        row
        for row in COMPETITOR_COMPARISONS
        if row.slug != comparison.slug and row.category != comparison.category
    ]
    return tuple((same_category + others)[:limit])
