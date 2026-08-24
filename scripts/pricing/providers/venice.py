"""Venice authenticated text catalog with provider-native pricing."""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json, validate
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import canonicalize_unqualified_model_id
from scripts.pricing.openai_catalog import positive_int

SLUG = "venice"
BASE_URL = "https://api.venice.ai/api/v1"
URL = f"{BASE_URL}/models?type=text"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "venice.json"
)
EXPECTED_MODELS = ["z-ai/glm-5.2"]

UPSTREAM_ID_MAP = {
    "z-ai/glm-5.2": "zai-org-glm-5-2",
    "z-ai/glm-5.1": "zai-org-glm-5-1",
    "z-ai/glm-5": "zai-org-glm-5",
    "z-ai/glm-5-turbo": "z-ai-glm-5-turbo",
    "z-ai/glm-5v-turbo": "z-ai-glm-5v-turbo",
    "z-ai/glm-4.7-flash": "zai-org-glm-4.7-flash",
    "z-ai/glm-4.7": "zai-org-glm-4.7",
    "z-ai/glm-4.6": "zai-org-glm-4.6",
    "qwen/qwen3.6-27b": "qwen3-6-27b",
    "qwen/qwen3.5-9b": "qwen3-5-9b",
    "qwen/qwen3.5-397b-a17b": "qwen3-5-397b-a17b",
    "qwen/qwen3-235b-a22b-thinking-2507": "qwen3-235b-a22b-thinking-2507",
    "qwen/qwen3-235b-a22b-instruct-2507": "qwen3-235b-a22b-instruct-2507",
    "qwen/qwen3-next-80b": "qwen3-next-80b",
    "qwen/qwen3-vl-235b-a22b": "qwen3-vl-235b-a22b",
    "qwen/qwen3-coder-480b-a35b-instruct-turbo": (
        "qwen3-coder-480b-a35b-instruct-turbo"
    ),
    "nvidia/nemotron-3.5-lightning": "nvidia-nemotron-3-5-lightning-30b-a3b",
}
_NATIVE_TO_MODEL_ID = {native: model_id for model_id, native in UPSTREAM_ID_MAP.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _model_id(native_id: str) -> str | None:
    return _NATIVE_TO_MODEL_ID.get(native_id) or canonicalize_unqualified_model_id(
        native_id
    )


def _usd_per_million(value: object) -> int | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return int((parsed * Decimal(1_000_000)).to_integral_value(ROUND_HALF_UP))


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    api_key = os.environ.get("VENICE_API_KEY")
    if not api_key:
        raise RuntimeError("VENICE_API_KEY is required for model discovery")
    payload = fetch_json(
        URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in rows:
        native_id = source.get("id")
        spec = source.get("model_spec")
        if not isinstance(native_id, str) or not isinstance(spec, dict):
            continue
        if spec.get("offline") is True:
            continue
        model_id = _model_id(native_id)
        pricing = spec.get("pricing")
        if model_id is None or not isinstance(pricing, dict):
            continue
        input_rate = pricing.get("input")
        output_rate = pricing.get("output")
        cache_rate = pricing.get("cache_input")
        prompt = _usd_per_million(
            input_rate.get("usd") if isinstance(input_rate, dict) else None
        )
        completion = _usd_per_million(
            output_rate.get("usd") if isinstance(output_rate, dict) else None
        )
        cached = _usd_per_million(
            cache_rate.get("usd") if isinstance(cache_rate, dict) else None
        )
        if prompt is None or completion is None:
            continue
        prices[model_id] = ModelPrice(
            prompt,
            completion,
            prompt_cached_micro_per_m=cached,
        )
        UPSTREAM_ID_MAP[model_id] = native_id
        capabilities = spec.get("capabilities")
        capability_map = capabilities if isinstance(capabilities, dict) else {}
        features = [
            feature
            for field, feature in (
                ("supportsFunctionCalling", "function-calling"),
                ("supportsReasoning", "reasoning"),
                ("supportsResponseSchema", "structured-outputs"),
            )
            if capability_map.get(field) is True
        ]
        discovered_row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(spec.get("name") or native_id),
            "endpoints": ["chat/completions"],
            "input_modalities": (
                ["text", "image"]
                if capability_map.get("supportsVision") is True
                else ["text"]
            ),
            "output_modalities": ["text"],
        }
        if features:
            discovered_row["features"] = features
        context_length = positive_int(
            spec.get("availableContextTokens") or source.get("context_length")
        )
        if context_length is not None:
            discovered_row["context_length"] = context_length
        max_output = positive_int(spec.get("maxCompletionTokens"))
        if max_output is not None:
            discovered_row["max_output_tokens"] = max_output
        discovered[model_id] = discovered_row

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[f"discovered {len(discovered)} online, priced text models"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
