"""Perceptron public catalog with exact per-million token pricing."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "perceptron"
BASE_URL = "https://perceptron.cloud/api/v1"
URL = "https://perceptron.cloud/api/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/perceptron.json"


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for source in rows:
        if source.get("is_free") is True:
            continue
        output = source.get("output_modalities")
        if isinstance(output, list) and "text" not in {str(item).casefold() for item in output}:
            continue
        inputs = source.get("input_modalities")
        if isinstance(inputs, list) and "text" not in {
            str(item).casefold() for item in inputs
        }:
            continue
        pricing = source.get("pricing")
        if not isinstance(pricing, dict):
            continue
        try:
            prompt = Decimal(str(pricing["input_price_per_1m"])) / Decimal(1_000_000)
            completion = Decimal(str(pricing["output_price_per_1m"])) / Decimal(1_000_000)
        except (InvalidOperation, KeyError, TypeError, ValueError):
            continue
        if prompt <= 0 or completion <= 0:
            continue
        row = dict(source)
        row["pricing"] = {"prompt": str(prompt), "completion": str(completion)}
        normalized.append(row)
    return normalized


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        catalog_url=URL,
        api_key_env="PERCEPTRON_API_KEY",
        explicit_model_map={},
        expected_models=("deepseek/deepseek-v4-flash", "minimax/minimax-m3", "z-ai/glm-5.3"),
        normalize_rows=_normalize_rows,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
