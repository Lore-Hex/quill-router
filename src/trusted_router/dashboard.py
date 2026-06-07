"""Dashboard rendering. The page itself lives in templates/dashboard.html
with HTML/CSS/JS in their own files; this module only resolves
settings-driven values and renders the Jinja2 template."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import cast
from xml.sax.saxutils import escape as xml_escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from trusted_router.benchmark_scores import scores_for_model
from trusted_router.catalog import (
    META_MODEL_IDS,
    MODELS,
    PROVIDERS,
    Model,
    ModelEndpoint,
    Provider,
    endpoints_for_model,
    meta_candidate_models,
    providers_for_display,
)
from trusted_router.config import Settings
from trusted_router.legal import (
    hipaa_readiness_packet,
    legal_entity,
    procurement_packet,
    provider_subprocessor_rows,
    soc2_readiness_packet,
    subprocessor_packet,
)
from trusted_router.measured import measured_for_model, measured_for_provider
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.og import OG_DESCRIPTION, OG_IMAGE_HEIGHT, OG_IMAGE_WIDTH, OG_TITLE
from trusted_router.regions import configured_regions, region_map_payload

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
DEV_USER_FALLBACK = "alpha@trustedrouter.local"
MODEL_SEO_SECTIONS: tuple[str, ...] = (
    "benchmarks",
    "providers",
    "performance",
    "pricing",
    "uptime",
    "api",
)
MODEL_SEO_SECTION_LABELS: dict[str, str] = {
    "benchmarks": "Benchmarks",
    "providers": "Providers",
    "performance": "Performance",
    "pricing": "Pricing",
    "uptime": "Uptime",
    "api": "API",
}
MODEL_COMPARE_URL_LIMIT = 2_600
MODEL_COMPARE_MODEL_LIMIT = 73
SEO_CORE_PATHS: tuple[str, ...] = (
    "/",
    "/models",
    "/providers",
    "/benchmarks",
    "/rankings",
    "/leaderboard",
    "/status",
    "/security",
    "/legal",
    "/legal/dpa",
    "/legal/baa",
    "/legal/soc2-readiness",
    "/legal/hipaa-readiness",
    "/legal/subprocessors",
    "/chat",
    "/compare/openrouter",
    "/compare/vercel-ai-gateway",
    "/compare/litellm",
    # SEO landing pages — each targets a high-intent buyer query.
    "/openrouter-alternative",
    "/private-llm-api",
    "/hipaa-llm-api",
    "/llm-zero-data-retention",
    "/claude-api-privacy",
    "/litellm-alternative",
    "/portkey-alternative",
    "/confidential-computing-llm",
    "/tinfoil-alternative",
    "/sign-in-with-trustedrouter",
    "/pricing",
    "/docs",
    "/apps",
    "/docs/agent-setup",
    "/docs/evals",
    "/docs/migrate-from-openrouter",
    "/docs/llms.txt",
    "/docs/llms-full.txt",
)
_BENCHMARK_INDEX_LINKS: tuple[dict[str, str], ...] = (
    {
        "label": "LMArena leaderboard",
        "href": "https://arena.ai/leaderboard",
        "kind": "Independent benchmark index",
    },
    {
        "label": "LiveBench",
        "href": "https://livebench.ai/",
        "kind": "Independent benchmark index",
    },
    {
        "label": "Artificial Analysis models",
        "href": "https://artificialanalysis.ai/models",
        "kind": "Independent benchmark index",
    },
    {
        "label": "HELM",
        "href": "https://crfm.stanford.edu/helm/",
        "kind": "Independent benchmark index",
    },
)
_PROVIDER_MODEL_INFO_LINKS: dict[str, tuple[dict[str, str], ...]] = {
    "anthropic": (
        {
            "label": "Anthropic model docs",
            "href": "https://platform.claude.com/docs/en/about-claude/models/overview",
            "kind": "Official model information",
        },
    ),
    "openai": (
        {
            "label": "OpenAI model docs",
            "href": "https://developers.openai.com/api/docs/models",
            "kind": "Official model information",
        },
    ),
    "gemini": (
        {
            "label": "Gemini model docs",
            "href": "https://ai.google.dev/gemini-api/docs/models",
            "kind": "Official model information",
        },
    ),
    "mistral": (
        {
            "label": "Mistral model docs",
            "href": "https://docs.mistral.ai/models/overview",
            "kind": "Official model information",
        },
    ),
    "deepseek": (
        {
            "label": "DeepSeek API docs",
            "href": "https://api-docs.deepseek.com/",
            "kind": "Official model information",
        },
    ),
    "kimi": (
        {
            "label": "Kimi API docs",
            "href": "https://platform.kimi.ai/docs/overview",
            "kind": "Official model information",
        },
    ),
    "zai": (
        {
            "label": "Z.AI model docs",
            "href": "https://docs.z.ai/guides/overview/quick-start",
            "kind": "Official model information",
        },
    ),
    "minimax": (
        {
            "label": "MiniMax model docs",
            "href": "https://platform.minimaxi.com/document/guides/chat-model/V2",
            "kind": "Official model information",
        },
    ),
    "grok": (
        {
            "label": "xAI model docs",
            "href": "https://docs.x.ai/developers/models",
            "kind": "Official model information",
        },
    ),
    "together": (
        {
            "label": "Together model reference",
            "href": "https://docs.together.ai/docs/serverless/models",
            "kind": "Official provider catalog",
        },
    ),
}
_MODEL_SPECIFIC_BENCHMARK_LINKS: dict[str, tuple[dict[str, str], ...]] = {
    "minimax/minimax-m3": (
        {
            "label": "MiniMax M3 model page",
            "href": "https://www.minimax.io/models/text/m3",
            "kind": "Official model information",
        },
        {
            "label": "MiniMax M3 release notes",
            "href": "https://www.minimax.io/blog/minimax-m3",
            "kind": "Official model information",
        },
        {
            "label": "BenchLM MiniMax M3",
            "href": "https://benchlm.ai/models/minimax-m3",
            "kind": "Independent benchmark page",
        },
    ),
}


@dataclass(frozen=True)
class PublicPage:
    template: str
    title: str
    description: str
    # Optional per-page social card filename under /static/og/. When set,
    # link unfurls use that tailored 1200x630 image instead of the default
    # /og.png. Generate the files per docs/marketing/og-card-spec.md.
    og_card: str | None = None


PUBLIC_PAGES: dict[str, PublicPage] = {
    "compare/openrouter": PublicPage(
        template="public/compare_openrouter.html",
        title="TrustedRouter Compared With OpenRouter",
        description="Keep the same API shape and add a verifiable prompt path.",
    ),
    "compare/vercel-ai-gateway": PublicPage(
        template="public/compare_vercel_ai_gateway.html",
        title="TrustedRouter And Vercel AI Gateway",
        description=(
            "Vercel AI Gateway is a strong developer gateway. "
            "TrustedRouter adds an open source attested prompt path."
        ),
    ),
    "compare/litellm": PublicPage(
        template="public/compare_litellm.html",
        title="TrustedRouter And LiteLLM",
        description="Use LiteLLM when you want to run the router yourself. Use TrustedRouter when you want hosted attestation.",
    ),
    "docs/migrate-from-openrouter": PublicPage(
        template="public/migrate_from_openrouter.html",
        title="Migrate From OpenRouter",
        description="Change base_url, keep OpenAI compatible clients, and verify the hosted gateway.",
    ),
    "docs/agent-setup": PublicPage(
        template="public/agent_setup.html",
        title="Agent Setup For TrustedRouter",
        description="Base URLs, env vars, smoke tests, and model aliases for coding agents.",
    ),
    "docs/evals": PublicPage(
        template="public/evals.html",
        title="TrustedRouter Evals Guide",
        description="Run model, provider, privacy, latency, and cost evals through one OpenAI compatible API.",
    ),
    "security": PublicPage(
        template="public/security.html",
        title="Security",
        description="What is logged, what is not logged, and where prompt traffic belongs.",
    ),
    # SEO landing pages — top-level slugs target high-intent buyer
    # queries. Each one is a self-contained sales surface: H2 above the
    # fold, one runnable code sample, one comparison table, a clear
    # CTA to /chat. Internal-link target for the marketing-grid cards
    # on /, /compare/openrouter, and the related landing pages.
    "openrouter-alternative": PublicPage(
        template="public/seo_openrouter_alternative.html",
        og_card="openrouter-alternative.png",
        title="OpenRouter Alternative — TrustedRouter",
        description=(
            "An open-source, hardware-attested OpenRouter alternative. "
            "Same OpenAI-compatible API, verifiable prompt path, no logs."
        ),
    ),
    "private-llm-api": PublicPage(
        template="public/seo_private_llm_api.html",
        og_card="private-llm-api.png",
        title="Private LLM API — Verifiable, Attested, Open Source",
        description=(
            "A private LLM API where privacy is cryptographically verifiable. "
            "Route to Claude, GPT, Gemini, DeepSeek through an attested gateway."
        ),
    ),
    "hipaa-llm-api": PublicPage(
        template="public/seo_hipaa_llm_api.html",
        og_card="hipaa-llm-api.png",
        title="HIPAA-Compatible LLM Routing — TrustedRouter",
        description=(
            "An auditable LLM API for HIPAA covered entities. "
            "Attested gateway, open-source routing code, no prompt logs by construction."
        ),
    ),
    "llm-zero-data-retention": PublicPage(
        template="public/seo_zero_data_retention.html",
        og_card="llm-zero-data-retention.png",
        title="Zero Data Retention LLM API — Verifiable in Source",
        description=(
            "Zero data retention as a structural property of the open-source code, "
            "not just a contract clause. Multi-provider routing with the same posture."
        ),
    ),
    "claude-api-privacy": PublicPage(
        template="public/seo_claude_api_privacy.html",
        og_card="claude-api-privacy.png",
        title="Claude API Privacy — Through TrustedRouter",
        description=(
            "Call Anthropic Claude through a hardware-attested, open-source router. "
            "Anthropic's privacy posture plus a routing path you can verify."
        ),
    ),
    # Competitor-alternative + category SEO pages (round 2).
    "litellm-alternative": PublicPage(
        template="public/seo_litellm_alternative.html",
        og_card="litellm-alternative.png",
        title="LiteLLM Alternative — Self-Host and Verify It",
        description=(
            "A LiteLLM alternative that's self-hostable AND verifiable. "
            "Hardware-attested gateway proves the no-logging guarantee."
        ),
    ),
    "portkey-alternative": PublicPage(
        template="public/seo_portkey_alternative.html",
        og_card="portkey-alternative.png",
        title="Portkey Alternative — Routing Without Logging Every Prompt",
        description=(
            "A Portkey alternative for teams that can't store prompt content. "
            "Usage metering without content logs, verifiable in source."
        ),
    ),
    "confidential-computing-llm": PublicPage(
        template="public/seo_confidential_computing_llm.html",
        og_card="confidential-computing-llm.png",
        title="Confidential Computing for LLMs — TrustedRouter",
        description=(
            "Run LLM inference behind hardware attestation across every provider. "
            "AWS Nitro Enclaves and GCP Confidential VMs, with remote attestation."
        ),
    ),
    "tinfoil-alternative": PublicPage(
        template="public/seo_tinfoil_alternative.html",
        og_card="tinfoil-alternative.png",
        title="Tinfoil Alternative — Verifiable Privacy, Every Provider",
        description=(
            "Same verifiable-privacy bet as Tinfoil, applied as a router. "
            "Attested, no-log gateway across 30+ providers with one API."
        ),
    ),
    "sign-in-with-trustedrouter": PublicPage(
        template="public/seo_sign_in_with_trustedrouter.html",
        og_card="sign-in-with-trustedrouter.png",
        title="Sign in with TrustedRouter — Let Your Users Bring Their Own AI",
        description=(
            "Add a sign-in button and your users bring their own TrustedRouter "
            "account — instant access to hundreds of models, billed to them, "
            "through an attested no-log gateway. Integrate in minutes with the "
            "Python, TypeScript, or Swift SDK."
        ),
    ),
    "pricing": PublicPage(
        template="public/pricing.html",
        og_card="pricing.png",
        title="Pricing — Usage-Based, No Subscription",
        description=(
            "Prepaid credits, BYOK, or usage-based billing — pay the provider "
            "price plus a small routing margin, with no monthly plan. Per-model "
            "prices are published on the models page."
        ),
    ),
    "docs": PublicPage(
        template="public/docs.html",
        og_card="docs.png",
        title="Docs — Quickstart, SDKs, and API Reference",
        description=(
            "Point any OpenAI-compatible SDK at TrustedRouter with one base_url "
            "change. Guides, Python / TypeScript / Swift SDKs, and the "
            "OpenAI-compatible API reference."
        ),
    ),
    "apps": PublicPage(
        template="public/apps.html",
        og_card="apps.png",
        title="Apps — Built on TrustedRouter",
        description=(
            "Apps routing through TrustedRouter can self-identify and appear "
            "here. Opt-in by construction and privacy-safe: names and counts "
            "only, never prompts or keys."
        ),
    ),
}


def _format_uptime(value: float | None, decimals: int = 4) -> str:
    """Render an uptime percentage. Caps display at "99.99%" — claiming
    a literal 100.0000% with a few hundred probe samples behind it is
    overconfident; "99.99%+" reads honest, matches what
    status.anthropic.com / status.github.com surface, and stops the eye
    from interpreting "100%" as a guarantee.

    Threshold is `>= 99.995` so the value rounds to 100 at 4 decimals
    of precision; anything that actually rounds below that shows its
    real number."""
    if value is None:
        return "n/a"
    if value >= 99.995:
        return ">99.99%"
    return f"{value:.{decimals}f}%"


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )
    env.filters["uptime_pct"] = _format_uptime
    return env


def _static_version(settings: Settings) -> str:
    # In production this is the release tag (cache-friendly across requests).
    # In dev, fall back to the latest mtime of the static dir so every edit
    # invalidates the browser cache without a hard-reload.
    if settings.release and settings.release != "local":
        return settings.release
    static_dir = Path(__file__).parent / "static"
    try:
        mtime = max(p.stat().st_mtime for p in static_dir.iterdir() if p.is_file())
        return f"local-{int(mtime)}"
    except (OSError, ValueError):
        return "local"


def dashboard_html(settings: Settings) -> str:
    domain = settings.trusted_domain
    environment = settings.environment.lower()
    tr_config = {
        "environment": environment,
        "defaultDevUser": "" if environment == "production" else DEV_USER_FALLBACK,
        "apiBaseUrl": settings.api_base_url,
        "stablecoinCheckoutEnabled": settings.stablecoin_checkout_enabled,
        "paypalEnabled": settings.paypal_enabled,
        "googleEnabled": settings.google_oauth_enabled,
        "githubEnabled": settings.github_oauth_enabled,
    }
    map_regions = region_map_payload(settings)
    api_region_count = len(configured_regions(settings))
    return _env().get_template("dashboard.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{domain}/",
        og_image=f"https://{domain}/og.png",
        og_title=OG_TITLE,
        og_description=OG_DESCRIPTION,
        og_image_width=OG_IMAGE_WIDTH,
        og_image_height=OG_IMAGE_HEIGHT,
        tr_config=json.dumps(tr_config),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        paypal_enabled=settings.paypal_enabled,
        map_regions=map_regions,
        api_region_count=api_region_count,
        primary_region=settings.primary_region,
        static_version=_static_version(settings),
    )


def public_apps_html(settings: Settings, *, apps: dict[str, object]) -> str:
    """Render the /apps directory page with the cached app-usage snapshot.
    Reuses the PUBLIC_PAGES["apps"] metadata (title/description/OG) and injects
    the privacy-safe ranked app list (see trusted_router.apps.aggregate_apps)."""
    page = PUBLIC_PAGES["apps"]
    return _env().get_template(page.template).render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/apps",
        title=f"{page.title} | TrustedRouter",
        heading=page.title,
        description=page.description,
        og_image=_og_image_url(settings, page.og_card),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
        apps=apps,
    )


def _og_image_url(settings: Settings, og_card: str | None) -> str:
    """Resolve the social-card URL for a page. Returns the tailored card
    only when its PNG exists under static/og/; otherwise the default
    brand card. Lets us declare per-page cards before the images are
    generated without ever serving a 404 unfurl."""
    if og_card and (STATIC_DIR / "og" / og_card).is_file():
        return f"https://{settings.trusted_domain}/static/og/{og_card}"
    return f"https://{settings.trusted_domain}/og.png"


def public_page_html(settings: Settings, page_key: str) -> str:
    page = PUBLIC_PAGES[page_key]
    return _env().get_template(page.template).render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/{page_key}",
        title=f"{page.title} | TrustedRouter",
        heading=page.title,
        description=page.description,
        # Absolute, environment-correct card URL so link unfurls work in
        # staging/preview too. Uses the page's tailored card only once the
        # PNG actually exists on disk — so we can declare og_card now and
        # each card auto-activates the moment its image is generated into
        # static/og/, with zero risk of a 404 unfurl in the meantime.
        og_image=_og_image_url(settings, page.og_card),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_legal_html(settings: Settings) -> str:
    packet = procurement_packet(settings)
    return _env().get_template("public/legal.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/legal",
        title="Legal And Procurement Packet | TrustedRouter",
        heading="Legal and procurement packet",
        description=(
            "Read-only procurement packet for legal teams reviewing TrustedRouter for sensitive work."
        ),
        packet=packet,
        entity=legal_entity(settings),
        subprocessors=subprocessor_packet(),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_dpa_html(settings: Settings) -> str:
    return _env().get_template("public/legal_dpa.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/legal/dpa",
        title="DPA Draft | TrustedRouter",
        heading="Data Processing Addendum draft",
        description=(
            "Draft DPA terms for customer counsel review. Production legal workloads require a signed agreement or written exception."
        ),
        entity=legal_entity(settings),
        subprocessors=subprocessor_packet(),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_baa_html(settings: Settings) -> str:
    return _env().get_template("public/legal_baa.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/legal/baa",
        title="BAA Draft | TrustedRouter",
        heading="Business Associate Agreement draft",
        description=(
            "Draft BAA terms for HIPAA review. PHI workloads require a signed BAA and route restrictions."
        ),
        entity=legal_entity(settings),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_soc2_readiness_html(settings: Settings) -> str:
    packet = soc2_readiness_packet(settings)
    return _env().get_template("public/legal_soc2_readiness.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/legal/soc2-readiness",
        title="SOC 2 Readiness | TrustedRouter",
        heading="SOC 2 readiness",
        description=(
            "SOC 2 Type I readiness package for auditor and procurement review. No SOC 2 report has been obtained yet."
        ),
        entity=legal_entity(settings),
        packet=packet,
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_hipaa_readiness_html(settings: Settings) -> str:
    packet = hipaa_readiness_packet(settings)
    return _env().get_template("public/legal_hipaa_readiness.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/legal/hipaa-readiness",
        title="HIPAA Readiness | TrustedRouter",
        heading="HIPAA readiness",
        description=(
            "HIPAA readiness package for covered-entity and business-associate review. PHI requires a signed BAA."
        ),
        entity=legal_entity(settings),
        packet=packet,
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_subprocessors_html(settings: Settings) -> str:
    return _env().get_template("public/legal_subprocessors.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/legal/subprocessors",
        title="Subprocessors | TrustedRouter",
        heading="Subprocessors",
        description=(
            "Platform vendors and downstream model providers used by TrustedRouter."
        ),
        entity=legal_entity(settings),
        subprocessors=subprocessor_packet(),
        provider_subprocessors=provider_subprocessor_rows(),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def procurement_json(settings: Settings) -> str:
    return json.dumps(procurement_packet(settings), sort_keys=True, indent=2)


def soc2_readiness_json(settings: Settings) -> str:
    return json.dumps(soc2_readiness_packet(settings), sort_keys=True, indent=2)


def hipaa_readiness_json(settings: Settings) -> str:
    return json.dumps(hipaa_readiness_packet(settings), sort_keys=True, indent=2)


def subprocessors_json(settings: Settings) -> str:
    return json.dumps(subprocessor_packet(), sort_keys=True, indent=2)


def public_models_html(settings: Settings) -> str:
    return _env().get_template("public/models.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/models",
        title="Models | TrustedRouter",
        heading="Models",
        description="Hundreds of models with provider routes, prices, status, and policy notes.",
        models=[_model_view(model) for model in MODELS.values()],
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_benchmarks_html(settings: Settings) -> str:
    return _env().get_template("public/seo_index.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/benchmarks",
        title="Benchmarks | TrustedRouter",
        heading="Benchmarks",
        description=(
            "Model benchmark entry points, route measurements, and independent sources."
        ),
        page_kind="benchmarks",
        models=_seo_model_rows(),
        providers=[_provider_view(provider) for provider in providers_for_display()],
        benchmark_links=list(_BENCHMARK_INDEX_LINKS),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_leaderboard_html(settings: Settings, snapshot: dict[str, object]) -> str:
    """Render the public performance leaderboard from a precomputed snapshot.

    `snapshot` is the output of `aggregate_leaderboard()` plus a `generated_at`
    timestamp — built (and cached) by the route so this stays render-only.
    """
    return _env().get_template("public/leaderboard.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/leaderboard",
        title="LLM Provider & Model Speed Leaderboard | TrustedRouter",
        heading="Provider & model performance",
        description=(
            "Measured time-to-first-token, time-to-first-byte, throughput, and "
            "uptime for every LLM provider and model TrustedRouter routes to — "
            "continuously sampled, not vendor-claimed."
        ),
        page_kind="leaderboard",
        snapshot=snapshot,
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_rankings_html(settings: Settings) -> str:
    return _env().get_template("public/seo_index.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/rankings",
        title="Model Rankings | TrustedRouter",
        heading="Model Rankings",
        description=(
            "Rank models by route count, provider diversity, price, and policy posture."
        ),
        page_kind="rankings",
        models=_seo_model_rows(),
        providers=[_provider_view(provider) for provider in providers_for_display()],
        benchmark_links=list(_BENCHMARK_INDEX_LINKS),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_chat_html(settings: Settings) -> str:
    """Render the public chat playground at /chat.

    The page itself is auth-free — anyone can load it and explore the
    model picker. The Send button is gated client-side on the
    `tr_signed_in=1` companion cookie via the existing
    `hasSignedInHint()` JS in static/dashboard.js; signed-out clicks
    pop the marketing sign-in modal instead of firing any provider
    inference.

    See docs (plan file) for the full architecture.
    """
    return _env().get_template("public/chat.html").render(
        # CRITICAL: chat playground uses /chat-proxy/v1 (same-origin
        # streaming pipe in routes/chat_proxy.py) to forward to
        # api.trustedrouter.com. Direct browser fetch to api.trustedrouter.com
        # is blocked by CORS (the attested gateway 401s preflight
        # with no ACAO headers). The proxy pipes raw bytes without
        # inspecting / logging them — privacy posture matches the
        # attested gateway itself. Same-origin also means x-trustedrouter-
        # provider response headers are visible without any CORS
        # expose-headers work, so "via {provider}" lights up.
        api_base_url="/chat-proxy/v1",
        site_url=f"https://{settings.trusted_domain}/chat",
        title="Chat | TrustedRouter",
        heading="Chat",
        description=(
            "Try any model and compare up to four at once. Zero "
            "tokens spent until you sign in."
        ),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_providers_html(settings: Settings) -> str:
    return _env().get_template("public/providers.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/providers",
        title="Providers | TrustedRouter",
        heading="Providers",
        description=(
            "Provider transparency for model compute, retention, confidential compute, and encrypted routes."
        ),
        providers=[_provider_view(provider) for provider in providers_for_display()],
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_provider_detail_html(settings: Settings, provider_slug: str) -> str | None:
    provider = PROVIDERS.get(provider_slug)
    if provider is None:
        return None
    served_models = _provider_model_rows(provider_slug)
    return _env().get_template("public/provider_detail.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/providers/{provider.slug}",
        title=f"{provider.name} Models | TrustedRouter",
        heading=provider.name,
        description=(
            f"{provider.name} models on TrustedRouter with prices, routes, policy notes, and source links."
        ),
        provider=_provider_detail_view(provider, served_models=served_models),
        served_models=served_models,
        measured=measured_for_provider(provider.slug, test_mode=settings.environment == "test"),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_model_detail_html(settings: Settings, model_id: str) -> str | None:
    """Render the per-model detail page for `/models/{author}/{slug}`.
    Returns None when the model id isn't in the catalog (route handler
    converts that to a styled 404)."""
    model = MODELS.get(model_id)
    if model is None or model.id in META_MODEL_IDS:
        return None
    site_url = f"https://{settings.trusted_domain}/models/{model_id}"
    return _env().get_template("public/model_detail.html").render(
        api_base_url=settings.api_base_url,
        site_url=site_url,
        title=f"{model.name} | TrustedRouter",
        heading=model.name,
        description=f"All providers serving {model.name} via TrustedRouter.",
        model=_model_detail_view(model),
        # Service/Offer JSON-LD. The page sells API access to a hosted
        # routing service, not a retail product with customer ratings.
        # Avoid Product schema so Search Console doesn't expect review
        # or aggregateRating fields that we cannot honestly provide yet.
        json_ld_blob=_model_json_ld(settings, model, site_url),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_model_compare_html(settings: Settings, left_id: str, right_id: str) -> str | None:
    left = MODELS.get(left_id)
    right = MODELS.get(right_id)
    if (
        left is None
        or right is None
        or left.id in META_MODEL_IDS
        or right.id in META_MODEL_IDS
        or left.id == right.id
    ):
        return None
    site_path = f"/compare/models/{left.id}/vs/{right.id}"
    return _env().get_template("public/model_compare.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}{site_path}",
        title=f"{left.name} vs {right.name} | TrustedRouter",
        heading=f"{left.name} vs {right.name}",
        description=(
            f"Compare {left.name} and {right.name} by providers, context, price, "
            "and TrustedRouter route support."
        ),
        left=_model_detail_view(left),
        right=_model_detail_view(right),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_model_section_html(settings: Settings, model_id: str, section: str) -> str | None:
    model = MODELS.get(model_id)
    if model is None or model.id in META_MODEL_IDS or section not in MODEL_SEO_SECTIONS:
        return None
    site_url = f"https://{settings.trusted_domain}/models/{model_id}/{section}"
    label = MODEL_SEO_SECTION_LABELS[section]
    return _env().get_template("public/model_section.html").render(
        api_base_url=settings.api_base_url,
        site_url=site_url,
        title=f"{model.name} {label} | TrustedRouter",
        heading=f"{model.name} {label}",
        description=_model_section_description(model, section),
        model=_model_detail_view(model, active_section=section),
        section=section,
        section_label=label,
        benchmark_links=_benchmark_links(model),
        benchmark_scores=scores_for_model(model.id),
        measured=measured_for_model(model.id, test_mode=settings.environment == "test"),
        json_ld_blob=_model_json_ld(settings, model, f"https://{settings.trusted_domain}/models/{model_id}"),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def public_model_not_found_html(settings: Settings, model_id: str) -> str:
    """Styled HTML 404 for `/models/{nonexistent}` — keeps the visitor
    inside the marketing chrome instead of dumping FastAPI's default
    JSON error body."""
    return _env().get_template("public/model_not_found.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/models",
        title="Model not found | TrustedRouter",
        heading="Model not found",
        description=f"No model with id {model_id} is in the TrustedRouter catalog.",
        requested_model_id=model_id,
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=_static_version(settings),
    )


def robots_txt(settings: Settings) -> str:
    domain = settings.trusted_domain
    return "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "Disallow: /console",
            "Disallow: /auth/",
            "Disallow: /v1/",
            "Disallow: /internal/",
            "Disallow: /google_oauth_callback",
            "Disallow: /github_oauth_callback",
            f"Sitemap: https://{domain}/sitemap.xml",
            "",
        ]
    )


def sitemap_xml(settings: Settings) -> str:
    domain = settings.trusted_domain
    paths: list[tuple[str, str, str]] = []
    for path in SEO_CORE_PATHS:
        paths.append((path, "daily" if path in {"/models", "/providers"} else "weekly", "0.9"))
    for provider in providers_for_display():
        paths.append((f"/providers/{provider.slug}", "weekly", "0.7"))
    for model in _public_models_for_seo():
        paths.append((f"/models/{model.id}", "daily", "0.8"))
        for section in MODEL_SEO_SECTIONS:
            paths.append((f"/models/{model.id}/{section}", "daily", "0.7"))
    for left, right in _model_comparison_pairs():
        paths.append((f"/compare/models/{left.id}/vs/{right.id}", "weekly", "0.5"))
    urls = "\n".join(
        "  <url>"
        f"<loc>{xml_escape(f'https://{domain}{path}')}</loc>"
        f"<changefreq>{changefreq}</changefreq>"
        f"<priority>{priority}</priority>"
        "</url>"
        for path, changefreq, priority in paths
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )


def llms_txt(settings: Settings) -> str:
    domain = settings.trusted_domain
    model_count = len(_public_models_for_seo())
    provider_count = len(providers_for_display())
    lines = [
        "# TrustedRouter",
        "",
        "TrustedRouter is an OpenAI compatible AI router with an attested prompt path.",
        "The control plane does not terminate prompt traffic; prompts belong on api.trustedrouter.com.",
        "",
        "## Primary Links",
        f"- Homepage: https://{domain}/",
        f"- Models: https://{domain}/models",
        f"- Providers: https://{domain}/providers",
        f"- Benchmarks: https://{domain}/benchmarks",
        f"- Rankings: https://{domain}/rankings",
        "- Status: https://status.trustedrouter.com/",
        "- Trust: https://trust.trustedrouter.com/",
        f"- Legal/procurement packet: https://{domain}/legal",
        f"- SOC 2 readiness: https://{domain}/legal/soc2-readiness",
        f"- HIPAA readiness: https://{domain}/legal/hipaa-readiness",
        f"- Agent setup: https://{domain}/docs/agent-setup",
        f"- Evals guide: https://{domain}/docs/evals",
        f"- Migration guide: https://{domain}/docs/migrate-from-openrouter",
        "",
        "## API",
        "- OpenAI compatible base URL: https://api.trustedrouter.com/v1",
        "- Chat completions: POST /v1/chat/completions",
        "- Responses: POST /v1/responses",
        "- Models: GET /v1/models",
        "- Providers: GET /v1/providers",
        "",
        "## Catalog",
        f"- Public model pages: {model_count}",
        f"- Provider pages: {provider_count}",
        "- Model pages include providers, pricing, performance, uptime, API quickstart, and benchmark links.",
        "",
        "## Privacy Boundary",
        "- TrustedRouter stores metadata and billing records, not prompt or output content by default.",
        "- Provider compute policy is shown separately on provider and model pages.",
        "",
    ]
    return "\n".join(lines)


def docs_llms_txt(settings: Settings) -> str:
    domain = settings.trusted_domain
    return "\n".join(
        [
            "# TrustedRouter Docs",
            "",
            f"- Agent setup: https://{domain}/docs/agent-setup",
            f"- Evals guide: https://{domain}/docs/evals",
            f"- Migrate from OpenRouter: https://{domain}/docs/migrate-from-openrouter",
            f"- Security: https://{domain}/security",
            f"- Legal/procurement packet: https://{domain}/legal",
            f"- SOC 2 readiness: https://{domain}/legal/soc2-readiness",
            f"- HIPAA readiness: https://{domain}/legal/hipaa-readiness",
            f"- Model catalog: https://{domain}/models",
            f"- Provider transparency: https://{domain}/providers",
            "- Public status: https://status.trustedrouter.com/",
            "- Trust evidence: https://trust.trustedrouter.com/",
            "",
            "Use https://api.trustedrouter.com/v1 as the OpenAI compatible API base URL.",
            "",
        ]
    )


def docs_llms_full_txt(settings: Settings) -> str:
    domain = settings.trusted_domain
    models = _seo_model_rows()
    providers = [_provider_view(provider) for provider in providers_for_display()]
    lines = [
        "# TrustedRouter Full LLM Context",
        "",
        "TrustedRouter is a hosted AI routing service with OpenAI compatible APIs and an attested gateway.",
        "The hosted prompt path is designed so the API gateway source, image digest, and attestation can be verified.",
        "",
        "## Canonical URLs",
        f"- Homepage: https://{domain}/",
        "- API base: https://api.trustedrouter.com/v1",
        "- Trust: https://trust.trustedrouter.com/",
        f"- Legal/procurement packet: https://{domain}/legal",
        f"- SOC 2 readiness: https://{domain}/legal/soc2-readiness",
        f"- HIPAA readiness: https://{domain}/legal/hipaa-readiness",
        "- Status: https://status.trustedrouter.com/",
        f"- Agent setup: https://{domain}/docs/agent-setup",
        f"- Evals guide: https://{domain}/docs/evals",
        f"- Migration guide: https://{domain}/docs/migrate-from-openrouter",
        f"- Compact LLM docs: https://{domain}/docs/llms.txt",
        f"- Full LLM docs: https://{domain}/docs/llms-full.txt",
        "",
        "## Models",
    ]
    for model in models[:250]:
        lines.append(
            f"- {model['id']}: {model['name']}; providers={model['provider_count']}; "
            f"prompt={model['prompt_price']}; completion={model['completion_price']}; "
            f"url=https://{domain}{model['detail_href']}"
        )
    if len(models) > 250:
        lines.append(f"- Additional model pages are listed in https://{domain}/sitemap.xml")
    lines.extend(["", "## Providers"])
    for provider in providers:
        lines.append(
            f"- {provider['name']} ({provider['id']}): tier={provider['privacy_tier']}; "
            f"ZDR={provider['zero_data_retention_label']}; "
            f"confidential={provider['confidential_compute_label']}; "
            f"E2EE={provider['provider_e2ee_label']}; "
            f"url=https://{domain}{provider['detail_href']}"
        )
    lines.extend(
        [
            "",
            "## Important Boundary",
            "TrustedRouter can prove the router code path and prompt transport boundary. "
            "It cannot make every upstream model provider confidential unless that route is explicitly marked.",
            "",
        ]
    )
    return "\n".join(lines)


def _model_view(model: Model) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    endpoints = endpoints_for_model(model.id) if model.id not in META_MODEL_IDS else []
    if model.id in META_MODEL_IDS:
        candidates = meta_candidate_models(model.id)
        prompt = _price_range(candidates, "prompt_price_microdollars_per_million_tokens")
        completion = _price_range(candidates, "completion_price_microdollars_per_million_tokens")
    elif endpoints:
        prompt = _endpoint_price_range(endpoints, "prompt_price_microdollars_per_million_tokens")
        completion = _endpoint_price_range(endpoints, "completion_price_microdollars_per_million_tokens")
    else:
        prompt = _price(model.prompt_price_microdollars_per_million_tokens)
        completion = _price(model.completion_price_microdollars_per_million_tokens)
    providers = _endpoint_provider_views(endpoints, fallback_provider=model.provider)
    return {
        "id": model.id,
        "name": model.name,
        "provider": provider.name,
        "publisher_slug": model.provider,
        "context_length": f"{model.context_length:,}",
        "prompt_price": prompt,
        "completion_price": completion,
        # Derive from endpoints (not the raw Model flag): supplemental
        # provider-native models carry prepaid_available=False as a catalog
        # dedup marker, but DO have a priced Credits endpoint and are fully
        # prepaid-routable. Mirror model_to_openrouter_shape so the public
        # catalog/detail page matches /v1/models.
        "prepaid": any(endpoint.usage_type == "Credits" for endpoint in endpoints)
        or model.prepaid_available,
        "byok": model.byok_available,
        "attested": provider.attested_gateway,
        "stores_content": provider.stores_content,
        "provider_zero_data_retention": provider.provider_zero_data_retention,
        "provider_confidential_compute": provider.provider_confidential_compute,
        "provider_e2ee": provider.provider_e2ee,
        "providers": providers,
        "provider_count": len(providers),
        "detail_href": f"/models/{model.id}" if model.id not in META_MODEL_IDS else None,
        "benchmarks_href": (
            f"/models/{model.id}/benchmarks" if model.id not in META_MODEL_IDS else None
        ),
    }


def _endpoint_provider_views(
    endpoints: Sequence[ModelEndpoint], *, fallback_provider: str
) -> list[dict[str, str]]:
    """Return distinct serving providers in endpoint order.

    A model can have separate Credits and BYOK endpoints on the same
    provider. The public catalog should list provider companies once,
    then let the detail table expose individual endpoint rows.
    """
    seen: set[str] = set()
    provider_views: list[dict[str, str]] = []
    provider_slugs = [endpoint.provider for endpoint in endpoints] or [fallback_provider]
    for slug in provider_slugs:
        if slug in seen:
            continue
        seen.add(slug)
        provider = PROVIDERS.get(slug)
        provider_views.append({"name": provider.name if provider else slug, "slug": slug})
    return provider_views


def _provider_view(provider: Provider) -> dict[str, object]:
    return {
        "id": provider.slug,
        "name": provider.name,
        "supports_prepaid": provider.supports_prepaid,
        "supports_byok": provider.supports_byok,
        "attested_gateway": provider.attested_gateway,
        "gateway_stores_content": provider.stores_content,
        "zero_data_retention": provider.provider_zero_data_retention,
        "confidential_compute": provider.provider_confidential_compute,
        "provider_e2ee": provider.provider_e2ee,
        "zero_data_retention_label": _policy_label(provider.provider_zero_data_retention),
        "confidential_compute_label": _policy_label(provider.provider_confidential_compute),
        "provider_e2ee_label": _policy_label(provider.provider_e2ee),
        "policy": provider.provider_policy,
        "policy_url": provider.provider_policy_url,
        "privacy_tier": _provider_privacy_tier(provider),
        "detail_href": f"/providers/{provider.slug}",
    }


def _provider_detail_view(
    provider: Provider,
    *,
    served_models: list[dict[str, object]],
) -> dict[str, object]:
    view = _provider_view(provider)
    view["served_model_count"] = len(served_models)
    view["prepaid_model_count"] = sum(1 for model in served_models if model["prepaid"])
    view["byok_model_count"] = sum(1 for model in served_models if model["byok"])
    return view


def _provider_privacy_tier(provider: Provider) -> str:
    if provider.slug == "trustedrouter":
        return "TR gateway"
    if provider.provider_e2ee and provider.provider_confidential_compute:
        return "Confidential"
    if provider.provider_zero_data_retention:
        return "No logs"
    if provider.provider_confidential_compute:
        return "Confidential compute"
    return "No provider claim"


def _policy_label(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "not claimed"


def _model_detail_view(model: Model, *, active_section: str | None = None) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    endpoints = endpoints_for_model(model.id)
    endpoint_views: list[dict[str, object]] = []
    for endpoint in endpoints:
        ep_provider = PROVIDERS.get(endpoint.provider)
        endpoint_views.append({
            "provider": ep_provider.name if ep_provider else endpoint.provider,
            "provider_slug": endpoint.provider,
            "provider_href": f"/providers/{endpoint.provider}",
            "usage_type": endpoint.usage_type,
            "prompt_price": _price(endpoint.prompt_price_microdollars_per_million_tokens),
            "completion_price": _price(endpoint.completion_price_microdollars_per_million_tokens),
            "prompt_microdollars_per_million_tokens": endpoint.prompt_price_microdollars_per_million_tokens,
            "completion_microdollars_per_million_tokens": endpoint.completion_price_microdollars_per_million_tokens,
            "attested_gateway": ep_provider.attested_gateway if ep_provider else False,
            "stores_content": ep_provider.stores_content if ep_provider else False,
            "provider_zero_data_retention": (
                ep_provider.provider_zero_data_retention if ep_provider else None
            ),
            "provider_confidential_compute": (
                ep_provider.provider_confidential_compute if ep_provider else None
            ),
            "provider_e2ee": ep_provider.provider_e2ee if ep_provider else None,
            "provider_policy": ep_provider.provider_policy if ep_provider else "",
            "endpoint_id": endpoint.id,
        })
    # Sort cheapest-first by total prompt+completion price; ties broken by
    # provider name. Click-to-sort JS in the template lets visitors flip
    # to throughput / latency / context views.
    endpoint_views.sort(
        key=lambda view: (
            cast(int, view["prompt_microdollars_per_million_tokens"])
            + cast(int, view["completion_microdollars_per_million_tokens"]),
            str(view["provider"]),
        )
    )
    return {
        "id": model.id,
        "name": model.name,
        "provider": provider.name,
        "publisher_slug": model.provider,
        "context_length": f"{model.context_length:,}",
        "context_length_int": model.context_length,
        "endpoints": endpoint_views,
        "endpoint_count": len(endpoint_views),
        "providers": _endpoint_provider_views(endpoints, fallback_provider=model.provider),
        "section_links": _model_section_links(model.id, active_section=active_section),
        "supports_chat": model.supports_chat,
        "supports_messages": model.supports_messages,
        "supports_embeddings": model.supports_embeddings,
        # Derive from endpoints (not the raw Model flag): supplemental
        # provider-native models carry prepaid_available=False as a catalog
        # dedup marker, but DO have a priced Credits endpoint and are fully
        # prepaid-routable. Mirror model_to_openrouter_shape so the public
        # catalog/detail page matches /v1/models.
        "prepaid": any(endpoint.usage_type == "Credits" for endpoint in endpoints)
        or model.prepaid_available,
        "byok": model.byok_available,
    }


def _model_section_links(
    model_id: str,
    *,
    active_section: str | None,
) -> list[dict[str, object]]:
    links: list[dict[str, object]] = [
        {
            "label": "Overview",
            "href": f"/models/{model_id}",
            "active": active_section is None,
        }
    ]
    for section in MODEL_SEO_SECTIONS:
        links.append(
            {
                "label": MODEL_SEO_SECTION_LABELS[section],
                "href": f"/models/{model_id}/{section}",
                "active": active_section == section,
            }
        )
    return links


def _model_section_description(model: Model, section: str) -> str:
    label = MODEL_SEO_SECTION_LABELS[section].lower()
    if section == "benchmarks":
        return f"Benchmark and measurement links for {model.name}, with TrustedRouter route data first."
    if section == "providers":
        return f"Every provider endpoint TrustedRouter can route for {model.name}."
    if section == "performance":
        return f"TrustedRouter performance signals and provider route posture for {model.name}."
    if section == "pricing":
        return f"Prompt and completion pricing for every {model.name} route."
    if section == "uptime":
        return f"Uptime and status entry points for {model.name} routes."
    if section == "api":
        return f"OpenAI compatible quickstart for {model.name} on TrustedRouter."
    return f"{model.name} {label} on TrustedRouter."


def _benchmark_links(model: Model) -> list[dict[str, str]]:
    provider_links = list(_PROVIDER_MODEL_INFO_LINKS.get(model.provider, ()))
    model_links = list(_MODEL_SPECIFIC_BENCHMARK_LINKS.get(model.id, ()))
    return [
        {
            "label": "TrustedRouter performance page",
            "href": f"/models/{model.id}/performance",
            "kind": "TrustedRouter measurement",
        },
        {
            "label": "TrustedRouter uptime page",
            "href": f"/models/{model.id}/uptime",
            "kind": "TrustedRouter measurement",
        },
        *model_links,
        *provider_links,
        *_BENCHMARK_INDEX_LINKS,
    ]


def _public_models_for_seo() -> list[Model]:
    return sorted(
        [model for model in MODELS.values() if model.id not in META_MODEL_IDS],
        key=lambda model: model.id,
    )


def _model_comparison_pairs() -> list[tuple[Model, Model]]:
    candidates = sorted(
        _public_models_for_seo(),
        key=lambda model: (
            -len(endpoints_for_model(model.id)),
            -(model.context_length or 0),
            model.id.lower(),
        ),
    )[:MODEL_COMPARE_MODEL_LIMIT]
    return list(combinations(candidates, 2))[:MODEL_COMPARE_URL_LIMIT]


def _seo_model_rows() -> list[dict[str, object]]:
    return [_model_view(model) for model in _public_models_for_seo()]


def _provider_model_rows(provider_slug: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for model in _public_models_for_seo():
        endpoints = [endpoint for endpoint in endpoints_for_model(model.id) if endpoint.provider == provider_slug]
        if not endpoints:
            continue
        rows.append(
            {
                "id": model.id,
                "name": model.name,
                "detail_href": f"/models/{model.id}",
                "benchmarks_href": f"/models/{model.id}/benchmarks",
                "context_length": f"{model.context_length:,}",
                "endpoint_count": len(endpoints),
                "prompt_price": _endpoint_price_range(
                    endpoints,
                    "prompt_price_microdollars_per_million_tokens",
                ),
                "completion_price": _endpoint_price_range(
                    endpoints,
                    "completion_price_microdollars_per_million_tokens",
                ),
                "prepaid": any(not endpoint.is_byok for endpoint in endpoints),
                "byok": any(endpoint.is_byok for endpoint in endpoints),
            }
        )
    return sorted(rows, key=lambda row: str(row["id"]))


_BRAND_DISPLAY_NAMES: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "meta-llama": "Meta",
    "mistralai": "Mistral AI",
    "moonshotai": "Moonshot AI",
    "z-ai": "Z.AI",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "x-ai": "xAI",
    "minimax": "MiniMax",
    "thedrummer": "TheDrummer",
    "arcee-ai": "Arcee AI",
    "stepfun": "StepFun",
    "bytedance": "ByteDance",
    "xiaomi": "Xiaomi",
    "nousresearch": "Nous Research",
    "phala": "Phala",
}


def _model_json_ld(settings: Settings, model: Model, site_url: str) -> str:
    """Build the Service/Offer JSON-LD blob for the model detail page.

    Returns a JSON string ready to be injected into a
    `<script type="application/ld+json">` tag.

    Price: cheapest prompt rate across this model's endpoints, expressed
    as USD per million tokens, matching the unit the page itself displays.
    """
    endpoints = endpoints_for_model(model.id)
    prompt_prices = [
        ep.prompt_price_microdollars_per_million_tokens
        for ep in endpoints
        if ep.prompt_price_microdollars_per_million_tokens > 0
    ]
    if not prompt_prices:
        # Edge case: catalog has the model but no priced endpoint.
        # Fall back to the model-level price (often the cheapest seen
        # historically).
        cheapest_micro_per_m = model.prompt_price_microdollars_per_million_tokens
    else:
        cheapest_micro_per_m = min(prompt_prices)
    # microdollars-per-million-tokens → dollars-per-million-tokens.
    cheapest_usd_per_m = cheapest_micro_per_m / MICRODOLLARS_PER_DOLLAR

    brand_slug = model.provider
    brand_name = _BRAND_DISPLAY_NAMES.get(brand_slug, brand_slug.title())

    payload = {
        "@context": "https://schema.org",
        "@type": "Service",
        "name": model.name,
        "description": (
            f"{model.name} via TrustedRouter. Pay-per-token API; pricing "
            f"shown is USD per million prompt tokens (cheapest provider). "
            f"Output tokens billed separately at the endpoint's published rate."
        ),
        "url": site_url,
        "serviceType": "AI model routing API",
        "provider": {
            "@type": "Organization",
            "name": "TrustedRouter",
            "url": f"https://{settings.trusted_domain}/",
        },
        "brand": {
            "@type": "Brand",
            "name": brand_name,
        },
        "areaServed": "Worldwide",
        "offers": {
            "@type": "Offer",
            "price": f"{cheapest_usd_per_m:.6f}",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
            "url": site_url,
            "priceSpecification": {
                "@type": "UnitPriceSpecification",
                "price": f"{cheapest_usd_per_m:.6f}",
                "priceCurrency": "USD",
                "unitCode": "E37",  # UN/CEFACT code for "kilo" — closest
                "unitText": "per million prompt tokens",
            },
        },
    }
    return json.dumps(payload, separators=(",", ":"))


def _endpoint_price_range(endpoints: Sequence[ModelEndpoint], attr: str) -> str:
    values = [getattr(ep, attr) for ep in endpoints if getattr(ep, attr) > 0]
    if not values:
        return _price(0)
    low = min(values)
    high = max(values)
    if low == high:
        return _price(low)
    return f"{_price(low)} to {_price(high)}"


def _price_range(models: list[Model], attr: str) -> str:
    values = [getattr(model, attr) for model in models if getattr(model, attr) > 0]
    if not values:
        return "selected route"
    low = min(values)
    high = max(values)
    if low == high:
        return _price(low)
    return f"{_price(low)} to {_price(high)}"


def _price(microdollars_per_million: int) -> str:
    if microdollars_per_million <= 0:
        return "selected route"
    value = Decimal(microdollars_per_million) / Decimal(MICRODOLLARS_PER_DOLLAR)
    return f"${value.normalize():f}/1M"
