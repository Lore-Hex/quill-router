"""Featherless current-plan chat catalog with exact authenticated prices."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from scripts.pricing.base import fetch_json
from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "featherless"
BASE_URL = "https://api.featherless.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/featherless.json"
)
MANIFEST_STALE_FALLBACK = True

CURATED_NATIVE_MODELS = (
    "deepseek-ai/DeepSeek-V4-Flash-0731",
    "moonshotai/Kimi-K3",
    "Qwen/Qwen3.8-Flash-Next",
    "zai-org/GLM-5.2",
    "zai-org/GLM-5.3",
    "zai-org/GLM-5.3-Flash",
)

# Featherless serves tens of thousands of community fine-tunes. Scan a bounded
# release window, but only admit first-party model publishers we route elsewhere.
# Required models above remain fail-closed even after they leave this window.
DISCOVERY_NATIVE_OWNERS = frozenset(
    {
        "deepseek-ai",
        "MiniMaxAI",
        "moonshotai",
        "Qwen",
        "XiaomiMiMo",
        "zai-org",
    }
)
DISCOVERY_PAGE_SIZE = 1000


def _is_discovery_candidate(row: dict[str, Any]) -> bool:
    native_id = row.get("id")
    return (
        isinstance(native_id, str)
        and native_id.partition("/")[0] in DISCOVERY_NATIVE_OWNERS
        and row.get("available_on_current_plan") is True
    )


def _load_rows(api_key: str) -> list[dict[str, Any]]:
    query = urlencode(
        {
            "available_on_current_plan": "true",
            "status": "active",
            "sort": "-hf_created_at",
            "page": "1",
            "per_page": str(DISCOVERY_PAGE_SIZE),
        }
    )
    listing = fetch_json(
        f"{URL}?{query}",
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
        raise RuntimeError("featherless: invalid paginated model catalog")

    rows_by_id = {
        str(row["id"]): row
        for row in listing["data"]
        if isinstance(row, dict) and _is_discovery_candidate(row)
    }
    for native_id in CURATED_NATIVE_MODELS:
        payload = fetch_json(
            f"{URL}/{quote(native_id, safe='')}",
            extra_headers={"Authorization": f"Bearer {api_key}"},
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"featherless: invalid model detail for {native_id}")
        if payload.get("available_on_current_plan") is not True:
            raise RuntimeError(f"featherless: {native_id} is not available on current plan")
        rows_by_id[native_id] = payload
    return list(rows_by_id.values())

CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="FEATHERLESS_API_KEY",
        explicit_model_map={},
        expected_models=(
            "deepseek/deepseek-v4-flash-0731",
            "moonshotai/kimi-k3",
            "qwen/qwen3.8-flash-next",
            "z-ai/glm-5.2",
            "z-ai/glm-5.3",
            "z-ai/glm-5.3-flash",
        ),
        catalog_url=URL,
        catalog_loader=_load_rows,
        pricing_source_url="https://featherless.ai/pricing",
        canary_max_tokens=32,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
