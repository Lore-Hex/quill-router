"""Hixtube's authenticated Catalog v2 and live, usage-accounted canaries."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import probe_openai_chat
from scripts.pricing.provider_contract_catalog import discover_provider_contract_catalog

SLUG = "hixtube"
BASE_URL = "https://api.hixtube.com/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/hixtube.json"
MANIFEST_STALE_FALLBACK = True
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def canonical_model_id(native_id: str) -> str:
    return native_id


def discover(payload: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    prices, rows = discover_provider_contract_catalog(payload, upstream_id_map={})
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("hixtube: no active priced chat models" if not prices else "; ".join(errors))
    for row in rows.values():
        _retirement(row)
        reliability = dict(row.get("provider_reliability") or {})
        reliability.pop("support_contact", None)
        reliability.pop("incident_contact", None)
        row["provider_reliability"] = reliability
    return prices, rows


def _retirement(row: dict[str, Any]) -> datetime:
    value = row.get("retirement_at")
    if not isinstance(value, str):
        raise RuntimeError("hixtube: public pilot retirement is required")
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("hixtube: invalid retirement") from error
    now = datetime.now(UTC)
    if deadline.tzinfo is None or not now < deadline <= now + timedelta(hours=24):
        raise RuntimeError("hixtube: retirement must be within the next 24 hours")
    return deadline.astimezone(UTC)


def fetch() -> ProviderPricingResult:
    _DISCOVERED_ROWS.clear()
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("HIXTUBE_API_KEY")
    if not key:
        raise RuntimeError("HIXTUBE_API_KEY is required for discovery")
    payload = fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"})
    prices, rows = discover(payload)
    # A short-lived pilot may restart under the same model ID. Every fresh
    # declaration must prove callability before it can renew the runtime cutoff.
    checked = frozenset(rows)
    healthy = {
        model_id for model_id in checked
        if probe_openai_chat(
            base_url=BASE_URL, api_key=key, model=rows[model_id]["upstream_id"],
            max_tokens=64, expected_content="PONG", require_usage=True,
            prompt="Reply with exactly PONG and nothing else.",
        )
    }
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    UPSTREAM_ID_MAP.update({model_id: row["upstream_id"] for model_id, row in rows.items()})
    _DISCOVERED_ROWS.update(rows)
    return ProviderPricingResult(
        slug=SLUG, prices=prices, source="api", fetched_url=URL,
        notes=[f"Catalog v2: {len(rows)} models; {len(healthy)}/{len(checked)} pilot canaries passed"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("hixtube: fetch must succeed before writing manifest")
    deadline = min(_retirement(row) for row in _DISCOVERED_ROWS.values())
    # Publish the ordinary manifest and its runtime cutoff in one replacement.
    # A refresh must never stretch a 24h pilot into the normal stale-price window.
    with tempfile.TemporaryDirectory(dir=MANIFEST_PATH.parent, prefix=".hixtube-") as directory:
        candidate = Path(directory) / MANIFEST_PATH.name
        if MANIFEST_PATH.exists():
            candidate.write_bytes(MANIFEST_PATH.read_bytes())
        notes = write_discovered_chat_manifest(
            result, manifest_path=candidate, discovered_rows=_DISCOVERED_ROWS, source_url=URL,
        )
        raw = json.loads(candidate.read_text(encoding="utf-8"))
        for row in raw["models"]:
            if row.get("routable", True):
                deadline = min(deadline, _retirement(row))
        raw["pricing_valid_until"] = deadline.isoformat().replace("+00:00", "Z")
        candidate.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        candidate.replace(MANIFEST_PATH)
    return notes
