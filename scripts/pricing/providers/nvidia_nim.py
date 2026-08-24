"""NVIDIA hosted NIM discovery, held dark until production entitlement exists."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import ModelPrice, ProviderPricingResult, validate

SLUG = "nvidia-nim"
BASE_URL = "https://integrate.api.nvidia.com/v1"
URL = "https://docs.api.nvidia.com/nim/docs/run-anywhere"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/nvidia-nim.json"
)
_DISCOVERED_ROWS: list[dict[str, Any]] = []
INCLUDE_IN_PRICE_INDEX = False
MANIFEST_STALE_FALLBACK = True


def fetch() -> ProviderPricingResult:
    """Authenticate and retain NVIDIA's live catalog without making it routable."""

    global _DISCOVERED_ROWS  # noqa: PLW0603
    key = os.environ.get("NVIDIA_NIM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("nvidia-nim: NVIDIA_NIM_API_KEY is required")
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.get(
            f"{BASE_URL}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        response.raise_for_status()
        payload = response.json()
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raise RuntimeError("nvidia-nim: authenticated catalog has no data list")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_models:
        upstream_id = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(upstream_id, str) or "/" not in upstream_id:
            continue
        model_id = upstream_id.strip().lower()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        rows.append(
            {
                "id": model_id,
                "upstream_id": upstream_id,
                "display_name": upstream_id,
                "model_type": "discovery",
                "routable": False,
                "routable_reason": "production-entitlement-required",
                "status": 1,
            }
        )
    if len(rows) < 10:
        raise RuntimeError("nvidia-nim: live catalog unexpectedly returned fewer than 10 models")
    _DISCOVERED_ROWS = sorted(rows, key=lambda row: str(row["id"]))
    prices = {str(row["id"]): ModelPrice(0, 0) for row in _DISCOVERED_ROWS}
    errors = validate(prices, prices, allow_all_zero=True)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Write discovery evidence without turning preview endpoints into routes."""

    payload = {
        "provider": SLUG,
        "source": f"{BASE_URL}/models",
        "pricing_source": URL,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "model_count": len(_DISCOVERED_ROWS),
        "models": _DISCOVERED_ROWS,
    }
    MANIFEST_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [
        f"{result.slug}: refreshed discovery-only provider manifest "
        f"({len(_DISCOVERED_ROWS)} rows; production entitlement required)"
    ]
