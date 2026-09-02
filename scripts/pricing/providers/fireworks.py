"""Fireworks AI catalog and first-party pricing integration.

Fireworks publishes a first-party serverless pricing table for its headline
models. We fetch that docs page and parse the standard serving-path prices.
Prices become routable only when the authenticated operator model list also
contains the model. The supplemental manifest is rebuilt from that intersection
so newly published, priced chat models are added automatically.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ProviderPricingResult,
    fetch_json,
    fetch_provider,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import (
    canonicalize_unqualified_model_id,
    price_aliases_for_versioned_families,
    remember_upstream_id,
)
from scripts.pricing.openai_catalog import discover_available_priced_chat_catalog
from trusted_router.provider_lifecycle import provider_model_retired

SLUG = "fireworks"
URL = "https://docs.fireworks.ai/serverless/pricing.md"
MODELS_URL = "https://api.fireworks.ai/inference/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "fireworks.json"
)

EXPECTED_MODELS = [
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.7-code",
    "deepseek/deepseek-v4-pro-0813",
    "deepseek/deepseek-v4-flash-0731",
    "z-ai/glm-5.2",
    "z-ai/glm-5.2-fast",
    "openai/gpt-oss-120b",
    "meta-models/muse-glimmer-30b",
    "minimax/minimax-m3",
    "nvidia/nemotron-3.5-lightning",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "qwen/qwen3.8-max",
]

_DISPLAY_NAMES = {
    "deepseek/deepseek-v4-flash-0731": "DeepSeek V4 Flash 0731",
    "deepseek/deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek/deepseek-v4-pro-0813": "DeepSeek V4 Pro 0813",
    "minimax/minimax-m2.7": "MiniMax M2.7",
    "minimax/minimax-m3": "MiniMax M3",
    "meta-models/muse-glimmer-30b": "Muse Glimmer 30B",
    "moonshotai/kimi-k2.6": "Kimi K2.6",
    "moonshotai/kimi-k2.6-fast": "Kimi K2.6 Fast",
    "moonshotai/kimi-k2.7-code": "Kimi K2.7 Code",
    "moonshotai/kimi-k2.7-code-fast": "Kimi K2.7 Code Fast",
    "moonshotai/kimi-k3": "Kimi K3",
    "moonshotai/kimi-k3-fast": "Kimi K3 Fast",
    "nvidia/nemotron-3.5-lightning": "NVIDIA Nemotron 3.5 Lightning",
    "nvidia/nemotron-3-ultra-550b-a55b": "NVIDIA Nemotron 3 Ultra",
    "openai/gpt-oss-20b": "OpenAI GPT OSS 20B",
    "openai/gpt-oss-120b": "OpenAI GPT OSS 120B",
    "qwen/qwen3.7-plus": "Qwen 3.7 Plus",
    "qwen/qwen3.8-max": "Qwen 3.8 Max",
    "z-ai/glm-5.2": "GLM 5.2",
    "z-ai/glm-5.2-fast": "GLM 5.2 Fast",
    "z-ai/glm-5.3-flash": "GLM 5.3 Flash",
}

_NATIVE_TO_CANONICAL = {
    "accounts/fireworks/models/kimi-k3": "moonshotai/kimi-k3",
    "accounts/fireworks/models/kimi-k2p6": "moonshotai/kimi-k2.6",
    "accounts/fireworks/models/kimi-k2p7-code": "moonshotai/kimi-k2.7-code",
    "accounts/fireworks/routers/kimi-k3-fast": "moonshotai/kimi-k3-fast",
    "accounts/fireworks/routers/kimi-k2p6-turbo": "moonshotai/kimi-k2.6-fast",
    "accounts/fireworks/routers/kimi-k2p7-code-fast": "moonshotai/kimi-k2.7-code-fast",
    "accounts/fireworks/models/deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "accounts/fireworks/models/deepseek-v4-pro-0813": "deepseek/deepseek-v4-pro-0813",
    "accounts/fireworks/models/deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "accounts/fireworks/models/deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    "accounts/fireworks/models/glm-5p2": "z-ai/glm-5.2",
    "accounts/fireworks/routers/glm-5p2-fast": "z-ai/glm-5.2-fast",
    "accounts/fireworks/models/glm-5p3-flash": "z-ai/glm-5.3-flash",
    "accounts/fireworks/models/glm-5p1": "z-ai/glm-5.1",
    "accounts/fireworks/models/gpt-oss-120b": "openai/gpt-oss-120b",
    "accounts/fireworks/models/gpt-oss-20b": "openai/gpt-oss-20b",
    "accounts/fireworks/models/minimax-m3": "minimax/minimax-m3",
    "accounts/fireworks/models/minimax-m2p7": "minimax/minimax-m2.7",
    "accounts/fireworks/models/qwen3p7-plus": "qwen/qwen3.7-plus",
    "accounts/fireworks/models/qwen3p8-max": "qwen/qwen3.8-max",
    "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b": (
        "nvidia/nemotron-3.5-lightning"
    ),
    "accounts/fireworks/models/nemotron-3-ultra-nvfp4": (
        "nvidia/nemotron-3-ultra-550b-a55b"
    ),
    "accounts/fireworks/models/muse-glimmer-30b": "meta-models/muse-glimmer-30b",
}
UPSTREAM_ID_MAP = {canonical: native for native, canonical in _NATIVE_TO_CANONICAL.items()}
# Fireworks can publish and serve a launch model before its authenticated
# /v1/models response catches up. Keep only explicitly verified exceptions,
# and only while the first-party pricing page still contains the model.
VERIFIED_PRICED_LAUNCH_MODELS = frozenset({"moonshotai/kimi-k3"})
_VISION_MODEL_IDS = frozenset(
    {
        "moonshotai/kimi-k3",
        "moonshotai/kimi-k3-fast",
        "qwen/qwen3.8-max",
    }
)
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_VERSIONED_PRICE_FAMILIES = {
    "deepseek/deepseek-v4-flash-": "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro-": "deepseek/deepseek-v4-pro",
}
_PRESERVE_UNPRICED_MODEL_IDS = frozenset({"z-ai/glm-5.3-flash"})


def _live_model_rows() -> list[dict[str, Any]]:
    api_key = os.environ.get("FIREWORKS_API_KEY") or os.environ.get("FIREWORKS_AI_API_KEY")
    if not api_key:
        raise RuntimeError("fireworks: FIREWORKS_API_KEY is required")
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("fireworks: /v1/models response has no data list")
    return [row for row in rows if isinstance(row, dict)]


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS

    source_rows = _live_model_rows()
    live_model_ids: set[str] = set()
    live_rows: list[dict[str, Any]] = []
    resolved_native_map = dict(_NATIVE_TO_CANONICAL)
    for row in source_rows:
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        canonical = resolved_native_map.get(native_id) or canonicalize_unqualified_model_id(
            native_id
        )
        if canonical is None or provider_model_retired(SLUG, canonical, native_id):
            continue
        resolved_native_map[native_id] = canonical
        live_model_ids.add(canonical)
        live_rows.append(row)
        remember_upstream_id(UPSTREAM_ID_MAP, canonical, native_id)

    price_aliases = price_aliases_for_versioned_families(
        live_model_ids,
        _VERSIONED_PRICE_FAMILIES,
    )
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=frozenset(price_aliases),
        required_model_price_aliases=price_aliases,
    )
    verified_launch_ids = VERIFIED_PRICED_LAUNCH_MODELS.intersection(result.prices)
    routable_model_ids = live_model_ids | verified_launch_ids
    docs_only = sorted(set(result.prices) - routable_model_ids)
    result.prices = {
        model_id: price
        for model_id, price in result.prices.items()
        if model_id in routable_model_ids
    }
    discovered = discover_available_priced_chat_catalog(
        live_rows,
        prices=result.prices,
        explicit_map=resolved_native_map,
        upstream_id_map=UPSTREAM_ID_MAP,
        preserve_unpriced_model_ids=_PRESERVE_UNPRICED_MODEL_IDS,
    )
    # A verified launch exception is allowed to precede the account model-list
    # feed, but only while the first-party pricing page still publishes it.
    for model_id in verified_launch_ids - set(discovered):
        upstream_id = UPSTREAM_ID_MAP.get(model_id)
        if upstream_id is None:
            continue
        discovered[model_id] = {
            "id": model_id,
            "upstream_id": upstream_id,
            "display_name": model_id,
            "endpoints": ["chat/completions"],
        }
    for model_id in _VISION_MODEL_IDS:
        if model_id in discovered:
            discovered[model_id]["input_modalities"] = ["text", "image"]
    for model_id, row in discovered.items():
        fallback_name = model_id.rsplit("/", 1)[-1].replace("-", " ").title()
        row["display_name"] = f"{_DISPLAY_NAMES.get(model_id, fallback_name)} on Fireworks"
    _DISCOVERED_MANIFEST_ROWS = discovered
    errors = validate(result.prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    if docs_only:
        result.notes.append(
            "official pricing rows not enabled for this Fireworks account: " + ", ".join(docs_only)
        )
    launch_ids_missing_from_catalog = sorted(verified_launch_ids - live_model_ids)
    if launch_ids_missing_from_catalog:
        result.notes.append(
            "verified launch models served before /v1/models catalog update: "
            + ", ".join(launch_ids_missing_from_catalog)
        )
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Publish the fresh intersection of Fireworks availability and prices."""

    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
        pricing_source_url=URL,
    )
