"""Google Gemini — human-only provider config.

Google's official pricing page is server-rendered and is fetched directly.
An earlier implementation depended on a Jina mirror, but that endpoint began
returning HTTP 401 while the official page remained available. Keeping the
official source as the fetch target removes that unnecessary failure mode.

The Gemini docs pricing page has explicit context-tier breakdowns
(e.g. "$1.25, prompts <= 200k tokens / $2.50, prompts > 200k tokens"
for Gemini 2.5 Pro) which the parser converts into PriceTier objects
for tier-aware billing.

TrustedRouter publishes Google AI Studio and Google Vertex as separate runtime
providers. This adapter owns AI Studio model discovery. The shared refresh may
apply Google's standard Gemini token prices to existing Vertex endpoint rows,
but it never invents Vertex availability from AI Studio discovery.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from scripts.pricing.base import (
    ProviderPricingResult,
    fetch_json,
    fetch_provider,
    guard_manifest_prune,
    reconcile_manifest_tombstones,
    validate,
)

SLUG = "gemini"
URL = "https://ai.google.dev/gemini-api/docs/pricing"
VERTEX_PRICING_URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "google-ai-studio.json"
)

EXPECTED_MODELS = [
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
    "google/gemini-3.5-flash",
    "google/gemini-3.6-flash",
]
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_STANDARD_TEXT_MODEL_RE = re.compile(r"^google/gemini-\d+(?:\.\d+)?-(?:pro|flash|flash-lite)$")


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _live_model_rows() -> dict[str, dict[str, Any]]:
    headers: dict[str, str] = {}
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        headers["x-goog-api-key"] = api_key
    rows: dict[str, dict[str, Any]] = {}
    page_token: str | None = None
    seen_tokens: set[str] = set()
    for page_index in range(10):
        params = {"pageSize": "1000"}
        if page_token is not None:
            params["pageToken"] = page_token
        payload = fetch_json(
            f"{MODELS_URL}?{urlencode(params)}",
            extra_headers=headers,
        )
        raw_rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(raw_rows, list):
            raise RuntimeError("gemini: /v1beta/models response has no models list")

        for source_row in raw_rows:
            if not isinstance(source_row, dict):
                continue
            raw_name = source_row.get("name")
            if not isinstance(raw_name, str):
                continue
            native_id = raw_name.removeprefix("models/").strip()
            if not native_id.casefold().startswith("gemini-"):
                continue
            methods = source_row.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            model_id = f"google/{native_id.casefold()}"
            row: dict[str, Any] = {"id": model_id, "upstream_id": native_id}
            display_name = source_row.get("displayName")
            if isinstance(display_name, str) and display_name:
                row["display_name"] = display_name
            context_length = _positive_int(source_row.get("inputTokenLimit"))
            if context_length is not None:
                row["context_length"] = context_length
            max_output_tokens = _positive_int(source_row.get("outputTokenLimit"))
            if max_output_tokens is not None:
                row["max_output_tokens"] = max_output_tokens
            rows[model_id] = row

        next_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
        if not next_token:
            break
        if not isinstance(next_token, str) or next_token in seen_tokens:
            raise RuntimeError("gemini: /v1beta/models pagination token did not advance")
        seen_tokens.add(next_token)
        page_token = next_token
        if page_index == 9:
            raise RuntimeError("gemini: /v1beta/models exceeded 10 pages")
    if not rows:
        raise RuntimeError("gemini: /v1beta/models returned no chat-capable Gemini rows")
    return rows


def _known_manifest_model_ids() -> set[str]:
    if not MANIFEST_PATH.exists():
        return set()
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return set()
    return {
        model_id
        for row in rows
        if isinstance(row, dict) and isinstance((model_id := row.get("id")), str) and model_id
    }


def _new_required_price_ids(live_rows: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Require prices for newly launched stable Gemini text SKUs.

    Google's model feed also contains aliases, TTS, image-output, robotics,
    and experimental rows that do not correspond one-to-one with a token
    pricing section.  Stable Pro, Flash, and Flash Lite release IDs do.  A new
    stable release therefore triggers parser self-heal immediately, even when
    OpenRouter has not listed it yet.
    """

    known = _known_manifest_model_ids()
    return frozenset(
        model_id
        for model_id in live_rows
        if model_id not in known and _STANDARD_TEXT_MODEL_RE.fullmatch(model_id)
    )


