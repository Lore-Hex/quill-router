"""Live provider-state checks skip what a reviewed hold keeps dark on purpose.

The holds live in trusted_router.provider_contracts; nothing here names a
provider. A hold is a decision, not drift, so a freshness report must not
fail on it, and the hold is cleared only through its own reviewed process.

The hourly price refresh places a hold of its own, without review: a listed
route whose provider refuses every request is held as provider-route-unserved
(scripts/pricing/unserved_route_holds.py) on whichever routable row the
evidence names, and the shared manifest writers carry it forward until that
step lifts it. The refresh publishes only if this suite passes on the snapshot
that hold is in, so a release test that expects a committed row to route says
"routes, or row_on_unserved_route_hold". On 2026-11-26 one Cloudflare row was
held and a test that listed the states a row may be in, without this one,
failed the refresh's own snapshot.
"""

from __future__ import annotations

import json
from typing import Any

from trusted_router.catalog_ingest import _PROVIDER_MODELS_DIR
from trusted_router.provider_contracts import provider_route_reviewed_hold
from trusted_router.provider_manifest_policy import (
    _CANARY_QUARANTINE_REASONS,
    UNSERVED_ROUTE_HELD_AT_FIELD,
    UNSERVED_ROUTE_HOLD_REASON,
)


def manifest_rows(provider: str) -> list[dict[str, Any]]:
    path = _PROVIDER_MODELS_DIR / f"{provider}.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8")).get("models", [])
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]


def row_on_reviewed_hold(provider: str, row: dict[str, Any]) -> bool:
    upstream = row.get("upstream_id")
    return provider_route_reviewed_hold(
        provider, row["id"], upstream if isinstance(upstream, str) else None
    )


def row_on_unserved_route_hold(row: dict[str, Any]) -> bool:
    """The refresh's own hold, exactly as its step writes it: unroutable, with
    that step's reason and the time it was held. A row that is unroutable for
    any other reason (a failed canary, a delisting, a missing price, an
    operator hold) is not on this hold."""
    return (
        row.get("routable") is False
        and row.get("routable_reason") == UNSERVED_ROUTE_HOLD_REASON
        and bool(row.get(UNSERVED_ROUTE_HELD_AT_FIELD))
    )


def provider_fully_on_reviewed_hold(provider: str) -> bool:
    """Every row the provider still lists is held by a reviewed decision."""
    listed = [row for row in manifest_rows(provider) if row.get("routable") is not False]
    return bool(listed) and all(row_on_reviewed_hold(provider, row) for row in listed)


def vendor_prefixes_on_reviewed_hold() -> set[str]:
    """Vendor prefixes with a manifest row that a reviewed hold keeps dark."""
    prefixes: set[str] = set()
    for path in _PROVIDER_MODELS_DIR.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        provider = raw.get("provider") if isinstance(raw, dict) else None
        if not isinstance(provider, str):
            continue
        prefixes.update(
            row["id"].split("/")[0]
            for row in manifest_rows(path.stem)
            if "/" in row["id"] and row_on_reviewed_hold(provider, row)
        )
    return prefixes


# What scripts/pricing/base.py reconcile_manifest_tombstones writes on a
# provider's second miss: the row stays listed, unroutable, with the day it
# went missing.
DELISTED_REASON = "delisted-upstream"


def row_delisted_by_refresh(row: dict[str, Any]) -> bool:
    """The refresh's tombstone for a model the provider stopped listing."""
    return (
        row.get("routable") is False
        and row.get("routable_reason") == DELISTED_REASON
        and bool(row.get("missing_since"))
    )


def row_taken_down_by_refresh(row: dict[str, Any]) -> bool:
    """Every state the hourly refresh can put a routable row in on its own,
    with no review: delisted, held as unserved, or quarantined after a failed
    canary (the reasons provider_manifest_policy accepts for a quarantine).
    A release test that expects a committed row to route accepts these; a row
    unroutable for any other reason (an operator hold, a missing price) still
    fails it."""
    return (
        row_on_unserved_route_hold(row)
        or row_delisted_by_refresh(row)
        or (row.get("routable") is False and row.get("routable_reason") in _CANARY_QUARANTINE_REASONS)
    )


def committed_row(provider: str, model_id: str) -> dict[str, Any] | None:
    """The provider's committed manifest row for the model, if it lists one."""
    return next((row for row in manifest_rows(provider) if row["id"] == model_id), None)


def committed_route_taken_down(endpoint_id: str) -> bool:
    """The committed row behind a route id ("model@provider/kind") is in a
    state the refresh puts rows in on its own (row_taken_down_by_refresh)."""
    model_id, _, rest = endpoint_id.rpartition("@")
    row = committed_row(rest.split("/", 1)[0], model_id)
    return row is not None and row_taken_down_by_refresh(row)
