"""DeepSeek — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import (
    ProviderPricingResult,
    fetch_provider,
    runtime_required_models,
)
from scripts.pricing.model_ids import (
    price_aliases_for_versioned_families,
    remember_upstream_id,
)

SLUG = "deepseek"
URL = "https://api-docs.deepseek.com/quick_start/pricing"
EXPECTED_MODELS = [
    "deepseek/deepseek-v4-flash",
]
UPSTREAM_ID_MAP: dict[str, str] = {}
_VERSIONED_PRICE_FAMILIES = {
    "deepseek/deepseek-v4-flash-": "deepseek/deepseek-v4-flash",
}
_PERSISTED_VERSIONED_MODELS = frozenset({"deepseek/deepseek-v4-flash-0731"})


def fetch() -> ProviderPricingResult:
    required_models = _PERSISTED_VERSIONED_MODELS | runtime_required_models(SLUG)
    price_aliases = price_aliases_for_versioned_families(
        required_models,
        _VERSIONED_PRICE_FAMILIES,
    )
    for model_id in price_aliases:
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, "deepseek-v4-flash")
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=required_models,
        required_model_price_aliases=price_aliases,
    )
