"""Atlas Cloud OpenAI-compatible text model catalog and pricing."""

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
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.openai_catalog import discover_openai_chat_catalog

SLUG = "atlas-cloud"
URL = "https://api.atlascloud.ai/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "atlas-cloud.json"
)
EXPECTED_MODELS = [
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "deepseek/deepseek-v4-flash",
    "minimax/minimax-m3",
]
_NATIVE_TO_MODEL_ID = {
    "deepseek-ai/deepseek-v3.2": "deepseek/deepseek-v3.2",
    "deepseek-ai/deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-ai/deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "minimaxai/minimax-m2.5": "minimax/minimax-m2.5",
    "minimaxai/minimax-m2.7": "minimax/minimax-m2.7",
    "minimaxai/minimax-m3": "minimax/minimax-m3",
    "moonshotai/kimi-k2.5": "moonshotai/kimi-k2.5",
    "moonshotai/kimi-k2.6": "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.7-code": "moonshotai/kimi-k2.7-code",
    "zai-org/glm-4.7": "z-ai/glm-4.7",
    "zai-org/glm-5": "z-ai/glm-5",
    "zai-org/glm-5.1": "z-ai/glm-5.1",
    "zai-org/glm-5.2": "z-ai/glm-5.2",
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen/qwen3-235b-a22b-2507",
}
UPSTREAM_ID_MAP = {
    model_id: native_id for native_id, model_id in _NATIVE_TO_MODEL_ID.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _supports_chat_completions(row: dict[str, Any]) -> bool:
    """Exclude Atlas image generators that its feed mislabels as text output."""

    model_id = row.get("id")
    return isinstance(model_id, str) and "image" not in model_id.casefold()


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("ATLAS_CLOUD_API_KEY")
    if not api_key:
        raise RuntimeError("ATLAS_CLOUD_API_KEY is required for model discovery")
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
        include=_supports_chat_completions,
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
        notes=[f"discovered {len(discovered)} priced text-chat models"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
