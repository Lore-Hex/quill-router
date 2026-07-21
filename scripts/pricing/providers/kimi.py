"""Moonshot/Kimi first-party pricing and live-model discovery.

Moonshot publishes a documentation index at ``/docs/llms.txt``. Pricing
pages linked from that index contain JSX-shaped tables, while the authenticated
``/v1/models`` endpoint is authoritative for what the operator account can
actually invoke. A model becomes routable only when both sources agree: it
must be live and have a non-zero first-party price.

The provider module is human-maintained. The parser remains a pure
``parse(text)`` function in ``parsers/kimi.py`` and cannot perform network IO.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ProviderPricingResult,
    _coerce_to_model_prices,
    fetch_html,
    fetch_json,
    log,
    parser_path,
    runtime_required_models,
    validate,
)

SLUG = "kimi"
DOC_INDEX_URL = "https://platform.kimi.ai/docs/llms.txt"
MODELS_URL = "https://api.moonshot.ai/v1/models"
FALLBACK_SUBPAGES = (
    "https://platform.kimi.ai/docs/pricing/chat-k3.md",
    "https://platform.kimi.ai/docs/pricing/chat-k27-code.md",
    "https://platform.kimi.ai/docs/pricing/chat-k26.md",
    "https://platform.kimi.ai/docs/pricing/chat-k25.md",
    "https://platform.kimi.ai/docs/pricing/chat-k2.md",
    "https://platform.kimi.ai/docs/pricing/chat-v1.md",
)
URL = DOC_INDEX_URL
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "kimi.json"
)

EXPECTED_MODELS = [
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.7-code",
    "moonshotai/kimi-k2.7-code-highspeed",
]

_PRICING_LINK_RE = re.compile(
    r"https://platform\.kimi\.ai/docs/pricing/chat(?:-[a-z0-9]+)*\.md",
    re.IGNORECASE,
)
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _pricing_subpages(index_text: str) -> list[str]:
    """Return deduplicated, allowlisted Kimi chat-pricing pages.

    The origin and path are part of the regex, so a compromised documentation
    index cannot turn the pricing worker into an arbitrary URL fetcher.
    Fallback pages preserve known families during a transient index failure.
    """

    discovered = [match.group(0) for match in _PRICING_LINK_RE.finditer(index_text)]
    ordered = [*discovered, *FALLBACK_SUBPAGES]
    return list(dict.fromkeys(ordered))


def _combined_html() -> str:
    try:
        index_text = fetch_html(DOC_INDEX_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("kimi.doc_index_fetch_failed err=%s", exc)
        index_text = ""

    chunks: list[str] = []
    for url in _pricing_subpages(index_text):
        try:
            chunks.append(fetch_html(url))
        except Exception as exc:  # noqa: BLE001
            log.warning("kimi.subpage_fetch_failed url=%s err=%s", url, exc)
    return "\n\n--- PAGE BREAK ---\n\n".join(chunks)


def _canonical_model_id(native_id: str) -> str | None:
    lowered = native_id.strip().casefold()
    if lowered.startswith(("kimi-", "moonshot-v1-")):
        return f"moonshotai/{lowered}"
    return None


def _display_name(native_id: str) -> str:
    words = native_id.replace("-", " ").split()
    rendered: list[str] = []
    for word in words:
        lowered = word.casefold()
        if lowered == "kimi":
            rendered.append("Kimi")
        elif lowered == "moonshot":
            rendered.append("Moonshot")
        elif lowered == "highspeed":
            rendered.append("HighSpeed")
        elif lowered.startswith(("k2", "v1")):
            rendered.append(word.upper())
        else:
            rendered.append(word.title())
    return " ".join(rendered)


def _live_model_rows() -> dict[str, dict[str, Any]]:
    token = os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY")
    if not token:
        raise RuntimeError("kimi: KIMI_API_KEY or MOONSHOT_API_KEY is required")

    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {token}"},
    )
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list):
        raise RuntimeError("kimi: /v1/models response has no data list")

    rows: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        native_id = raw.get("id")
        if not isinstance(native_id, str):
            continue
        model_id = _canonical_model_id(native_id)
        if model_id is None:
            continue
        context_length = raw.get("context_length")
        if not isinstance(context_length, int) or context_length <= 0:
            context_length = 0
        rows[model_id] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": _display_name(native_id),
            "title": native_id,
            "model_type": "chat",
            "endpoints": ["chat/completions"],
            "context_length": context_length,
            "supports_image_in": bool(raw.get("supports_image_in")),
            "supports_video_in": bool(raw.get("supports_video_in")),
            "supports_reasoning": bool(raw.get("supports_reasoning")),
        }
    if not rows:
        raise RuntimeError("kimi: /v1/models returned no supported model ids")
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
    known = _known_manifest_model_ids()
    return frozenset(
        model_id
        for model_id in live_rows
        if model_id not in known and model_id != "moonshotai/moonshot-v1-auto"
    )


def fetch() -> ProviderPricingResult:
    """Fetch first-party prices and intersect them with live account models."""

    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603
    _DISCOVERED_MANIFEST_ROWS = {}

    live_rows = _live_model_rows()
    required_price_ids = _new_required_price_ids(live_rows) | runtime_required_models(SLUG)

    html = _combined_html()
    if not html:
        raise RuntimeError("kimi: all pricing pages failed to fetch")

    src = parser_path(SLUG).read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    exec(compile(src, str(parser_path(SLUG)), "exec"), namespace)  # noqa: S102
    parse_fn = namespace.get("parse")
    if not callable(parse_fn):
        raise RuntimeError(f"{SLUG}: parsers/{SLUG}.py has no callable `parse`")
    raw = parse_fn(html)

    prices, schema_errors = _coerce_to_model_prices(raw)
    if schema_errors:
        raise RuntimeError(f"{SLUG}: parser schema errors: {schema_errors}")
    if prices is None:
        raise RuntimeError(f"{SLUG}: parser returned None unexpectedly")

    prices = {model_id: price for model_id, price in prices.items() if model_id in live_rows}
    _DISCOVERED_MANIFEST_ROWS = live_rows

    errors = validate(
        prices,
        EXPECTED_MODELS,
        required_models=required_price_ids,
    )
    if errors:
        raise RuntimeError(f"{SLUG}: validation failed: {errors}")
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="multi_page_md+api",
        fetched_url=URL,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Refresh the provider-native Kimi manifest from live, priced models."""

    if MANIFEST_PATH.exists():
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    else:
        raw = {"provider": SLUG, "models": []}
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("kimi manifest has no models list")

    existing_by_id: dict[str, dict[str, Any]] = {
        row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    active_rows: list[dict[str, Any]] = []
    updated: list[str] = []
    appended: list[str] = []
    for model_id, price in sorted(result.prices.items()):
        discovered = _DISCOVERED_MANIFEST_ROWS.get(model_id)
        if discovered is None:
            continue
        existing = existing_by_id.get(model_id)
        row = dict(existing) if existing is not None else {}
        row.update(discovered)
        if existing is None:
            appended.append(model_id)

        tier = price.tiers[0]
        row["input_token_price_per_m"] = tier.prompt_micro_per_m
        row["output_token_price_per_m"] = tier.completion_micro_per_m
        if tier.prompt_cached_micro_per_m is not None:
            row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
        else:
            row.pop("cached_input_token_price_per_m", None)
        active_rows.append(row)
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"kimi manifest did not update expected model(s): {missing}")

    active_rows.sort(key=lambda row: str(row.get("id") or ""))
    raw["models"] = active_rows
    raw["_about"] = (
        "Provider-native Moonshot/Kimi routes. Refreshed hourly only for models "
        "present in both the official pricing docs and authenticated /v1/models feed."
    )
    raw["source"] = MODELS_URL
    raw["pricing_source"] = DOC_INDEX_URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(active_rows)
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    suffix = f", appended {len(appended)}" if appended else ""
    return [f"kimi: refreshed provider_models/kimi.json ({len(updated)} priced rows{suffix})"]
