"""Anthropic first-party model discovery and pricing.

The authenticated Models API is authoritative for account availability and
capabilities. Anthropic's public pricing page is authoritative for token
prices. Keeping both in one provider adapter lets the hourly refresh publish a
new Claude release without waiting for a downstream catalog.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from scripts.pricing.base import ProviderPricingResult, fetch_json, fetch_provider
from scripts.pricing.manifest import write_discovered_chat_manifest

SLUG = "anthropic"
URL = "https://www.anthropic.com/pricing"
MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"
API_KEY_ENV = "ANTHROPIC_API_KEY"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "anthropic.json"
)
# Model IDs we expect Anthropic to publish on its pricing page. Listed
# in OpenRouter canonical form (`anthropic/<slug>`) — parsers translate
# whatever the page says into these IDs.
EXPECTED_MODELS = [
    "anthropic/claude-opus-5",
    "anthropic/claude-opus-5-fast",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-fable-5",
    "anthropic/claude-opus-4.8",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-haiku-4.5",
]

_DISPLAY_NAME_RE = re.compile(
    r"^Claude (?P<family>Fable|Opus|Sonnet|Haiku) "
    r"(?P<version>[0-9]+(?:\.[0-9]+)?)$"
)
_CANONICAL_ID_RE = re.compile(
    r"^anthropic/claude-(?P<family>fable|opus|sonnet|haiku)-"
    r"(?P<version>[0-9]+(?:\.[0-9]+)?)$"
)
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_DISCOVERY_SOURCE_URL = URL


def _canonical_model_id(display_name: str) -> str | None:
    match = _DISPLAY_NAME_RE.fullmatch(display_name.strip())
    if match is None:
        return None
    return (
        f"anthropic/claude-{match.group('family').lower()}-"
        f"{match.group('version')}"
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _capability_supported(capabilities: dict[str, Any], name: str) -> bool:
    capability = capabilities.get(name)
    return isinstance(capability, dict) and capability.get("supported") is True


def _live_model_rows() -> dict[str, dict[str, Any]]:
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{API_KEY_ENV} is required for Anthropic model discovery")

    payload = fetch_json(
        MODELS_URL,
        extra_headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("Anthropic Models API returned no data list")
    if payload.get("has_more") is True:
        raise RuntimeError("Anthropic Models API exceeded the 1000-model discovery page")

    discovered: dict[str, dict[str, Any]] = {}
    for raw in data:
        if not isinstance(raw, dict):
            continue
        native_id = raw.get("id")
        display_name = raw.get("display_name")
        if not isinstance(native_id, str) or not isinstance(display_name, str):
            continue
        model_id = _canonical_model_id(display_name)
        context_length = _positive_int(raw.get("max_input_tokens"))
        max_output_tokens = _positive_int(raw.get("max_tokens"))
        if model_id is None or context_length is None or max_output_tokens is None:
            continue

        capabilities = raw.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        features = ["function-calling"]
        if _capability_supported(capabilities, "structured_outputs"):
            features.append("structured-outputs")
        if _capability_supported(capabilities, "thinking"):
            features.append("reasoning")

        input_modalities = ["text"]
        if _capability_supported(capabilities, "image_input"):
            input_modalities.append("image")

        discovered[model_id] = {
            "id": model_id,
            "display_name": display_name,
            "title": native_id,
            "context_length": context_length,
            "max_output_tokens": max_output_tokens,
            "model_type": "chat",
            "features": features,
            "input_modalities": input_modalities,
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "upstream_id": native_id,
            "created_at": raw.get("created_at"),
            "status": 1,
        }
    if not discovered:
        raise RuntimeError("Anthropic Models API returned no supported Claude models")
    return discovered


def _known_manifest_model_ids() -> frozenset[str]:
    return frozenset(_manifest_rows_by_id())


def _manifest_rows_by_id() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        model_id: dict(row)
        for row in rows
        if isinstance(row, dict)
        and isinstance((model_id := row.get("id")), str)
        and model_id
    }


def _family_and_version(model_id: str) -> tuple[str, tuple[int, int]] | None:
    match = _CANONICAL_ID_RE.fullmatch(model_id)
    if match is None:
        return None
    parts = tuple(int(part) for part in match.group("version").split("."))
    return match.group("family"), (parts[0], parts[1] if len(parts) > 1 else 0)


def _public_pricing_model_rows(
    prices: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build conservative discovery rows when no Models API key is present.

    The public pricing cards expose every current Claude family and price. An
    existing authenticated row keeps its exact capabilities and token limits.
    A brand-new row uses the stable dateless Claude ID convention and
    conservative generation defaults until a keyed refresh enriches it.
    """

    existing = _manifest_rows_by_id()
    newest_existing: dict[str, tuple[int, int]] = {}
    for model_id in existing:
        parsed = _family_and_version(model_id)
        if parsed is None:
            continue
        family, version = parsed
        newest_existing[family] = max(newest_existing.get(family, ()), version)

    discovered: dict[str, dict[str, Any]] = {}
    for model_id in prices:
        parsed = _family_and_version(model_id)
        if parsed is None:
            continue
        if model_id in existing:
            discovered[model_id] = existing[model_id]
            continue
        family_slug, version_parts = parsed
        if version_parts <= newest_existing.get(family_slug, ()):
            continue

        slug = model_id.removeprefix("anthropic/")
        parts = slug.split("-")
        if len(parts) < 3:
            continue
        family = parts[1].title()
        version = ".".join(parts[2:])
        major_text = parts[2].split(".", 1)[0]
        major = int(major_text) if major_text.isdigit() else 0
        discovered[model_id] = {
            "id": model_id,
            "display_name": f"Claude {family} {version}",
            "title": slug.replace(".", "-"),
            "context_length": 1_000_000 if major >= 5 else 200_000,
            "max_output_tokens": 128_000 if major >= 5 else 64_000,
            "model_type": "chat",
            "features": ["function-calling"],
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "upstream_id": slug.replace(".", "-"),
            "status": 1,
        }
    return discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _DISCOVERY_SOURCE_URL  # noqa: PLW0603

    has_api_key = bool(os.environ.get(API_KEY_ENV, "").strip())
    discovered = _live_model_rows() if has_api_key else {}
    required = frozenset(discovered) - _known_manifest_model_ids()
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=required,
    )
    if not discovered:
        discovered = _public_pricing_model_rows(result.prices)
    _DISCOVERED_MANIFEST_ROWS = discovered
    _DISCOVERY_SOURCE_URL = MODELS_URL if has_api_key else URL
    source = "account" if has_api_key else "public pricing"
    result.notes.append(f"discovered {len(discovered)} Anthropic {source} models")
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=_DISCOVERY_SOURCE_URL,
    )
