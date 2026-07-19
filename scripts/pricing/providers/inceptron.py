"""Inceptron serverless model catalog and pricing."""

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
from scripts.pricing.openai_catalog import (
    discover_openai_chat_catalog,
    probe_openai_chat,
)

SLUG = "inceptron"
URL = "https://api.inceptron.io/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "inceptron.json"
)
EXPECTED_MODELS = [
    "minimax/minimax-m2.5",
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.7-code",
]
_NATIVE_TO_MODEL_ID = {
    "MiniMaxAI/MiniMax-M2.5": "minimax/minimax-m2.5",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
}
UPSTREAM_ID_MAP = {
    model_id: native_id for native_id, model_id in _NATIVE_TO_MODEL_ID.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_OK = False


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _LIVE_CANARY_OK  # noqa: PLW0603

    api_key = os.environ.get("INCEPTRON_API_KEY")
    if not api_key:
        raise RuntimeError("INCEPTRON_API_KEY is required for model discovery")
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

    rows = payload.get("data") if isinstance(payload, dict) else payload
    source_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    prices, discovered = discover_openai_chat_catalog(
        source_rows,
        explicit_map=_NATIVE_TO_MODEL_ID,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    _LIVE_CANARY_OK = probe_openai_chat(
        base_url="https://api.inceptron.io/v1",
        api_key=api_key,
        model="MiniMaxAI/MiniMax-M2.5",
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
            f"discovered {len(discovered)} priced chat models",
            "manual funding required; provider has no autopay",
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
