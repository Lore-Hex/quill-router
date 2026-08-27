"""Engy authenticated model catalog, exact pricing, and account canary."""

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
    set_manifest_canary_state,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import canonicalize_unqualified_model_id
from scripts.pricing.openai_catalog import (
    discover_openai_chat_catalog,
    probe_openai_chat,
)

SLUG = "engy"
BASE_URL = "https://api.engy.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "engy.json"
)
EXPLICIT_MODEL_MAP = {
    "glm-5.2": "z-ai/glm-5.2",
    "glm-5.3-flash": "z-ai/glm-5.3-flash",
    "qwen3.6-35b-a3b": "qwen/qwen3.6-35b-a3b",
}
EXPECTED_MODELS = list(EXPLICIT_MODEL_MAP.values())
UPSTREAM_ID_MAP = {model_id: native_id for native_id, model_id in EXPLICIT_MODEL_MAP.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_OK = False


def _parse_catalog(
    payload: object,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("engy: authenticated /models response has no data list")
    source_rows = [row for row in rows if isinstance(row, dict)]
    live_model_map = dict(EXPLICIT_MODEL_MAP)
    for row in source_rows:
        if str(row.get("owned_by") or "").casefold() != "engy":
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        canonical_id = canonicalize_unqualified_model_id(native_id)
        if canonical_id is not None:
            live_model_map.setdefault(native_id, canonical_id)
    prices, discovered = discover_openai_chat_catalog(
        source_rows,
        explicit_map=live_model_map,
        upstream_id_map=UPSTREAM_ID_MAP,
        include=lambda row: str(row.get("owned_by") or "").casefold() == "engy",
    )
    for row in discovered.values():
        row["model_type"] = "chat"
        row["status"] = 1
        row["routable"] = True
        features = ["chat", "completion", "reasoning"]
        if row.get("upstream_id") in {"glm-5.2", "glm-5.3-flash"}:
            # Verified against Engy's live API with required function calling
            # and strict json_schema responses. Qwen currently ignores both,
            # so capability publication stays model-specific and fail-closed.
            features.extend(["tools", "json_mode", "structured_outputs"])
        model_price = prices.get(str(row["id"]))
        if model_price is not None and model_price.tiers[0].prompt_cached_micro_per_m is not None:
            features.append("prompt_caching")
        row["supported_features"] = features
    return prices, discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _LIVE_CANARY_OK  # noqa: PLW0603

    api_key = os.environ.get("ENGY_API_KEY")
    if not api_key:
        raise RuntimeError("engy: ENGY_API_KEY is required for model discovery")
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
    _DISCOVERED_MANIFEST_ROWS = discovered
    canary_model = UPSTREAM_ID_MAP[EXPECTED_MODELS[0]]
    _LIVE_CANARY_OK = probe_openai_chat(
        base_url=BASE_URL,
        api_key=api_key,
        model=canary_model,
        max_tokens=16,
    )
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[
            f"discovered {len(discovered)} Engy text models with exact API pricing",
            f"account canary {'passed' if _LIVE_CANARY_OK else 'failed'}",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
    set_manifest_canary_state(MANIFEST_PATH, healthy=_LIVE_CANARY_OK)
    return notes
