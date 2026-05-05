"""Dashboard rendering. The page itself lives in templates/dashboard.html
with HTML/CSS/JS in their own files; this module only resolves
settings-driven values and renders the Jinja2 template."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import cast

from jinja2 import Environment, FileSystemLoader, select_autoescape

from trusted_router.catalog import (
    AUTO_MODEL_ID,
    MODELS,
    PROVIDERS,
    Model,
    ModelEndpoint,
    auto_candidate_models,
    endpoints_for_model,
)
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


def public_model_detail_html(settings: Settings, model_id: str) -> str | None:
    """Render the per-model detail page for `/models/{author}/{slug}`.
    Returns None when the model id isn't in the catalog (route handler
    converts that to a styled 404)."""
    model = MODELS.get(model_id)
    if model is None or model.id == AUTO_MODEL_ID:
        return None
    return _env().get_template("public/model_detail.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/models/{model_id}",
        title=f"{model.name} - TrustedRouter",
        heading=model.name,
        description=f"All providers serving {model.name} via TrustedRouter.",
        model=_model_detail_view(model),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
    )


def public_model_not_found_html(settings: Settings, model_id: str) -> str:
    """Styled HTML 404 for `/models/{nonexistent}` — keeps the visitor
    inside the marketing chrome instead of dumping FastAPI's default
    JSON error body."""
    return _env().get_template("public/model_not_found.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{settings.trusted_domain}/models",
        title="Model not found - TrustedRouter",
        heading="Model not found",
        description=f"No model with id {model_id} is in the TrustedRouter catalog.",
        requested_model_id=model_id,
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
    )


def _model_view(model: Model) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    endpoints = endpoints_for_model(model.id) if model.id != AUTO_MODEL_ID else []
    if model.id == AUTO_MODEL_ID:
        candidates = auto_candidate_models()
        prompt = _price_range(candidates, "prompt_price_microdollars_per_million_tokens")
        completion = _price_range(candidates, "completion_price_microdollars_per_million_tokens")
    elif endpoints:
        prompt = _endpoint_price_range(endpoints, "prompt_price_microdollars_per_million_tokens")
        completion = _endpoint_price_range(endpoints, "completion_price_microdollars_per_million_tokens")
    else:
        prompt = _price(model.prompt_price_microdollars_per_million_tokens)
        completion = _price(model.completion_price_microdollars_per_million_tokens)
    distinct_providers = {endpoint.provider for endpoint in endpoints} or {model.provider}
    return {
        "id": model.id,
        "name": model.name,
        "provider": provider.name,
        "publisher_slug": model.provider,
        "context_length": f"{model.context_length:,}",
        "prompt_price": prompt,
        "completion_price": completion,
        "prepaid": model.prepaid_available,
        "byok": model.byok_available,
        "attested": provider.attested_gateway,
        "stores_content": provider.stores_content,
        "provider_count": len(distinct_providers),
        "detail_href": f"/models/{model.id}" if model.id != AUTO_MODEL_ID else None,
    }


def _model_detail_view(model: Model) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    endpoints = endpoints_for_model(model.id)
    endpoint_views: list[dict[str, object]] = []
    for endpoint in endpoints:
        ep_provider = PROVIDERS.get(endpoint.provider)
        endpoint_views.append({
            "provider": ep_provider.name if ep_provider else endpoint.provider,
            "provider_slug": endpoint.provider,
            "usage_type": endpoint.usage_type,
            "prompt_price": _price(endpoint.prompt_price_microdollars_per_million_tokens),
            "completion_price": _price(endpoint.completion_price_microdollars_per_million_tokens),
            "prompt_microdollars_per_million_tokens": endpoint.prompt_price_microdollars_per_million_tokens,
            "completion_microdollars_per_million_tokens": endpoint.completion_price_microdollars_per_million_tokens,
            "attested_gateway": ep_provider.attested_gateway if ep_provider else False,
            "stores_content": ep_provider.stores_content if ep_provider else False,
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
        "supports_chat": model.supports_chat,
        "supports_messages": model.supports_messages,
        "supports_embeddings": model.supports_embeddings,
        "prepaid": model.prepaid_available,
        "byok": model.byok_available,
    }


def _endpoint_price_range(endpoints: Sequence[ModelEndpoint], attr: str) -> str:
    values = [getattr(ep, attr) for ep in endpoints if getattr(ep, attr) > 0]
    if not values:
        return _price(0)
    low = min(values)
    high = max(values)
    if low == high:
        return _price(low)
    return f"{_price(low)}–{_price(high)}"


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
