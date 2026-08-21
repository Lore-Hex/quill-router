"""Poolside authenticated model catalog, pricing, and route canaries."""

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

SLUG = "poolside"
BASE_URL = "https://inference.poolside.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "poolside.json"
)
EXPLICIT_MODEL_MAP = {
    "poolside/laguna-s-2.1": "poolside/laguna-s-2.1",
    "poolside/laguna-xs-2.1": "poolside/laguna-xs-2.1",
}
EXPECTED_MODELS = list(EXPLICIT_MODEL_MAP.values())
UPSTREAM_ID_MAP = {
    model_id: native_id for native_id, model_id in EXPLICIT_MODEL_MAP.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_CHECKED_CANARY_IDS: frozenset[str] = frozenset()
_HEALTHY_CANARY_IDS: frozenset[str] = frozenset()


def _parse_catalog(
    payload: object,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("poolside: authenticated /models response has no data list")
    source_rows = [row for row in rows if isinstance(row, dict)]
    prices, discovered = discover_openai_chat_catalog(
        source_rows,
        explicit_map=EXPLICIT_MODEL_MAP,
        upstream_id_map=UPSTREAM_ID_MAP,
        allow_zero_prices=True,
    )
    source_by_id = {
        str(row.get("id")): row
        for row in source_rows
        if isinstance(row.get("id"), str)
    }
    for model_id, row in discovered.items():
        price = prices.get(model_id)
        source = source_by_id.get(str(row.get("upstream_id")), {})
        if price is None:
            raise RuntimeError(f"poolside: {model_id} has no parseable price")
        all_zero = all(
            tier.prompt_micro_per_m == 0
            and tier.completion_micro_per_m == 0
            and tier.prompt_cached_micro_per_m in {None, 0}
            for tier in price.tiers
        )
        if all_zero and source.get("is_free") is not True:
            raise RuntimeError(
                f"poolside: refusing zero-price route without is_free=true: {model_id}"
            )
        row["model_type"] = "chat"
        row["status"] = 1
        features = ["chat", "completion"]
        advertised = {
            str(value).casefold()
            for value in source.get("supported_features", [])
            if isinstance(value, str)
        }
        if "reasoning" in advertised:
            features.append("reasoning")
        if "tools" in advertised:
            features.append("tools")
        if price.tiers[0].prompt_cached_micro_per_m is not None:
            features.append("prompt_caching")
        row["supported_features"] = features
    return prices, discovered


def fetch() -> ProviderPricingResult:
    global _CHECKED_CANARY_IDS, _DISCOVERED_MANIFEST_ROWS, _HEALTHY_CANARY_IDS

    api_key = os.environ.get("POOLSIDE_API_KEY")
    if not api_key:
        raise RuntimeError("poolside: POOLSIDE_API_KEY is required for model discovery")
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
            prompt="Reply with exactly PONG and nothing else.",
            # Laguna XS can spend ~200 tokens in reasoning before emitting
            # three visible PONG tokens. A tiny canary budget yields a valid
            # HTTP 200 with empty visible content and is not a healthy route.
            max_tokens=512,
        )
    }
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    _CHECKED_CANARY_IDS = frozenset(checked)
    _HEALTHY_CANARY_IDS = frozenset(healthy)
    errors = validate(
        prices,
        EXPECTED_MODELS,
        allow_authoritative_all_zero=True,
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[
            f"discovered {len(discovered)} Poolside text models with exact API pricing",
            f"canaries passed {len(healthy)}/{len(checked)} checked routes",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
