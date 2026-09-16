"""OpenAI authenticated availability plus official token pricing."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    ProviderPricingResult,
    emit_workflow_warning,
    fetch_json,
    fetch_provider,
    runtime_required_models,
)
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import (
    discover_available_priced_chat_catalog,
    probe_openai_chat,
)
from trusted_router.image_generation import OPENAI_IMAGE_MODEL_IDS

SLUG = "openai"
URL = "https://developers.openai.com/api/docs/pricing"
BASE_URL = "https://api.openai.com/v1"
MODELS_URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "openai.json"
)

EXPECTED_MODELS = [
    "openai/gpt-6-astra",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-luna",
    "openai/gpt-5.5",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
]

_MODEL_METADATA_OVERRIDES: dict[str, dict[str, Any]] = {
    "openai/gpt-6-astra": {
        "context_length": 1_050_000,
        "max_output_tokens": 128_000,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "supported_features": [
            "function-calling",
            "reasoning-effort",
            "structured-output",
        ],
    },
}

_NON_CHAT_MARKERS = (
    "audio",
    "realtime",
    "transcribe",
    "tts",
    "image",
    "instruct",
    "embedding",
    "moderation",
    "search-preview",
    "deep-research",
    "computer-use",
)
_DATED_MODEL_RE = re.compile(r"-(?:20\d{2}-\d{2}-\d{2}|20\d{6})$")
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def probe_openai_image(*, api_key: str, model: str) -> bool:
    """One low-quality canary only for new or previously held image routes."""
    try:
        response = httpx.post(
            f"{BASE_URL}/images/generations",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "prompt": "A red square on white.", "size": "1024x1024", "quality": "low", "n": 1},
            timeout=180,
        )
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {})
        return bool(data.get("data")) and all(
            type(usage.get(field)) is int and usage[field] > 0
            for field in ("input_tokens", "output_tokens", "total_tokens")
        )
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False


def _is_stable_chat_model(row: dict[str, Any]) -> bool:
    native_id = row.get("id")
    if not isinstance(native_id, str):
        return False
    value = native_id.casefold()
    if value.startswith("ft:") or _DATED_MODEL_RE.search(value):
        return False
    if any(marker in value for marker in _NON_CHAT_MARKERS):
        return False
    return value.startswith(("gpt-", "o1", "o3", "o4", "chat-latest"))


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        require_runtime_models=False,
    )
    unpriced = sorted(runtime_required_models(SLUG) - result.prices.keys())
    if unpriced:
        note = f"openai: no official price for discovered models; excluded from routing: {unpriced}"
        emit_workflow_warning(note)
        result.notes.append(note)
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CHATGPT_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for model discovery")
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    explicit_map = {
        str(row["id"]): f"openai/{str(row['id']).casefold()}"
        for row in rows
        if _is_stable_chat_model(row)
    }
    discovered = discover_available_priced_chat_catalog(
        rows,
        prices=result.prices,
        explicit_map=explicit_map,
        upstream_id_map=UPSTREAM_ID_MAP,
        include=_is_stable_chat_model,
    )
    if not discovered:
        raise RuntimeError("openai: no priced chat models found in authenticated catalog")
    for model_id, metadata in _MODEL_METADATA_OVERRIDES.items():
        if row := discovered.get(model_id):
            row.update(metadata)

    available = {row.get("id") for row in rows}
    for model_id in sorted(OPENAI_IMAGE_MODEL_IDS):
        native_id = model_id.removeprefix("openai/")
        if native_id not in available:
            continue
        UPSTREAM_ID_MAP[model_id] = native_id
        discovered[model_id] = {
            "id": model_id, "upstream_id": native_id,
            "display_name": "GPT Image 2.5 " + native_id.rsplit("-", 1)[1].title(),
            "model_type": "image", "endpoints": ["images"],
            "input_modalities": ["text"], "output_modalities": ["image"],
        }

    checked = models_requiring_canary(MANIFEST_PATH, discovered)
    healthy = {
        model_id
        for model_id in sorted(checked)
        if (probe_openai_image(api_key=api_key, model=UPSTREAM_ID_MAP[model_id])
            if model_id in OPENAI_IMAGE_MODEL_IDS else probe_openai_chat(
            base_url=BASE_URL,
            api_key=api_key,
            model=UPSTREAM_ID_MAP[model_id],
            max_tokens_field="max_completion_tokens",
        ))
    }
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    result.source = "api"
    result.fetched_url = MODELS_URL
    result.notes.extend(
        [
            f"intersected official pricing with {len(discovered)} authenticated chat models",
            f"canaried {len(checked)} new/held routes ({len(healthy)} healthy)",
        ]
    )
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
    )
