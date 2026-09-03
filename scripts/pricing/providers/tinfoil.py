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

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from trusted_router.provider_lifecycle import provider_model_retired

SLUG = "tinfoil"
URL = "https://inference.tinfoil.sh/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "tinfoil.json"
)

EXPECTED_MODELS = [
    "deepseek/deepseek-v4-flash",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
    "google/gemma-4-31b-it",
]

# Tinfoil-native id → TR-canonical id. Most are renamings to align
# with OR's `vendor/model-name` convention. Tinfoil also wraps each
# model in their TEE attestation pipeline, but the underlying weights
# match the upstream model.
_NATIVE_TO_OR_ID = {
    "kimi-k2-6": "moonshotai/kimi-k2.6",
    "kimi-k2-7-code": "moonshotai/kimi-k2.7-code",
    # Kimi K3 launched on Tinfoil's public API on 2026-08-20. Keeping the
    # native ID explicit prevents a future generic-id normalization change
    # from silently dropping this confidential route.
    "kimi-k3": "moonshotai/kimi-k3",
    "glm-5-1": "z-ai/glm-5.1",
    "glm-5-2": "z-ai/glm-5.2",
    "glm-5-3": "z-ai/glm-5.3",
    "glm-5-3-flash": "z-ai/glm-5.3-flash",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
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
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}

# Only variants whose canonical spelling is stable may bypass manual review.
# Quantization/build suffixes (for example ``-fp8`` or a date) can describe a
# deployment rather than a distinct public model and must stay review-gated.
_GLM_NATIVE_ID_RE = re.compile(r"^glm-(\d+)-(\d+)(?:-(flash|air|fast|turbo))?$")
_ANNOUNCED_FEED_REMOVALS = (("z-ai/glm-5.2", "glm-5-2"),)
_PRESERVED_MANIFEST_FIELDS = frozenset(
    {
        "id",
        "upstream_id",
        "display_name",
        "title",
        "model_type",
        "features",
        "input_modalities",
        "output_modalities",
        "endpoints",
        "status",
        "context_length",
        "max_output_tokens",
        # A model may have been held by a canary or operator after the last
        # successful discovery. Preserving a delisted-but-announced route must
        # never clear that safety state on the next refresh.
        "routable",
        "routable_reason",
        "unresolved_since",
    }
)


def _canonical_model_id(native_id: str) -> str | None:
    """Map reviewed IDs and safely recognize future Tinfoil GLM releases."""

    normalized = native_id.strip().casefold()
    explicit = _NATIVE_TO_OR_ID.get(normalized)
    if explicit is not None:
        return explicit
    match = _GLM_NATIVE_ID_RE.fullmatch(normalized)
    if match is not None:
        major, minor, variant = match.groups()
        suffix = f"-{variant}" if variant else ""
        return f"z-ai/glm-{major}.{minor}{suffix}"
    return mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)


def _preserve_announced_feed_removals(
    prices: dict[str, ModelPrice],
    discovered: dict[str, dict[str, Any]],
    notes: list[str],
) -> None:
    """Keep announced, still-callable routes until their lifecycle cutoff.

    Tinfoil can remove a model from ``/v1/models`` before its stated API
    retirement. Preserve the committed, last-known-good price and metadata
    during that migration window; the shared lifecycle policy removes the
    route at the exact cutoff even if this manifest remains unchanged.
    """

    pending = [
        (model_id, upstream_id)
        for model_id, upstream_id in _ANNOUNCED_FEED_REMOVALS
        if model_id not in prices
        and not provider_model_retired(SLUG, model_id, upstream_id)
    ]
    if not pending:
        return
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        notes.append(f"could not preserve announced feed removals: {type(exc).__name__}")
        return
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        notes.append("could not preserve announced feed removals: invalid manifest")
        return
    existing = {
        row.get("id"): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    for model_id, upstream_id in pending:
        row = existing.get(model_id)
        if not isinstance(row, dict):
            notes.append(f"announced feed removal missing from manifest: {model_id}")
            continue
        prompt = row.get("input_token_price_per_m")
        completion = row.get("output_token_price_per_m")
        cached = row.get("cached_input_token_price_per_m")
        if (
            not isinstance(prompt, int)
            or isinstance(prompt, bool)
            or not isinstance(completion, int)
            or isinstance(completion, bool)
            or (cached is not None and (not isinstance(cached, int) or isinstance(cached, bool)))
        ):
            notes.append(f"announced feed removal has invalid manifest price: {model_id}")
            continue
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        )
        discovered[model_id] = {
            key: value for key, value in row.items() if key in _PRESERVED_MANIFEST_FIELDS
        }
        discovered[model_id]["upstream_id"] = upstream_id
        notes.append(
            f"preserved {model_id} with last-known-good manifest pricing "
            "until its announced retirement"
        )