def _refresh_price(row: dict[str, Any], result: ProviderPricingResult, model_id: str) -> bool:
    price = result.prices.get(model_id)
    if price is None:
        return False
    tier = price.tiers[0]
    row["input_token_price_per_m"] = tier.prompt_micro_per_m
    row["output_token_price_per_m"] = tier.completion_micro_per_m
    if len(price.tiers) > 1:
        row["price_tiers"] = [
            {
                "max_prompt_tokens": price_tier.max_prompt_tokens,
                "input_token_price_per_m": price_tier.prompt_micro_per_m,
                "output_token_price_per_m": price_tier.completion_micro_per_m,
                "cached_input_token_price_per_m": price_tier.prompt_cached_micro_per_m,
            }
            for price_tier in price.tiers
        ]
        row.pop("cached_input_token_price_per_m", None)
    elif tier.prompt_cached_micro_per_m is not None:
        row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
        row.pop("price_tiers", None)
    else:
        row.pop("cached_input_token_price_per_m", None)
        row.pop("price_tiers", None)
    return True


def _refresh_verified_vertex_manifest(result: ProviderPricingResult) -> int:
    """Refresh prices for Vertex rows whose availability was verified separately."""

    path = MANIFEST_PATH.with_name("google-vertex.json")
    if not path.exists():
        return 0
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("google-vertex manifest has no models list")
    updated = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if isinstance(model_id, str) and _refresh_price(row, result, model_id):
            updated += 1
    if not updated:
        return 0
    raw["pricing_source"] = VERTEX_PRICING_URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return updated


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    live_rows = _live_model_rows()
    required_price_ids = _new_required_price_ids(live_rows)
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=required_price_ids,
    )
    _DISCOVERED_MANIFEST_ROWS = live_rows
    result.prices = {
        model_id: price for model_id, price in result.prices.items() if model_id in live_rows
    }
    errors = validate(result.prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    if result.source != "stale_snapshot":
        # The manifest's availability view came from a complete live API feed;
        # use that freshness marker for consecutive-miss accounting.
        result.source = "api"
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Rebuild Gemini supplemental routes from Google's live models feed."""

    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("gemini manifest has no models list")

    if not _DISCOVERED_MANIFEST_ROWS:
        guarded = guard_manifest_prune(rows, [], provider_slug=SLUG)
        if guarded is rows:
            return ["gemini: kept old manifest (mass-prune guard)"]
        raise RuntimeError("gemini discovery returned no supported model rows")

    existing_by_id = {
        row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    present_rows: dict[str, dict[str, Any]] = {}
    updated: list[str] = []
    appended: list[str] = []
    for model_id, discovered in sorted(_DISCOVERED_MANIFEST_ROWS.items()):
        existing = existing_by_id.get(model_id)
        if existing is None:
            row: dict[str, Any] = {
                "display_name": str(discovered.get("display_name") or model_id),
                "title": model_id,
                "model_type": "chat",
                "features": [],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "endpoints": ["chat/completions"],
                "status": 1,
            }
            appended.append(model_id)
        else:
            # Preserve human annotations and routable:false on known rows.
            row = dict(existing)
        row.update(discovered)
        if _refresh_price(row, result, model_id):
            updated.append(model_id)
        elif existing is None:
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
        return ["gemini: kept old manifest (mass-prune guard)"]

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
    raw["provider"] = "google-ai-studio"
    raw["source"] = MODELS_URL
    raw["pricing_source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rebuilt)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    vertex_updated = _refresh_verified_vertex_manifest(result)
    changes: list[str] = []
    if appended:
        changes.append(f"appended {len(appended)}")
    if tombstoned:
        changes.append(f"tombstoned {len(tombstoned)} unavailable")
    if vertex_updated:
        changes.append(f"repriced {vertex_updated} verified Vertex rows")
    suffix = f", {', '.join(changes)}" if changes else ""
    return [f"gemini: refreshed Google AI Studio manifest ({len(updated)} priced rows{suffix})"]
