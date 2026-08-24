"""Pearl Research authenticated model catalog, pricing, and canaries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import (
    discover_openai_chat_catalog,
    probe_openai_chat,
)

SLUG = "pearl"
BASE_URL = "https://inference.pearlresearch.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "pearl.json"
)
EXPECTED_MODELS = [
    "google/gemma-4-31b-it",
    "z-ai/glm-5.2",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-flash",
]
EXPLICIT_MODEL_MAP: dict[str, str] = {}
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _parse_catalog(
    payload: object,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("pearl: authenticated /models response has no data list")
    source_rows = [row for row in rows if isinstance(row, dict)]
    prices, discovered = discover_openai_chat_catalog(
        source_rows,
        explicit_map=EXPLICIT_MODEL_MAP,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    for model_id, row in discovered.items():
        row["model_type"] = "chat"
        row["status"] = 1
        source_features = {
            str(feature)
            for feature in row.get("supported_features", [])
            if isinstance(feature, str)
        }
        features = ["chat", "completion"]
        for feature in ("tools", "json_mode", "reasoning"):
            if feature in source_features:
                features.append(feature)
        if "json_mode" in source_features:
            features.append("structured_outputs")
        price = prices.get(model_id)
        if price is not None and price.tiers[0].prompt_cached_micro_per_m is not None:
            features.append("prompt_caching")
        row["supported_features"] = features
    return prices, discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    api_key = os.environ.get("PEARL_RESEARCH_API_KEY")
    if not api_key:
        raise RuntimeError("pearl: PEARL_RESEARCH_API_KEY is required for discovery")
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(
            URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": PROVIDER_FETCH_UA,
            },
        )
        response.raise_for_status()
        payload = response.json()

    prices, discovered = _parse_catalog(payload)
    checked = models_requiring_canary(MANIFEST_PATH, discovered)
    healthy = {
        model_id
        for model_id in checked
        if probe_openai_chat(
            base_url=BASE_URL,
            api_key=api_key,
            model=UPSTREAM_ID_MAP[model_id],
            expected_content="PONG",
            max_tokens=256,
        )
    }
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[
            f"discovered {len(discovered)} Pearl text models with exact API pricing",
            f"canaried {len(checked)} new or previously unhealthy routes; {len(healthy)} passed",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
