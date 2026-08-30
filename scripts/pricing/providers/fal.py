"""FAL image generation with authenticated discovery and exact fixed pricing."""

from __future__ import annotations

import base64
import binascii
import os
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from scripts.pricing.base import ModelPrice, ProviderPricingResult, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    guard_fixed_output_prices,
    models_requiring_canary,
    write_discovered_chat_manifest,
)

SLUG = "fal"
API_BASE = "https://api.fal.ai/v1"
RUN_URL = "https://fal.run/fal-ai/flux/schnell"
MODEL_ID = "fal/flux-1-schnell"
UPSTREAM_ID = "fal-ai/flux/schnell"
CATALOG_URL = f"{API_BASE}/models?endpoint_id=fal-ai%2Fflux%2Fschnell"
PRICING_URL = f"{API_BASE}/models/pricing?endpoint_id=fal-ai%2Fflux%2Fschnell"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/fal.json"
)
INCLUDE_IN_PRICE_INDEX = False
MANIFEST_STALE_FALLBACK = True
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Key {api_key}", "Accept": "application/json"}


def _price_for_1024_square(payload: object) -> int:
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise RuntimeError("fal: pricing response has no prices list")
    matches = [
        row
        for row in payload["prices"]
        if isinstance(row, dict) and row.get("endpoint_id") == UPSTREAM_ID
    ]
    if len(matches) != 1:
        raise RuntimeError("fal: exact FLUX.1 Schnell price is missing or ambiguous")
    row = matches[0]
    if row.get("unit") != "megapixels" or row.get("currency") != "USD":
        raise RuntimeError("fal: unsupported pricing unit")
    try:
        dollars_per_megapixel = Decimal(str(row["unit_price"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("fal: invalid unit price") from exc
    if not dollars_per_megapixel.is_finite() or dollars_per_megapixel <= 0:
        raise RuntimeError("fal: invalid unit price")
    microdollars = dollars_per_megapixel * Decimal(1024 * 1024)
    return int(microdollars.to_integral_value(ROUND_CEILING))


def _catalog_has_route(payload: object) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return False
    return any(
        isinstance(row, dict)
        and row.get("endpoint_id") == UPSTREAM_ID
        and isinstance(row.get("metadata"), dict)
        and row["metadata"].get("status") == "active"
        for row in payload["models"]
    )


def _valid_canary_image(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    images = payload.get("images")
    nsfw = payload.get("has_nsfw_concepts")
    if not isinstance(images, list) or len(images) != 1 or nsfw != [False]:
        return False
    image = images[0]
    if not isinstance(image, dict) or image.get("width") != 1024 or image.get("height") != 1024:
        return False
    if image.get("content_type") != "image/png":
        return False
    url = image.get("url")
    prefix = "data:image/png;base64,"
    if not isinstance(url, str) or not url.startswith(prefix):
        return False
    try:
        raw = base64.b64decode(url[len(prefix) :], validate=True)
        with Image.open(BytesIO(raw)) as decoded:
            return decoded.format == "PNG" and decoded.size == (1024, 1024)
    except (binascii.Error, OSError, ValueError):
        return False


def _probe_generation(api_key: str) -> bool:
    try:
        response = httpx.post(
            RUN_URL,
            headers={**_headers(api_key), "Content-Type": "application/json"},
            json={
                "prompt": "A single black dot centered on a white background",
                "image_size": {"width": 1024, "height": 1024},
                "num_images": 1,
                "num_inference_steps": 4,
                "enable_safety_checker": True,
                "output_format": "png",
                "acceleration": "none",
                "sync_mode": True,
            },
            timeout=90,
            follow_redirects=False,
        )
        return response.status_code == 200 and _valid_canary_image(response.json())
    except (httpx.HTTPError, TypeError, ValueError):
        return False


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603

    api_key = os.environ.get("FAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("fal: FAL_API_KEY is required")
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        catalog_response = client.get(CATALOG_URL, headers=_headers(api_key))
        catalog_response.raise_for_status()
        catalog = catalog_response.json()
        if not _catalog_has_route(catalog):
            raise RuntimeError("fal: FLUX.1 Schnell is not active in the provider catalog")
        pricing_response = client.get(PRICING_URL, headers=_headers(api_key))
        pricing_response.raise_for_status()
        fixed_price = _price_for_1024_square(pricing_response.json())

    rows = {
        MODEL_ID: {
            "id": MODEL_ID,
            "upstream_id": UPSTREAM_ID,
            "display_name": "FLUX.1 Schnell on FAL",
            "model_type": "image",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "endpoints": ["images"],
            "supported_features": ["image-generation"],
            "fixed_output_price_microdollars": {"1k": fixed_price},
            "status": 1,
        }
    }
    checked = models_requiring_canary(MANIFEST_PATH, rows)
    healthy = {MODEL_ID} if MODEL_ID in checked and _probe_generation(api_key) else set()
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    guard_fixed_output_prices(MANIFEST_PATH, rows)
    _DISCOVERED_ROWS = rows

    prices = {MODEL_ID: ModelPrice(0, 0)}
    errors = validate(prices, [MODEL_ID], allow_all_zero=True)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=PRICING_URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
        notes=[f"canaried {len(checked)} new or unhealthy routes; {len(healthy)} passed"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS,
        source_url=CATALOG_URL,
        pricing_source_url=PRICING_URL,
    )
