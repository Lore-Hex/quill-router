"""Vultr Serverless Inference authenticated catalog and exact token prices."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "vultr"
BASE_URL = "https://api.vultrinference.com/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/vultr.json"
)
MANIFEST_STALE_FALLBACK = True

MODEL_MAP = {
    "nvidia/Nemotron-Cascade-2-30B-A3B": "nvidia/nemotron-cascade-2-30b-a3b",
    "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16": (
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-bf16"
    ),
    "zai-org/GLM-5.2-FP8": "z-ai/glm-5.2",
    "deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    "qwen3.8-27b": "qwen/qwen3.8-27b",
    "minimax-m3": "minimax/minimax-m3",
}


def _price(modalities: object, *, modality_type: str, price_type: str) -> Decimal | None:
    if not isinstance(modalities, list):
        return None
    for modality in modalities:
        if not isinstance(modality, dict) or modality.get("type") != modality_type:
            continue
        prices = modality.get("pricing")
        if not isinstance(prices, list):
            continue
        for row in prices:
            if (
                not isinstance(row, dict)
                or row.get("type") != price_type
                or row.get("unit") != "token"
            ):
                continue
            try:
                value = Decimal(str(row["cost_usd"]))
            except (InvalidOperation, KeyError, TypeError, ValueError):
                return None
            return value if value.is_finite() and value > 0 else None
    return None


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for source in rows:
        native_id = source.get("id")
        if not isinstance(native_id, str) or native_id not in MODEL_MAP:
            continue
        prompt = _price(source.get("input_modalities"), modality_type="text", price_type="prompt")
        completion = _price(
            source.get("output_modalities"), modality_type="text", price_type="completion"
        )
        if prompt is None or completion is None:
            continue
        row = dict(source)
        row["pricing"] = {"prompt": str(prompt), "completion": str(completion)}
        row["input_modalities"] = ["text"]
        row["output_modalities"] = ["text"]
        for modality in source.get("input_modalities", []):
            if not isinstance(modality, dict) or modality.get("type") != "text":
                continue
            context = (modality.get("supported_inputs") or {}).get("max_context_length")
            if isinstance(context, dict) and isinstance(context.get("value"), int):
                row["context_length"] = context["value"]
        for modality in source.get("output_modalities", []):
            if not isinstance(modality, dict) or modality.get("type") != "text":
                continue
            maximum = modality.get("max_length")
            if isinstance(maximum, dict) and isinstance(maximum.get("value"), int):
                row["max_output_tokens"] = maximum["value"]
            parameters = modality.get("supported_parameters")
            if isinstance(parameters, dict):
                features = [
                    feature
                    for parameter, feature in (
                        ("tools", "tools"),
                        ("reasoning", "reasoning"),
                    )
                    if parameter in parameters
                ]
                if features:
                    row["supported_features"] = features
        normalized.append(row)
    return normalized


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="VULTR_API_KEY",
        explicit_model_map=MODEL_MAP,
        expected_models=(
            "deepseek/deepseek-v4-flash-0731",
            "minimax/minimax-m3",
            "qwen/qwen3.8-27b",
            "z-ai/glm-5.2",
        ),
        normalize_rows=_normalize_rows,
        canary_max_tokens=64,
        canary_expected_content="PONG",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
