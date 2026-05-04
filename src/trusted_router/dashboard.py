"""Dashboard rendering. The page itself lives in templates/dashboard.html
with HTML/CSS/JS in their own files; this module only resolves
settings-driven values and renders the Jinja2 template."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from trusted_router.catalog import AUTO_MODEL_ID, MODELS, PROVIDERS, Model, auto_candidate_models
from trusted_router.config import Settings
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.og import OG_DESCRIPTION, OG_IMAGE_HEIGHT, OG_IMAGE_WIDTH, OG_TITLE
from trusted_router.regions import region_map_payload

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
DEV_USER_FALLBACK = "alpha@trustedrouter.local"


@dataclass(frozen=True)
class PublicPage:
    template: str
    title: str
    description: str


PUBLIC_PAGES: dict[str, PublicPage] = {
    "compare/openrouter": PublicPage(
        template="public/compare_openrouter.html",
        title="OpenRouter-Compatible, But Verifiable",
        description="Change base_url, keep your models, get a verifiable non-logging prompt path.",
    ),
    "compare/vercel-ai-gateway": PublicPage(
        template="public/compare_vercel_ai_gateway.html",
        title="TrustedRouter And Vercel AI Gateway",
        description=(
            "Vercel AI Gateway for Vercel-native model access. "
            "TrustedRouter for verifiable private routing."
        ),
    ),
    "compare/litellm": PublicPage(
        template="public/compare_litellm.html",
        title="TrustedRouter And LiteLLM",
        description="LiteLLM if you want to self-host. TrustedRouter if you want hosted plus attested.",
    ),
    "docs/migrate-from-openrouter": PublicPage(
        template="public/migrate_from_openrouter.html",
        title="Migrate From OpenRouter",
        description="Change base_url, keep OpenAI-compatible clients, and test the trust path.",
    ),
    "security": PublicPage(
        template="public/security.html",
        title="Security",
        description="What is logged, what is not logged, and where prompt traffic belongs.",
    ),
}


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )


def dashboard_html(settings: Settings) -> str:
    domain = settings.trusted_domain
    environment = settings.environment.lower()
    tr_config = {
        "environment": environment,
        "defaultDevUser": "" if environment == "production" else DEV_USER_FALLBACK,
        "apiBaseUrl": settings.api_base_url,
        "stablecoinCheckoutEnabled": settings.stablecoin_checkout_enabled,
        "googleEnabled": settings.google_oauth_enabled,
        "githubEnabled": settings.github_oauth_enabled,
    }
    map_regions = region_map_payload(settings)
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
        map_regions=map_regions,
        primary_region=settings.primary_region,
    )


def public_page_html(settings: Settings, page_key: str) -> str:
    page = PUBLIC_PAGES[page_key]
    return _env().get_template(page.template).render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/{page_key}",
        title=f"{page.title} - TrustedRouter",
        heading=page.title,
        description=page.description,
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
    )


def public_models_html(settings: Settings) -> str:
    return _env().get_template("public/models.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/models",
        title="Models - TrustedRouter",
        heading="Models",
        description="Public model catalog. Prompt traffic belongs on the attested API path.",
        models=[_model_view(model) for model in MODELS.values()],
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
    )


def _model_view(model: Model) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    if model.id == AUTO_MODEL_ID:
        candidates = auto_candidate_models()
        prompt = _price_range(candidates, "prompt_price_microdollars_per_million_tokens")
        completion = _price_range(candidates, "completion_price_microdollars_per_million_tokens")
    else:
        prompt = _price(model.prompt_price_microdollars_per_million_tokens)
        completion = _price(model.completion_price_microdollars_per_million_tokens)
    return {
        "id": model.id,
        "name": model.name,
        "provider": provider.name,
        "context_length": f"{model.context_length:,}",
        "prompt_price": prompt,
        "completion_price": completion,
        "prepaid": model.prepaid_available,
        "byok": model.byok_available,
        "attested": provider.attested_gateway,
        "stores_content": provider.stores_content,
    }


def _price_range(models: list[Model], attr: str) -> str:
    values = [getattr(model, attr) for model in models if getattr(model, attr) > 0]
    if not values:
        return "selected route"
    low = min(values)
    high = max(values)
    if low == high:
        return _price(low)
    return f"{_price(low)}-{_price(high)}"


def _price(microdollars_per_million: int) -> str:
    if microdollars_per_million <= 0:
        return "selected route"
    value = Decimal(microdollars_per_million) / Decimal(MICRODOLLARS_PER_DOLLAR)
    return f"${value.normalize():f}/1M"
