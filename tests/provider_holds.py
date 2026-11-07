"""Live provider-state checks skip what a reviewed hold keeps dark on purpose.

The holds live in trusted_router.provider_contracts; nothing here names a
provider. A hold is a decision, not drift, so a freshness report must not
fail on it, and the hold is cleared only through its own reviewed process.
"""

from __future__ import annotations

import json
from typing import Any

from trusted_router.catalog_ingest import _PROVIDER_MODELS_DIR
from trusted_router.provider_contracts import provider_route_reviewed_hold


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
