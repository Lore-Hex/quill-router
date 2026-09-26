"""Telnyx provider-native catalog and pricing refresh.

Telnyx's authenticated OpenAI-compatible model feed is authoritative for
models callable by the operator account. Pricing is reconciled from two
authenticated provider-owned sources:

1. positive rates in the authenticated model feed;
2. standard-tier rates from the inference product pricing API.

The authenticated feed now publishes positive, cached-inclusive USD rates.
Legacy zero placeholders are never interpreted as free. Product pricing is
consulted only for missing prices, never as a prerequisite for a complete
authenticated catalog. Unpriced models are held out of routing by the manifest
writer. Account-level free allowances are not per-request token discounts.
"""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from scripts.pricing.openai_catalog import positive_int

SLUG = "telnyx"
BASE_URL = "https://api.telnyx.com/v2/ai/openai"
MODELS_URL = f"{BASE_URL}/models"
PRICING_URL = "https://telnyx.com/pricing/inference-api"
PRODUCT_PRICING_URL = "https://api.telnyx.com/v2/pricing/products/inference"
URL = PRICING_URL
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "telnyx.json"
)

_NATIVE_TO_OR_ID = {
    "google/gemma-2b-it": "google/gemma-2b-it",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/Meta-Llama-3.1-70B-Instruct": "meta-llama/llama-3.1-70b-instruct",
    "meta-llama/Meta-Llama-3.1-8B-Instruct": "meta-llama/llama-3.1-8b-instruct",
    "MiniMaxAI/MiniMax-M2.7": "minimax/minimax-m2.7",
    "MiniMaxAI/MiniMax-M3-MXFP8": "minimax/minimax-m3",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K3": "moonshotai/kimi-k3",
    "Qwen/Qwen3-235B-A22B": "qwen/qwen3-235b-a22b",
    "zai-org/GLM-5.1-FP8": "z-ai/glm-5.1",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
}

EXPECTED_MODELS = list(_NATIVE_TO_OR_ID.values())
UPSTREAM_ID_MAP = {model_id: native_id for native_id, model_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}

# Product billing names are not inference IDs. Join exact native basenames
# except for these two names verified against both authenticated feeds.
_PRODUCT_NAMES = {
    "deepseek-ai/DeepSeek-V4-Flash-0731": "deepseek-v4-flash",
    "deepseek-ai/DeepSeek-V4.1-Flash": "deepseek-v41-flash",
}


def _dollars_per_m_to_micro_per_m(value: object, *, allow_zero: bool = False) -> int | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        return None
    return int((parsed * Decimal(1_000_000)).to_integral_value(ROUND_HALF_UP))


def _model_price(
    *,
    prompt: object,
    completion: object,
    cached: object = None,
) -> ModelPrice | None:
    prompt_micro = _dollars_per_m_to_micro_per_m(prompt)
    completion_micro = _dollars_per_m_to_micro_per_m(completion)
    if prompt_micro is None or completion_micro is None:
        return None
    cached_micro = _dollars_per_m_to_micro_per_m(cached, allow_zero=True)
    if cached is not None and cached_micro is None:
        return None
    return ModelPrice(
        prompt_micro_per_m=prompt_micro,
        completion_micro_per_m=completion_micro,
        prompt_cached_micro_per_m=cached_micro,
    )


