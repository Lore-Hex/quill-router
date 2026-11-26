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
