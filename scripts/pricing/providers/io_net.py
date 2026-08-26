"""IO Intelligence authenticated catalog with exact token prices."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "io-net"
BASE_URL = "https://api.intelligence.io.solutions/api/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/io-net.json"
)
MANIFEST_STALE_FALLBACK = True


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _nonnegative_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate IO's per-token fields into the shared OpenAI catalog shape."""

    normalized: list[dict[str, Any]] = []
    for source in rows:
        prompt = _positive_decimal(source.get("input_token_price"))
        completion = _positive_decimal(source.get("output_token_price"))
        if prompt is None or completion is None:
            continue

        row = dict(source)
        pricing = {
            "prompt": str(prompt),
            "completion": str(completion),
        }
        cached = _nonnegative_decimal(source.get("cache_read_token_price"))
        if cached is not None:
            pricing["input_cache_read"] = str(cached)
        row["pricing"] = pricing

        context_length = source.get("context_window") or source.get("max_model_len")
        if context_length is not None:
            row["context_length"] = context_length
        max_output = source.get("max_tokens")
        if max_output is not None:
            row["max_output_tokens"] = max_output

        features: list[str] = []
        for source_field, feature in (
            ("supports_tools", "tools"),
            ("supports_reasoning", "reasoning"),
            ("supports_prompt_cache", "prompt_cache"),
        ):
            if source.get(source_field) is True:
                features.append(feature)
        if features:
            row["supported_features"] = features
        normalized.append(row)
    return normalized


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env=("IONET_API_KEY", "IO_NET_API_KEY"),
        explicit_model_map={},
        expected_models=(
            "deepseek/deepseek-v4-flash-0731",
            "moonshotai/kimi-k3",
            "z-ai/glm-5.3",
            "z-ai/glm-5.3-flash",
        ),
        normalize_rows=_normalize_rows,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
