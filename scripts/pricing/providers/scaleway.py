"""Scaleway authenticated availability with first-party EUR token prices."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from scripts.pricing.base import ModelPrice, fetch_html, fetch_json
from scripts.pricing.currency import (
    ECB_FX_URL,
    FX_RESERVE,  # noqa: F401 - retained for callers auditing the conversion reserve
)
from scripts.pricing.currency import (
    eur_microdollars_per_million as _microdollars_per_million,
)
from scripts.pricing.currency import (
    usd_per_eur as _usd_per_eur,
)
from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "scaleway"
BASE_URL = "https://api.scaleway.ai/v1"
URL = f"{BASE_URL}/models"
# Scaleway's public Product Catalog API (no credentials). It replaced the
# marketing pricing page, which started answering automated fetches with 403
# in November 2026. Same first-party EUR list prices, per 1,000 tokens.
PRICING_URL = "https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products"
PRICING_QUERY = {"product_types": "generative_apis", "page_size": 500}
PRICING_REGION = "fr-par"
MAX_PRICING_PAGES = 10
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/scaleway.json"
)
MANIFEST_STALE_FALLBACK = True

MODEL_MAP = {
    "llama-3.3-70b-instruct": "meta-llama/llama-3.3-70b-instruct",
    "pixtral-12b-2409": "mistralai/pixtral-12b-2409",
    "qwen3-235b-a22b-instruct-2507": "qwen/qwen3-235b-a22b-instruct-2507",
    "mistral-small-3.2-24b-instruct-2506": "mistralai/mistral-small-3.2-24b-instruct-2506",
    "qwen3-coder-30b-a3b-instruct": "qwen/qwen3-coder-30b-a3b-instruct",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "qwen3.5-397b-a17b": "qwen/qwen3.5-397b-a17b",
    "gemma-4-26b-a4b-it": "google/gemma-4-26b-a4b-it",
    "qwen3.6-35b-a3b": "qwen/qwen3.6-35b-a3b",
    "mistral-medium-3.5-128b": "mistralai/mistral-medium-3.5-128b",
    "glm-5.2": "z-ai/glm-5.2",
    "deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
}

_TOKEN_KINDS = {
    "input_token": "prompt",
    "output_token": "completion",
    "input_cached_token": "cached",
}


def _eur_per_million(product: dict[str, Any]) -> Decimal | None:
    price = product.get("price")
    retail = price.get("retail_price") if isinstance(price, dict) else None
    unit = product.get("unit_of_measure")
    if not isinstance(retail, dict) or not isinstance(unit, dict):
        return None
    if retail.get("currency_code") != "EUR" or unit.get("unit") != "token":
        raise RuntimeError(f"scaleway: unexpected price unit for {product.get('sku')!r}")
    size, units, nanos = unit.get("size"), retail.get("units", 0), retail.get("nanos", 0)
    if not all(type(value) is int for value in (size, units, nanos)) or size <= 0:
        raise RuntimeError(f"scaleway: malformed price for {product.get('sku')!r}")
    try:
        eur = (Decimal(units) + Decimal(nanos) / Decimal(1_000_000_000)) * Decimal(1_000_000)
        return eur / Decimal(size)
    except InvalidOperation as exc:
        raise RuntimeError(f"scaleway: malformed price for {product.get('sku')!r}") from exc


def _parse_catalog_prices(
    products: list[Any], *, usd_per_eur: Decimal
) -> dict[str, ModelPrice]:
    """Realtime fr-par token list prices from the public Product Catalog."""

    rates: dict[str, dict[str, Decimal]] = {}
    for product in products:
        if not isinstance(product, dict) or product.get("product_category") != "Generative APIs":
            continue
        model_id = MODEL_MAP.get(str(product.get("product")))
        properties = product.get("properties")
        generative = properties.get("generative_apis") if isinstance(properties, dict) else None
        locality = product.get("locality")
        if (
            model_id is None
            or not isinstance(generative, dict)
            or generative.get("consumption_mode") != "realtime"
            or not isinstance(locality, dict)
            or locality.get("region") != PRICING_REGION
        ):
            continue
        kind = _TOKEN_KINDS.get(str(generative.get("token_type")))
        if kind is None:
            continue
        eur = _eur_per_million(product)
        if eur is None:
            continue
        previous = rates.setdefault(model_id, {}).setdefault(kind, eur)
        if previous != eur:
            raise RuntimeError(f"scaleway: conflicting {kind} prices for {model_id}")
    prices: dict[str, ModelPrice] = {}
    for model_id, kinds in rates.items():
        if "prompt" not in kinds or "completion" not in kinds:
            continue
        prices[model_id] = ModelPrice(
            _microdollars_per_million(kinds["prompt"], usd_per_eur),
            _microdollars_per_million(kinds["completion"], usd_per_eur),
            prompt_cached_micro_per_m=(
                _microdollars_per_million(kinds["cached"], usd_per_eur)
                if "cached" in kinds
                else None
            ),
        )
    return prices


def _fetch_catalog_products() -> list[Any]:
    products: list[Any] = []
    for page in range(1, MAX_PRICING_PAGES + 1):
        payload = fetch_json(f"{PRICING_URL}?{urlencode({**PRICING_QUERY, 'page': page})}")
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise RuntimeError("scaleway: product catalog has no products list")
        total = payload.get("total_count")
        if type(total) is not int or total < 0:
            raise RuntimeError("scaleway: product catalog has no total_count")
        products.extend(payload["products"])
        if len(products) >= total or not payload["products"]:
            if len(products) != total:
                raise RuntimeError("scaleway: product catalog page count mismatch")
            return products
    raise RuntimeError("scaleway: product catalog exceeds the page limit")


def _load_prices() -> dict[str, ModelPrice]:
    return _parse_catalog_prices(
        _fetch_catalog_products(),
        usd_per_eur=_usd_per_eur(fetch_html(ECB_FX_URL)),
    )


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="SCALEWAY_SECRET_KEY",
        explicit_model_map=MODEL_MAP,
        expected_models=(
            "deepseek/deepseek-v4-flash-0731",
            "z-ai/glm-5.2",
            "qwen/qwen3.6-35b-a3b",
        ),
        pricing_source_url=PRICING_URL,
        price_loader=_load_prices,
        canary_max_tokens=32,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
