"""FriendliAI — human-only provider config.

Friendli publishes an OpenAI-compatible Model API at
https://api.friendli.ai/serverless/v1. Its `/models` response includes
model ids, context length, and per-million-token pricing. The response uses
mixed namespaces:

* GLM/Qwen/MiniMax/DeepSeek use upstream-author ids such as
  `zai-org/GLM-5.2`.
* Llama rows use Friendli-local ids such as
  `meta-llama-3.3-70b-instruct`.

This adapter keeps exact upstream ids in `UPSTREAM_ID_MAP` so the gateway
can call Friendli without falling through to a lowercase/author-stripping
guess.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    guard_manifest_prune,
    reconcile_manifest_tombstones,
    validate,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from scripts.pricing.openai_catalog import dollars_per_token_to_micro_per_m
from trusted_router.provider_lifecycle import provider_model_retired

SLUG = "friendli"
URL = "https://api.friendli.ai/serverless/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "friendli.json"
)
DEPRECATED_NATIVE_IDS = {
    # Friendli notified customers that GLM-5 serverless Model APIs stop being
    # supported at 2026-07-03 00:00 UTC. Dedicated endpoints are unaffected, but
    # TrustedRouter's Friendli route uses the serverless Model API.
    "zai-org/GLM-5",
}

EXPECTED_MODELS = [
    "z-ai/glm-5.2",
]

_NATIVE_TO_OR_ID = {
    "meta-llama-3.3-70b-instruct": "meta-llama/llama-3.3-70b-instruct",
    "meta-llama-3.1-8b-instruct": "meta-llama/llama-3.1-8b-instruct",
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen/qwen3-235b-a22b-2507",
    "LGAI-EXAONE/K-EXAONE-236B-A23B": "lgai-exaone/k-exaone-236b-a23b",
    "zai-org/GLM-5": "z-ai/glm-5",
    "MiniMaxAI/MiniMax-M2.5": "minimax/minimax-m2.5",
    "deepseek-ai/DeepSeek-V3.2": "deepseek/deepseek-v3.2",
    "zai-org/GLM-5.1": "z-ai/glm-5.1",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
}

UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _discovered_manifest_row(
    model_id: str, native_id: str, source_row: dict[str, Any]
) -> dict[str, Any]:
    row: dict[str, Any] = {"id": model_id, "upstream_id": native_id}
    display_name = source_row.get("display_name") or source_row.get("name")
    if isinstance(display_name, str) and display_name:
        row["display_name"] = display_name
    context_length = _positive_int(
        source_row.get("context_length") or source_row.get("max_model_len")
    )
    if context_length is not None:
        row["context_length"] = context_length
    max_output_tokens = _positive_int(source_row.get("max_output_tokens"))
    if max_output_tokens is not None:
        row["max_output_tokens"] = max_output_tokens
    return row


def _price_to_micro_per_m(value: object) -> int | None:
    """Convert Friendli's USD-per-token strings without binary floats."""
    return dollars_per_token_to_micro_per_m(value)


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    api_key = os.environ.get("FRIENDLI_API_KEY")
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []
    prices: dict[str, ModelPrice] = {}
    manifest_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        if native_id in DEPRECATED_NATIVE_IDS:
            continue
        or_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if or_id is None:
            continue
        if provider_model_retired(SLUG, or_id, native_id):
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        manifest_rows[or_id] = _discovered_manifest_row(or_id, native_id, row)
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        prompt = _price_to_micro_per_m(pricing.get("input") or pricing.get("prompt"))
        completion = _price_to_micro_per_m(
            pricing.get("output") or pricing.get("completion")
        )
        if prompt is None or completion is None:
            continue
        cache_read = _price_to_micro_per_m(pricing.get("input_cache_read"))
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cache_read,
        )

    _DISCOVERED_MANIFEST_ROWS = manifest_rows
    notes: list[str] = []
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")
        raise RuntimeError("; ".join(errors))

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Rebuild Friendli routes from its live serverless model feed."""

    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("friendli manifest has no models list")

    if not _DISCOVERED_MANIFEST_ROWS:
        guarded = guard_manifest_prune(rows, [], provider_slug=SLUG)
        if guarded is rows:
            return ["friendli: kept old manifest (mass-prune guard)"]
        raise RuntimeError("friendli discovery returned no supported model rows")

    existing_by_id = {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    present_rows: dict[str, dict[str, Any]] = {}
    updated: list[str] = []
    appended: list[str] = []
    for model_id, discovered in sorted(_DISCOVERED_MANIFEST_ROWS.items()):
        existing = existing_by_id.get(model_id)
        if existing is None:
            row: dict[str, Any] = {
                "display_name": str(discovered.get("display_name") or model_id),
                "title": str(discovered.get("upstream_id") or model_id),
                "model_type": "chat",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "endpoints": ["chat/completions"],
                "status": 1,
            }
            appended.append(model_id)
        else:
            # Start from the committed row so annotations such as _note,
            # feature flags, and an explicit routable:false survive rebuilds.
            row = dict(existing)
        row.update(discovered)

        price = result.prices.get(model_id)
        if price is not None:
            tier = price.tiers[0]
            row["input_token_price_per_m"] = tier.prompt_micro_per_m
            row["output_token_price_per_m"] = tier.completion_micro_per_m
            if tier.prompt_cached_micro_per_m is not None:
                row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
            else:
                row.pop("cached_input_token_price_per_m", None)
            updated.append(model_id)
        elif existing is None:
            # Discovery without an authoritative price is useful metadata, but
            # it must never become a zero-priced route.
            row["routable"] = False
        present_rows[model_id] = row

    rebuilt = reconcile_manifest_tombstones(
        rows,
        present_rows,
        priced_ids=set(result.prices),
        source=result.source,
    )
    guarded = guard_manifest_prune(rows, rebuilt, provider_slug=SLUG)
    if guarded is rows:
        return ["friendli: kept old manifest (mass-prune guard)"]

    rebuilt_by_id = {
        row["id"]: row
        for row in rebuilt
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    tombstoned = sorted(
        model_id
        for model_id, old_row in existing_by_id.items()
        if old_row.get("routable") is not False
        and rebuilt_by_id.get(model_id, {}).get("routable") is False
    )
    raw["models"] = rebuilt
    raw["provider"] = SLUG
    raw["source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rebuilt)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    changes: list[str] = []
    if appended:
        changes.append(f"appended {len(appended)}")
    if tombstoned:
        changes.append(f"tombstoned {len(tombstoned)} unavailable")
    suffix = f", {', '.join(changes)}" if changes else ""
    return [
        f"friendli: refreshed provider_models/friendli.json "
        f"({len(updated)} priced rows{suffix})"
    ]
