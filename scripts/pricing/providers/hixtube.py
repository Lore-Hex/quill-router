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
    set_manifest_model_canary_states,
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
    if not isinstance(payload, dict) or payload.get("contract_version") != "2.0":
        raise RuntimeError("hixtube: Catalog v2 contract is required")
    provider = payload.get("provider")
    if not isinstance(provider, dict) or provider.get("id") != SLUG:
        raise RuntimeError("hixtube: provider.id must be hixtube")
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


def _retirement_time(row: dict[str, Any]) -> datetime:
    value = row.get("retirement_at")
    if not isinstance(value, str):
        raise RuntimeError("hixtube: public pilot retirement is required")
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("hixtube: invalid retirement") from error
    if deadline.tzinfo is None:
        raise RuntimeError("hixtube: retirement must have a timezone")
    return deadline.astimezone(UTC)


def _retirement(row: dict[str, Any]) -> datetime:
    deadline = _retirement_time(row)
    now = datetime.now(UTC)
    if not now < deadline <= now + timedelta(hours=24):
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
        curated_ids: set[str] = set()
        if MANIFEST_PATH.exists():
            previous = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            for row in previous["models"]:
                if row.get("routable") is False and row.get("routable_reason") is None:
                    curated_ids.add(row["id"])
                    row.pop("routable_reason", None)
                # Only our retirement marker can recover this way. Fresh
                # discovery already validated the cutoff, price and canary.
                if (row.get("routable_reason") == "pilot-retired"
                        and _DISCOVERED_ROWS.get(row["id"], {}).get("routable") is True):
                    row.pop("routable", None)
                    row.pop("routable_reason", None)
            candidate.write_text(json.dumps(previous), encoding="utf-8")
        notes = write_discovered_chat_manifest(
            result, manifest_path=candidate, discovered_rows=_DISCOVERED_ROWS, source_url=URL,
            confirmed_disabled_ids={
                model_id for model_id, row in _DISCOVERED_ROWS.items()
                if row.get("routable_reason") == "provider-canary-failed"
            },
        )
        # Keep the shared writer's reasonless curated holds untouched while
        # ensuring a mass-prune guard cannot override a direct failed probe.
        checked = _DISCOVERED_ROWS.keys() - curated_ids
        set_manifest_model_canary_states(
            candidate, checked_model_ids=checked,
            healthy_model_ids={model_id for model_id in checked if _DISCOVERED_ROWS[model_id].get("routable") is True},
        )
        raw = json.loads(candidate.read_text(encoding="utf-8"))
        now = datetime.now(UTC)
        for row in raw["models"]:
            if row.get("routable", True):
                retired_at = _retirement_time(row)
                if retired_at <= now:
                    row.update(routable=False, routable_reason="pilot-retired")
                else:
                    deadline = min(deadline, retired_at)
        raw["pricing_valid_until"] = deadline.isoformat().replace("+00:00", "Z")
        candidate.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        candidate.replace(MANIFEST_PATH)
    return notes


def confirmed_manifest_quarantines(
    result: ProviderPricingResult, before_rows: list[Any], after_rows: list[Any],
) -> frozenset[str]:
    """Evidence for retained pilot quarantines at the outer publication gate."""
    if result.source != "api":
        return frozenset()
    previous = {row["id"]: row for row in before_rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    confirmed = set()
    for row in after_rows:
        if not isinstance(row, dict) or row.get("routable") is not False:
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str):
            continue
        discovered = _DISCOVERED_ROWS.get(model_id, {})
        if (row.get("routable_reason") == discovered.get("routable_reason") == "provider-canary-failed"
                and discovered.get("routable") is False and model_id in result.prices):
            confirmed.add(model_id)
        elif (row.get("routable_reason") == "pilot-retired" and model_id in previous
              and row.get("retirement_at") == previous[model_id].get("retirement_at")
              and _retirement_time(previous[model_id]) <= datetime.now(UTC)):
            confirmed.add(model_id)
    return frozenset(confirmed)