def _live_catalog(
    payload: object,
) -> tuple[dict[str, dict[str, Any]], dict[str, ModelPrice]]:
    source_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        raise RuntimeError("telnyx: authenticated /models response has no data list")

    discovered: dict[str, dict[str, Any]] = {}
    direct_prices: dict[str, ModelPrice] = {}
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        if str(source.get("owned_by") or "").casefold() != "telnyx":
            continue
        if str(source.get("task") or "").casefold() != "text-generation":
            continue
        service_tiers = source.get("service_tiers")
        if service_tiers is not None:
            if (
                not isinstance(service_tiers, list)
                or not service_tiers
                or any(not isinstance(tier, str) or not tier.strip() for tier in service_tiers)
            ):
                raise RuntimeError("telnyx: invalid service_tiers in authenticated catalog")
            if "default" not in {tier.strip().casefold() for tier in service_tiers}:
                continue
        native_id = source.get("id")
        if not isinstance(native_id, str) or not native_id:
            continue
        model_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if model_id is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        input_modalities = ["text"]
        if source.get("is_vision_supported") is True:
            input_modalities.append("image")
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(source.get("name") or native_id.split("/", 1)[-1]),
            "title": native_id,
            "model_type": "chat",
            "input_modalities": input_modalities,
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "status": 1,
            # Available upstream tiers are metadata, not TR routing support.
            # Our published prices and requests remain on the default tier.
            "provider_service_tiers": sorted(
                {tier.strip().casefold() for tier in (service_tiers or ["default"])}
            ),
            "pricing_source": MODELS_URL,
        }
        license_name = source.get("license")
        if isinstance(license_name, str) and license_name.strip():
            row["license"] = license_name.strip()
        context_length = positive_int(source.get("context_length"))
        if context_length is not None:
            row["context_length"] = context_length
        max_output_tokens = positive_int(source.get("max_completion_tokens"))
        if "max_completion_tokens" in source:
            row["max_output_tokens"] = max_output_tokens
        regions = source.get("regions")
        # TR serves the default tier. A union across tiers can overstate that
        # route's geography; missing declarations must also clear old metadata.
        if "regions_by_service_tier" in source:
            tier_regions = source["regions_by_service_tier"]
            regions = tier_regions.get("default") if isinstance(tier_regions, dict) else None
        row["provider_regions"] = (
            list(dict.fromkeys(regions))
            if isinstance(regions, list) and all(isinstance(r, str) and r for r in regions)
            else []
        )
        if model_id in discovered:
            raise RuntimeError(f"telnyx: duplicate canonical model {model_id}")
        discovered[model_id] = row

        pricing = source.get("pricing")
        if not isinstance(pricing, dict):
            continue
        currency = str(pricing.get("currency") or "").strip().casefold()
        if not currency:
            continue  # Legacy rows require an independently USD-denominated fallback.
        if currency != "usd":
            raise RuntimeError(f"telnyx: unsupported pricing currency for {native_id}")
        if str(pricing.get("unit") or "").casefold() != "1m_tokens":
            continue
        price = _model_price(
            prompt=pricing.get("input"),
            completion=pricing.get("output"),
            cached=pricing.get("cached_prompt"),
        )
        if price is not None:
            direct_prices[model_id] = price
    if not discovered:
        raise RuntimeError("telnyx: authenticated catalog returned no Telnyx text models")
    return discovered, direct_prices


def _product_rate(value: object) -> Decimal | None:
    """Accept a flat paid rate, never reinterpret volume tiers as context tiers."""
    if not isinstance(value, list) or not value:
        return None
    rates: set[Decimal] = set()
    for band in value:
        if not isinstance(band, dict):
            return None
        try:
            rate = Decimal(str(band.get("rate")))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not rate.is_finite() or rate < 0:
            return None
        rates.add(rate)
    if len(rates) != 1:
        return None
    return rates.pop() * 1000  # Product API uses USD/1k, catalog uses USD/1M.


def _product_prices(
    payload: object,
    discovered: dict[str, dict[str, Any]],
) -> dict[str, ModelPrice]:
    source_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        raise RuntimeError("telnyx: product pricing response has no data list")
    product_models: dict[str, str] = {}
    for model_id, row in discovered.items():
        native_id = row["upstream_id"]
        product_name = _PRODUCT_NAMES.get(native_id, native_id.split("/", 1)[-1].casefold())
        if product_name in product_models:
            raise RuntimeError(f"telnyx: ambiguous product name {product_name}")
        product_models[product_name] = model_id
    prices: dict[str, ModelPrice] = {}
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        if source.get("service_tier") != "standard":
            continue
        model_id = product_models.get(str(source.get("model") or ""))
        if model_id is None:
            continue
        rates = source.get("rates")
        if not isinstance(rates, dict):
            continue
        if rates.get("currency") != "USD" or rates.get("unit") != "per_1k_tokens":
            raise RuntimeError(f"telnyx: unsupported product pricing units for {model_id}")
        values = rates.get("values")
        if not isinstance(values, dict):
            continue
        cached = _product_rate(values.get("cached_input"))
        if "cached_input" in values and cached is None:
            continue
        price = _model_price(
            prompt=_product_rate(values.get("input")),
            completion=_product_rate(values.get("output")),
            cached=cached,
        )
        if price is not None:
            if model_id in prices:
                raise RuntimeError(f"telnyx: duplicate standard product price for {model_id}")
            prices[model_id] = price
    return prices


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("TELNYX_API_KEY")
    if not api_key:
        raise RuntimeError("telnyx: TELNYX_API_KEY is required")
    headers = {"Authorization": f"Bearer {api_key}"}
    live_payload = fetch_json(MODELS_URL, extra_headers=headers)
    discovered, direct_prices = _live_catalog(live_payload)
    prices = dict(direct_prices)
    if discovered.keys() - prices.keys():
        product_prices = _product_prices(
            fetch_json(PRODUCT_PRICING_URL, extra_headers=headers),
            discovered,
        )
        for model_id in discovered.keys() - prices.keys():
            if model_id in product_prices:
                prices[model_id] = product_prices[model_id]
                discovered[model_id]["pricing_source"] = PRODUCT_PRICING_URL
    # Delisted historical IDs must not freeze discovery of the current catalog.
    errors = validate(prices, [])
    if errors:
        raise RuntimeError("; ".join(errors))

    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=MODELS_URL,
        notes=[
            f"discovered {len(discovered)} Telnyx-owned text models",
            "pricing precedence: authenticated catalog > standard-tier product pricing API",
            f"{len(discovered.keys() - prices.keys())} models withheld for missing prices",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
    )
