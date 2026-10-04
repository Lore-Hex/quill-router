"""Committed catalog history. No storage backend or catalog construction at read time."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).with_name("data")
HISTORY_PATH = DATA_DIR / "model_changes.jsonl"
STATE_PATH = DATA_DIR / "model_changes_state.json"
PRICE_TYPES = frozenset({"pricing_schedule_changed"})
ROUTE_TYPES = frozenset({
    "model_added", "model_removed", "endpoint_added", "endpoint_removed", "endpoint_changed",
    "confidential_route_gained", "confidential_route_lost", "privacy_tier_changed",
})


def timestamp(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def change(
    model: str, kind: str, before: Any, after: Any, effective_at: str,
    *, source: str = "catalog", endpoint: str | None = None,
) -> dict[str, Any]:
    row = dict(model=model, type=kind, before=before, after=after,
               effective_at=effective_at, source=source)
    if endpoint is not None:
        row["endpoint"] = endpoint
    row["id"] = hashlib.sha256(canonical(row).encode()).hexdigest()[:32]
    return row


def route_state(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Project public rows; prices, labels and endpoint ordering cannot create noise."""
    state = {}
    for row in rows:
        tr = row["trustedrouter"]
        if tr.get("internal_only"):
            continue
        state[row["id"]] = {
            "privacy_tier": tr["privacy_tier"],
            # None means this alias does not make a capability declaration.
            "confidential": tr.get("capabilities", {}).get("confidential"),
            "endpoints": {
                ep["id"]: {**{key: ep.get(key) for key in (
                    "provider", "usage_type", "upstream_id", "privacy_tier",
                )}, "confidential": ep.get("capabilities", {}).get("confidential")}
                for ep in tr.get("endpoints", [])
            },
            "pricing_schedules": tr.get("pricing_schedules", {}),
        }
    return state


def diff_catalogs(
    before: dict[str, Any], after: dict[str, Any], effective_at: str,
    *, source: str = "catalog",
) -> list[dict[str, Any]]:
    events = []
    for model in sorted(before.keys() | after.keys()):
        old, new = before.get(model), after.get(model)
        if old is None or new is None:
            events.append(change(model, "model_added" if old is None else "model_removed",
                                 old, new, effective_at, source=source))
            continue
        old_eps, new_eps = old["endpoints"], new["endpoints"]
        for endpoint in sorted(old_eps.keys() | new_eps.keys()):
            left, right = old_eps.get(endpoint), new_eps.get(endpoint)
            if left == right:
                continue
            kind = ("endpoint_added" if left is None else
                    "endpoint_removed" if right is None else "endpoint_changed")
            events.append(change(model, kind, left, right, effective_at,
                                 source=source, endpoint=endpoint))
        for field, kind in (("privacy_tier", "privacy_tier_changed"),
                            ("pricing_schedules", "pricing_schedule_changed")):
            if old.get(field) != new.get(field):
                events.append(change(model, kind, old.get(field), new.get(field),
                                     effective_at, source=source))
        if old["confidential"] != new["confidential"] and None not in (
            old["confidential"], new["confidential"],
        ):
            events.append(change(model, "confidential_route_gained" if new["confidential"]
                                 else "confidential_route_lost", old["confidential"],
                                 new["confidential"], effective_at, source=source))
    return events


def apply_changes(state: dict[str, Any], events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Advance a recorded baseline over already-recorded scheduled cutovers."""
    result = json.loads(canonical(state))
    fields = {"privacy_tier_changed": "privacy_tier",
              "pricing_schedule_changed": "pricing_schedules",
              "confidential_route_gained": "confidential",
              "confidential_route_lost": "confidential"}
    for event in sorted(events, key=lambda e: (e["effective_at"], e["id"])):
        model, kind = event["model"], event["type"]
        if kind == "model_added":
            result[model] = event["after"]
        elif kind == "model_removed":
            result.pop(model, None)
        elif kind in fields and model in result:
            result[model][fields[kind]] = event["after"]
        elif kind.startswith("endpoint_") and model in result:
            endpoints = result[model]["endpoints"]
            if kind == "endpoint_removed":
                endpoints.pop(event["endpoint"], None)
            else:
                endpoints[event["endpoint"]] = event["after"]
    return result


@lru_cache(maxsize=1)
def load_history() -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(line) for line in HISTORY_PATH.read_text().splitlines() if line)


def active_history(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(events)
    cancelled = {e["before"]["id"] for e in rows if e["type"] == "schedule_cancelled"}
    return [e for e in rows if e["id"] not in cancelled]


def last_route_changes(at: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for event in active_history(load_history()):
        if event["type"] in ROUTE_TYPES and event["effective_at"] <= at:
            model = event["model"]
            result[model] = max(result.get(model, ""), event["effective_at"])
    return result