def _microdollars_per_million(raw: object) -> int | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return int((value * Decimal(1_000_000)).to_integral_value(ROUND_HALF_UP))


def _chat_manifest_row(
    source: dict[str, Any],
    *,
    model_id: str,
    upstream_id: str,
) -> dict[str, Any] | None:
    model_type = source.get("type")
    if model_type not in (None, "chat"):
        return None
    raw_endpoints = source.get("endpoints")
    endpoint_names = (
        {str(endpoint) for endpoint in raw_endpoints} if isinstance(raw_endpoints, list) else set()
    )
    if endpoint_names and "/v1/chat/completions" not in endpoint_names:
        return None

    endpoints = ["chat/completions"]
    if "/v1/responses" in endpoint_names:
        endpoints.append("responses")
    features: list[str] = []
    if source.get("reasoning") is True:
        features.append("reasoning")
    if source.get("tool_calling") is True:
        features.append("function-calling")
    multimodal = source.get("multimodal") is True
    if multimodal:
        features.append("multimodal")

    row: dict[str, Any] = {
        "id": model_id,
        "upstream_id": upstream_id,
        "display_name": str(source.get("name") or upstream_id),
        "title": model_id,
        "model_type": "chat",
        "features": features,
        "input_modalities": ["text", "image"] if multimodal else ["text"],
        "output_modalities": ["text"],
        "endpoints": endpoints,
        "status": 1,
    }
    context_window = source.get("context_window")
    if isinstance(context_window, int) and not isinstance(context_window, bool):
        if context_window > 0:
            row["context_length"] = context_window
    return row


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    payload = fetch_json(URL)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RuntimeError("tinfoil: /v1/models returned unexpected shape")
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        or_id = _canonical_model_id(native_id)
        if or_id is None:
            notes.append(f"unmapped native id: {native_id}")
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        if provider_model_retired(SLUG, or_id, native_id):
            continue
        manifest_row = _chat_manifest_row(
            row,
            model_id=or_id,
            upstream_id=native_id,
        )
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        input_micro = _microdollars_per_million(pricing.get("inputTokenPricePer1M"))
        output_micro = _microdollars_per_million(pricing.get("outputTokenPricePer1M"))
        if input_micro is None or output_micro is None:
            continue
        cached_raw = pricing.get("cachedInputTokenPricePer1M")
        cached_input_micro = (
            _microdollars_per_million(cached_raw) if cached_raw is not None else None
        )
        if cached_raw is not None and cached_input_micro is None:
            notes.append(f"invalid cached-input price for {native_id}: {cached_raw!r}")
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=input_micro,
            completion_micro_per_m=output_micro,
            prompt_cached_micro_per_m=cached_input_micro,
        )
        # The shared snapshot merger still refreshes every mapped live route.
        # Explicitly expected models and syntactically strict GLM releases are
        # safe to append from Tinfoil's structured catalog. Other families stay
        # review-gated even when generic canonicalization can name them.
        if manifest_row is not None and (
            or_id in EXPECTED_MODELS
            or _GLM_NATIVE_ID_RE.fullmatch(native_id.strip().casefold()) is not None
        ):
            discovered[or_id] = manifest_row

    _preserve_announced_feed_removals(prices, discovered, notes)
    _DISCOVERED_MANIFEST_ROWS = discovered

    active_expected_models = [
        model_id
        for model_id in EXPECTED_MODELS
        if not provider_model_retired(SLUG, model_id, UPSTREAM_ID_MAP.get(model_id))
    ]
    errors = validate(prices, active_expected_models)
    if errors:
        notes.append(f"validation notes: {errors}")
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
