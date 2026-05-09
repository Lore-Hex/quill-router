"""Tinfoil — human-only provider config.

Tinfoil's `/v1/models` endpoint at inference.tinfoil.sh returns full
JSON with per-model pricing inline:

    {"id": "kimi-k2-6", "pricing": {"inputTokenPricePer1M": 1.5,
                                    "outputTokenPricePer1M": 5.25, ...}}

So we bypass the parser tier entirely (same shape as the Together
adapter) and translate native ids → OR-canonical inline.

OpenAI-compatible chat completions at inference.tinfoil.sh/v1.
"""
from __future__ import annotations

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    validate,
)

SLUG = "tinfoil"
URL = "https://inference.tinfoil.sh/v1/models"

EXPECTED_MODELS = [
    "moonshotai/kimi-k2.6",
]

# Tinfoil-native id → TR-canonical id. Most are renamings to align
# with OR's `vendor/model-name` convention. Tinfoil also wraps each
# model in their TEE attestation pipeline, but the underlying weights
# match the upstream model.
_NATIVE_TO_OR_ID = {
    "kimi-k2-6": "moonshotai/kimi-k2.6",
    "glm-5-1": "z-ai/glm-5.1",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "gemma4-31b": "google/gemma-4-31b",
    "qwen3-vl-30b": "qwen/qwen3-vl-30b",
    "llama3-3-70b": "meta-llama/llama-3.3-70b-instruct",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "voxtral-small-24b": "mistralai/voxtral-small-24b",
    "whisper-large-v3-turbo": "openai/whisper-large-v3-turbo",
    "qwen3-tts": "qwen/qwen3-tts",
    "nomic-embed-text": "nomic-ai/nomic-embed-text",
}


def fetch() -> ProviderPricingResult:
    payload = fetch_json(URL)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RuntimeError("tinfoil: /v1/models returned unexpected shape")
    prices: dict[str, ModelPrice] = {}
    notes: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        or_id = _NATIVE_TO_OR_ID.get(native_id)
        if or_id is None:
            notes.append(f"unmapped native id: {native_id}")
            continue
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        try:
            input_usd = float(pricing.get("inputTokenPricePer1M"))
            output_usd = float(pricing.get("outputTokenPricePer1M"))
        except (TypeError, ValueError):
            continue
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=int(round(input_usd * 1_000_000)),
            completion_micro_per_m=int(round(output_usd * 1_000_000)),
        )

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )
