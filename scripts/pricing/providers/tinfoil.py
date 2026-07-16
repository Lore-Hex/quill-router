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
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id

SLUG = "tinfoil"
URL = "https://inference.tinfoil.sh/v1/models"

EXPECTED_MODELS = [
    "moonshotai/kimi-k2.6",
    "z-ai/glm-5.2",
    "google/gemma-4-31b-it",
]

# Tinfoil-native id → TR-canonical id. Most are renamings to align
# with OR's `vendor/model-name` convention. Tinfoil also wraps each
# model in their TEE attestation pipeline, but the underlying weights
# match the upstream model.
_NATIVE_TO_OR_ID = {
    "kimi-k2-6": "moonshotai/kimi-k2.6",
    "kimi-k2-7-code": "moonshotai/kimi-k2.7-code",
    "glm-5-1": "z-ai/glm-5.1",
    "glm-5-2": "z-ai/glm-5.2",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "gemma4-31b": "google/gemma-4-31b-it",
    "qwen3-vl-30b": "qwen/qwen3-vl-30b-a3b-instruct",
    "llama3-3-70b": "meta-llama/llama-3.3-70b-instruct",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "voxtral-small-24b": "mistralai/voxtral-small-24b",
    "whisper-large-v3-turbo": "openai/whisper-large-v3-turbo",
    "qwen3-tts": "qwen/qwen3-tts",
    "nomic-embed-text": "nomic-ai/nomic-embed-text",
}
UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}


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
        or_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if or_id is None:
            notes.append(f"unmapped native id: {native_id}")
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        try:
            input_usd = float(pricing.get("inputTokenPricePer1M"))
            output_usd = float(pricing.get("outputTokenPricePer1M"))
        except (TypeError, ValueError):
            continue
        cached_input_usd: float | None = None
        try:
            cached_raw = pricing.get("cachedInputTokenPricePer1M")
            if cached_raw is not None:
                cached_input_usd = float(cached_raw)
        except (TypeError, ValueError):
            notes.append(f"invalid cached-input price for {native_id}: {cached_raw!r}")
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=int(round(input_usd * 1_000_000)),
            completion_micro_per_m=int(round(output_usd * 1_000_000)),
            prompt_cached_micro_per_m=(
                int(round(cached_input_usd * 1_000_000))
                if cached_input_usd is not None
                else None
            ),
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
