"""Neurometric canonical model catalog, pricing, and account canary."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.manifest import (
    set_manifest_canary_state,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import probe_openai_chat
from scripts.pricing.provider_contract_catalog import (
    discover_provider_contract_catalog,
)

SLUG = "neurometric"
BASE_URL = "https://wharf.neurometric.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "neurometric.json"
)
EXPECTED_MODELS = ["ibm-granite/granite-4.1-8b"]
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_OK = False


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _LIVE_CANARY_OK  # noqa: PLW0603

    api_key = os.environ.get("NEUROMETRIC_API_KEY")
    if not api_key:
        raise RuntimeError("NEUROMETRIC_API_KEY is required for model discovery")
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

    prices, discovered = discover_provider_contract_catalog(
        payload,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    canary_model = UPSTREAM_ID_MAP.get(EXPECTED_MODELS[0], EXPECTED_MODELS[0])
    _LIVE_CANARY_OK = probe_openai_chat(
        base_url=BASE_URL,
        api_key=api_key,
        model=canary_model,
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
            f"validated canonical provider contract with {len(discovered)} active chat models",
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
