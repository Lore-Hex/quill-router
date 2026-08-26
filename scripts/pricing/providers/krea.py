"""Krea asynchronous image generation and first-party fixed pricing."""

from __future__ import annotations

import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    guard_fixed_output_prices,
    models_requiring_canary,
    write_discovered_chat_manifest,
)

SLUG = "krea"
BASE_URL = "https://api.krea.ai"
OPENAPI_URL = f"{BASE_URL}/openapi.json"
MODEL_ID = "krea/krea-2-medium"
UPSTREAM_ID = "krea/krea-2/medium"
GENERATE_PATH = f"/generate/image/{UPSTREAM_ID}"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/krea.json"
)
INCLUDE_IN_PRICE_INDEX = False
MANIFEST_STALE_FALLBACK = True
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def _fixed_text_to_image_price(openapi: object) -> int:
    if not isinstance(openapi, dict):
        raise RuntimeError("krea: OpenAPI document is not an object")
    try:
        operation = openapi["paths"][GENERATE_PATH]["post"]
        pricing = operation["x-krea-pricing"]
        points = pricing["price_points"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("krea: Krea 2 Medium pricing contract is missing") from exc
    if pricing.get("type") != "fixed" or pricing.get("currency") != "USD":
        raise RuntimeError("krea: unsupported pricing contract")
    for point in points if isinstance(points, list) else []:
        if not isinstance(point, dict):
            continue
        dimensions = point.get("dimensions")
        if not isinstance(dimensions, dict) or dimensions.get("k2BillingTier") != "text-to-image":
            continue
        try:
            price = Decimal(str(point["amount"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("krea: invalid text-to-image price") from exc
        microdollars = price * Decimal(1_000_000)
        if not price.is_finite() or price <= 0 or microdollars != microdollars.to_integral_value():
            raise RuntimeError("krea: invalid text-to-image price")
        return int(microdollars)
    raise RuntimeError("krea: text-to-image price point is missing")


def _probe_generation(api_key: str) -> bool:
    """Run one paid canary while a route is new or remains unhealthy."""

    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.post(
                f"{BASE_URL}{GENERATE_PATH}",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "prompt": "A solid black square on a white background",
                    "aspect_ratio": "1:1",
                    "resolution": "1K",
                },
            )
            if response.status_code != 200:
                return False
            payload = response.json()
            job_id = payload.get("job_id") if isinstance(payload, dict) else None
            if not isinstance(job_id, str) or not job_id.strip():
                return False
            for delay in (1, 2, 4, 8, 15, 30, 30):
                time.sleep(delay)
                poll = client.get(
                    f"{BASE_URL}/jobs/{job_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if poll.status_code != 200:
                    if poll.status_code == 429 or poll.status_code >= 500:
                        continue
                    return False
                state = poll.json()
                status = state.get("status") if isinstance(state, dict) else None
                if status == "completed":
                    return True
                if status in {"failed", "cancelled"}:
                    return False
    except (httpx.HTTPError, TypeError, ValueError):
        return False
    return False


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603

    api_key = os.environ.get("KREA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("krea: KREA_API_KEY is required")
    openapi = fetch_json(OPENAPI_URL)
    fixed_price = _fixed_text_to_image_price(openapi)

    # Authentication and catalog access are free. A generation canary is
    # performed only for a new or previously unhealthy route below.
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        auth = client.get(
            f"{BASE_URL}/jobs",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        auth.raise_for_status()

    rows = {
        MODEL_ID: {
            "id": MODEL_ID,
            "upstream_id": UPSTREAM_ID,
            "display_name": "Krea 2 Medium",
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
        fetched_url=OPENAPI_URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
        notes=[
            f"canaried {len(checked)} new or unhealthy routes; {len(healthy)} passed",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS,
        source_url=OPENAPI_URL,
        pricing_source_url=OPENAPI_URL,
    )
