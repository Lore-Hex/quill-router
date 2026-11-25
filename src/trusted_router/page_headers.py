"""Short page header for public pages on the shared marketing hero.

Each page gets one brand-kit line icon beside its title, chosen by page
family, and may carry a shorter sentence for phones. The full description
stays in the page for every screen size; phones show the short one.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from markupsafe import Markup

# Drawings from the brand kit icon set (24px grid, 1.5px stroke). Strokes use
# currentColor so the icon is Cream on the Ink header.
_ICON_PATHS: dict[str, str] = {
    "attested": (
        '<path d="M4 8.5 L4 4 L8.5 4"/><path d="M15.5 4 L20 4 L20 8.5"/>'
        '<path d="M20 15.5 L20 20 L15.5 20"/><path d="M8.5 20 L4 20 L4 15.5"/>'
        '<path d="M8 12.25 L10.75 15 L16 9.5"/>'
    ),
    "price": (
        '<path d="M10.5 3 L21 3 L21 13.5 L12 22 L2 12 L8.5 5"/>'
        '<rect x="15" y="7" width="2.5" height="2.5" fill="currentColor" stroke="none"/>'
    ),
    "region": (
        '<path d="M18.37 16.84 A8 8 0 0 1 5.63 16.84 M4.44 14.63 A8 8 0 0 1 4.44 9.37 '
        'M5.63 7.16 A8 8 0 0 1 18.37 7.16 M19.56 9.37 A8 8 0 0 1 19.56 14.63"/>'
        '<path d="M15.25 12 A3.25 8 0 1 1 8.75 12 A3.25 8 0 1 1 15.25 12"/>'
        '<path d="M2 8 L22 8"/><path d="M2 16 L22 16"/>'
    ),
    "document": (
        '<path d="M13 4 L5 4 L5 20 L19 20 L19 10"/><path d="M14.5 4 L19 8.5"/>'
        '<path d="M8 12 L16 12"/><path d="M8 16 L13 16"/>'
    ),
    "developer": (
        '<path d="M8 6.5 L3.25 11.25"/><path d="M3.25 12.75 L8 17.5"/>'
        '<path d="M16 6.5 L20.75 11.25"/><path d="M20.75 12.75 L16 17.5"/>'
        '<path d="M14 4 L10 20"/>'
    ),
    "route": (
        '<g stroke-linecap="round" stroke-linejoin="round"><path d="M3 12H8.5M8.5 12C12 12 '
        '12.5 6 16 6H21M8.5 12H21M8.5 12C12 12 12.5 18 16 18H21"/></g>'
    ),
    "people": (
        '<path d="M6.75 10.53 A3.5 3.5 0 1 1 10.25 10.53"/>'
        '<path d="M2.5 20.5 C2.5 16.6 5.2 13.5 8.5 13.5 C11.8 13.5 14.5 16.6 14.5 20.5"/>'
        '<path d="M15.54 10.83 A2.75 2.75 0 1 1 18.46 10.83"/>'
        '<path d="M16.25 13.6 C16.5 13.55 16.75 13.5 17 13.5 C19.5 13.5 21.5 16 21.5 19.25"/>'
    ),
    "status": '<path d="M2 12 L6.5 12 L9 6 L12.5 18 L15 9 L16.5 12 L22 12"/>',
    "model": (
        '<g stroke-linecap="round" stroke-linejoin="round"><path d="M12 3L20 7.5V16.5L12 '
        '21L4 16.5V7.5Z M4 7.5L12 12L20 7.5M12 12V21"/></g>'
    ),
    "receipt": (
        '<path d="M2 4 L22 4"/><path d="M5 6 L5 21 L7.33 19.5 L9.67 21 L12 19.5 L14.33 21 '
        'L16.67 19.5 L19 21 L19 6"/><path d="M8 9 L16 9"/><path d="M8 12.5 L16 12.5"/>'
        '<path d="M8 16 L12 16"/>'
    ),
}

# First match wins. Words are matched against the page path.
_ICON_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("document", ("legal", "terms", "privacy", "subprocessors", "telemetry")),
    ("receipt", ("receipts",)),
    ("price", ("pricing", "x402", "spend", "credits", "billing")),
    (
        "region",
        ("eu", "residency", "us-ai-models", "china", "chinese", "eu-ai-models"),
    ),
    (
        "attested",
        (
            "security",
            "trust",
            "confidential",
            "retention",
            "log",
            "private",
            "hipaa",
            "gdpr",
            "anonymous",
            "compliance",
            "encrypted",
            "conformance",
        ),
    ),
    ("status", ("status", "latency", "benchmarks", "performance", "failover", "evals")),
    ("people", ("about", "careers", "contact", "support", "sms", "notify")),
    (
        "model",
        ("models", "model", "glm", "kimi", "minimax", "gpt-oss", "fusion", "video"),
    ),
    (
        "developer",
        (
            "docs",
            "api",
            "mcp",
            "agent",
            "cline",
            "sillytavern",
            "apps",
            "batch",
            "caching",
            "tagging",
            "web-search",
            "sign-in",
        ),
    ),
)

# Shorter header sentences for phones, by page path. A page with no entry
# shows its full description on every screen size.
SHORT_LINES: dict[str, str] = {
    "docs/claude-code": "One key, Claude models, and per-key spend caps.",
    "docs/opencode": "Connect a key, choose a model, and check the bill.",
    "llm-gateway-pricing": "Compare fees and reproduce a monthly bill.",
    "docs/opencode-deepseek": "Three routes, with keys and model selectors.",
    "docs/cut-llm-api-costs": "Change the model, measure the saving.",
    "abliterated-deepseek": "Abliterated DeepSeek for cyberdefense via Maple's Confidential API.",
    "about": "The company, its legal identity, and checkable trust evidence.",
    "anonymous-llm-api": "Pay for 600+ models with crypto by x402 or MetaMask sign-in.",
    "apps": "Apps routing through TrustedRouter can self-identify and appear here.",
    "aws-bedrock-alternative": "Claude and 600+ models through a hardware-attested TEE gateway.",
    "azure-openai-alternative": "Hardware-attested inference with automatic provider fallback.",
    "benchmarks": "Benchmark scores, cited sources, and provider route measurements.",
    "benchmarks/reports": "Monthly reports of measured availability, speed, and throughput.",
    "best-llm-router": "Choose an AI router by cost, failover, model choice and privacy.",
    "careers": "Work on attested AI routing, evaluations, and infrastructure.",
    "china-ai-models": "Chinese-lab models and the providers that serve each one.",
    "chinese-ai-models-us-hosted": "Qwen, GLM, Kimi, and DeepSeek on US-hosted attested infrastructure.",
    "claude-api-privacy": "Call Claude through a hardware-attested, open-source router.",
    "cline-api-provider": "Point Cline or Roo Code at 600+ models with automatic fallback.",
    "compare": "TrustedRouter beside marketplaces, gateways, and cloud platforms.",
    "compare/litellm": "Compare hosted or self-managed routing, failover, and privacy.",
    "compare/models": "Compare context, routes, pricing, privacy, and performance.",
    "compare/openrouter": "Compare API compatibility, routing, privacy controls, and pricing.",
    "compare/vercel-ai-gateway": "Compare model routing, SDK support, observability, and failover.",
    "confidential-computing-llm": "LLM routing in GCP Confidential Space with remote attestation.",
    "contact": "Reach Lore Hex Corp for product, support, security, and billing.",
    "deepseek-api-privacy": "DeepSeek V4 Pro and V4 Flash through an attested gateway.",
    "docs": "Quickstarts, Python and TypeScript SDKs, and API reference.",
    "docs/agent-setup": "Set the base URL for Claude Code, Codex, Cursor, and SDK agents.",
    "docs/migrate-from-openai": "Change the base URL and key. Keep the OpenAI SDK.",
    "free-llm-api": "Up to 1 million free tokens. No card required.",
    "docs/batch": "Submit inline batches and poll encrypted results by batch ID.",
    "docs/byok": "List, create, update, and delete upstream provider keys.",
    "docs/decide": "Typed boolean, choice, and score questions with probabilities.",
    "docs/evals": "Run repeatable model evaluations and compare providers and cost.",
    "docs/mcp": "Connect coding agents to the live model catalog, credits, and docs.",
    "docs/migrate-from-openrouter": "Change base_url and keep your OpenAI-compatible clients.",
    "docs/model-precision": "Read per-provider quantization, model revisions, and evidence links.",
    "docs/notify": "Send a push, email, SMS, or phone call to your account owner.",
    "docs/performance-routing": "Configure latency and throughput preferences and fallback behavior.",
    "docs/prompt-caching": "Reuse stable prompt prefixes and inspect cached token usage.",
    "docs/provider-conformance": "Run the conformance suite before applying to the marketplace.",
    "docs/provider-routing": "Pin, prefer, or exclude providers per request.",
    "docs/receipts": "Opt into a signed, offline-verifiable receipt on any inference call.",
    "docs/spend-controls": "Create API keys with opt-in strict budget admission.",
    "docs/synth": "Run a panel of models and return one OpenAI-compatible answer.",
    "docs/tagging": "Attach AWS style tags and attribution metadata to LLM requests.",
    "docs/telemetry": "What SDKs measure, metadata retention, and how to opt out.",
    "docs/video": "Generate video through the attested API with exact quoted billing.",
    "docs/web-search": "Web search tools with citations, source controls, and streaming.",
    "docs/x402": "Let agents add prepaid credits with Stripe x402.",
    "eu": "The Europe West gateway base URL with data-residency controls.",
    "eu-ai-act-llm-compliance": "What deployers building on LLM APIs need from their vendor.",
    "eu-ai-models": "Models built by EU labs and providers operated from EU member states.",
    "gdpr-compliant-llm-api": "Built for GDPR workflows, with a signable DPA and an EU gateway.",
    "gemini-flash-alternative": "Compare Gemini Flash with low-cost open and proprietary alternatives.",
    "glm-5-api": "GLM-5 and GLM-5.2 on attested hardware, with ZDR and TEE tiers.",
    "gpt-oss-120b-api": "A Cerebras-served fast route with TEE-attested privacy tiers.",
    "groq-alternative": "600+ models, measured fast routes, and automatic failover.",
    "hipaa-llm-api": "An auditable LLM API for HIPAA covered entities.",
    "kimi-k2-api": "Kimi K2.7 Code and Kimi K2.6 with multiple provider routes.",
    "latest-model-apis": "Kimi K3, GLM 5.2, DeepSeek V4, and Gemini 3.6 Flash through one API.",
    "legal": "Operating entity, DPA, BAA, subprocessors, and security controls.",
    "legal/baa": "Draft Business Associate Agreement for HIPAA workloads.",
    "legal/dpa": "Draft DPA terms for customer counsel review.",
    "legal/hipaa-readiness": "Safeguards, risk analysis, incident duties, and BAA requirements.",
    "legal/soc2-readiness": "Readiness documentation and target scope for SOC 2 Type II.",
    "legal/subprocessors": "Platform vendors and downstream model providers, by purpose.",
    "litellm-alternative": "Hosted or self-managed, with provider failover and open source code.",
    "llm-api-for-financial-services": "Attested confidential compute and docs for DORA reviews.",
    "llm-api-for-law-firms": "Confidential compute and attestation your team can verify live.",
    "llm-data-residency": "EU gateway routing, zero-retention tiers, and an attested gateway.",
    "llm-document-processing": "Run contracts, medical records, and invoices with attested privacy.",
    "llm-failover": "Requests roll over when a provider is down.",
    "llm-provider-latency-benchmarks": "Measured time-to-first-token, throughput, uptime, and success rates.",
    "llm-zero-data-retention": "Zero data retention as a structural property of the code.",
    "minimax-m3-api": "MiniMax M3 on attested, verifiable infrastructure.",
    "model-precision": "Compare reviewed model weight formats by provider.",
    "new-york-token-exchange": "Compare AI models, pricing, privacy, and inference providers.",
    "no-log-llm-api": "Content-stateless real-time inference on attested TEE routes.",
    "openai-compatible-llm-api": "Keep the OpenAI SDK, change base_url once, and call 600+ models.",
    "performance-routing": "Prefer routes by time to first token and output speed.",
    "portkey-alternative": "Usage metering that is verifiable in source.",
    "privacy": "How Lore Hex Corp collects, uses, and protects your information.",
    "private-llm-api": "Cryptographically verifiable privacy through an attested gateway.",
    "prompt-caching": "Reuse provider prompt caches and inspect cached tokens and cost.",
    "providers": "Compare AI providers by coverage, pricing, privacy, and uptime.",
    "providers/marketplace": "Start with one model on confidential compute we can verify.",
    "rankings": "Rank AI models by price, privacy, context, and performance.",
    "resources": "Guides, comparisons, API quickstarts, SDKs, and model benchmarks.",
    "security": "Security boundaries, live attestation, and prompt handling.",
    "sign-in-with-trustedrouter": "Users bring their own account and choose a per-app spend cap.",
    "sillytavern-api": "A hardware-attested backend with crypto pay-per-request.",
    "sms": "How SMS alerts and one-time verification codes work.",
    "spend-controls": "Set daily, weekly, monthly and lifetime API key budgets.",
    "support": "Product, account, API, billing, and security support.",
    "terms": "The terms governing TrustedRouter services, accounts, and billing.",
    "tinfoil-alternative": "An attested gateway across 100+ providers with one API.",
    "us-ai-models": "Open-weight models from US labs and US-operated providers.",
    "vertex-ai-alternative": "600+ models, Gemini included, with one key and automatic fallback.",
    "x402-llm-api": "Your agent gets a 402, signs a payment, and retries.",
}


# Shorter titles for phones, for the pages whose full title would have to
# shrink too far to fit. The full title stays the page's h1 text.
SHORT_TITLES: dict[str, str] = {
    "abliterated-deepseek": "Abliterated DeepSeek API",
    "anonymous-llm-api": "Anonymous LLM API",
    "aws-bedrock-alternative": "AWS Bedrock Alternative",
    "azure-openai-alternative": "Azure OpenAI Alternative",
    "best-llm-router": "Best AI Router & LLM Gateway 2026",
    "china-ai-models": "Chinese AI Models",
    "chinese-ai-models-us-hosted": "Qwen, GLM, Kimi & DeepSeek APIs",
    "cline-api-provider": "Use with Cline & Roo Code",
    "docs/decide": "Decision Models API",
    "eu-ai-act-llm-compliance": "EU AI Act Compliance for LLM APIs",
    "gdpr-compliant-llm-api": "GDPR-Compliant LLM API",
    "glm-5-api": "GLM-5 & GLM-5.2 API",
    "gpt-oss-120b-api": "gpt-oss-120b API",
    "groq-alternative": "Groq Alternative",
    "latest-model-apis": "Latest AI Model APIs",
    "llm-api-for-financial-services": "LLM API for Financial Services",
    "llm-api-for-law-firms": "LLM API for Law Firms",
    "llm-data-residency": "LLM Data Residency",
    "llm-document-processing": "LLM API for Document Processing",
    "llm-failover": "LLM Failover & Fallback API",
    "llm-zero-data-retention": "Zero Data Retention LLM API",
    "minimax-m3-api": "MiniMax M3 API",
    "no-log-llm-api": "No-Log LLM API",
    "portkey-alternative": "Portkey Alternative",
    "private-llm-api": "Private LLM API",
    "sign-in-with-trustedrouter": "Sign in with TrustedRouter",
    "sillytavern-api": "Private SillyTavern API Backend",
    "tinfoil-alternative": "Tinfoil Alternative",
    "vertex-ai-alternative": "Vertex AI Alternative",
}


def _path(url_or_path: str) -> str:
    path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path
    return path.strip("/")


def _icon_name(path: str) -> str:
    words = set(path.replace("/", "-").split("-"))
    for name, keys in _ICON_RULES:
        for key in keys:
            if key in words or (("-" in key) and key in path):
                return name
    return "route"


def page_header_icon(url_or_path: str) -> Markup:
    name = _icon_name(_path(url_or_path))
    # Built only from the constants above, never from request input.
    return Markup(  # noqa: S704
        '<svg class="page-header-icon" viewBox="0 0 24 24" width="48" height="48" '
        'aria-hidden="true" focusable="false" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="butt" stroke-linejoin="miter" '
        f'stroke-miterlimit="10" data-icon="{name}">{_ICON_PATHS[name]}</svg>'
    )


def page_header_short_line(url_or_path: str) -> str | None:
    return SHORT_LINES.get(_path(url_or_path))


def page_header_title_size(heading: str, phone: bool = False) -> str:
    """Size step for the header title, by its length, so a long title shrinks
    to fit instead of stacking up. Phones have less room, so their largest
    step is kept for the shortest titles."""
    length = len(heading)
    if length <= (14 if phone else 20):
        return "s"
    if length <= 34:
        return "m"
    if length <= 48:
        return "l"
    return "xl"


def page_header_short_title(url_or_path: str) -> str | None:
    return SHORT_TITLES.get(_path(url_or_path))
