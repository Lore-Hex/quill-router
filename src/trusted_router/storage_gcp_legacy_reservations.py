"""Bounded legacy-reservation guard for typed-ledger maintenance."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

_LIVE_MAX_AGE = dt.timedelta(hours=24)
_CACHE_ATTR = "_typed_maintenance_legacy_reservation_snapshot"


@dataclass
class LegacyReservationSnapshot:
    live_by_workspace: dict[str, int] = field(default_factory=dict)
    stale_by_workspace: dict[str, int] = field(default_factory=dict)
    live_by_key: dict[str, int] = field(default_factory=dict)
    stale_by_key: dict[str, int] = field(default_factory=dict)


def legacy_reservation_snapshot(
    store: Any,
    *,
    now: dt.datetime | None = None,
) -> LegacyReservationSnapshot:
    """Return compact counts for unsettled legacy reservations.

    Typed billing is authoritative, but a genuinely live request from a rolling
    legacy revision must still block a ledger reshard. Reservations newer than
    24 hours, plus rows with malformed/missing timestamps, therefore remain
    blocking. Older rows are pre-cutover debris: retain them for audit, report
    them, but do not let them permanently prevent typed-ledger maintenance.

    The aggregate scan is cached on the short-lived operator store so a
    workspace with many keys scans the legacy reservation range only once.
    """
    cached = getattr(store, _CACHE_ATTR, None)
    if isinstance(cached, LegacyReservationSnapshot):
        return cached

    observed_at = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    cutoff = observed_at - _LIVE_MAX_AGE
    pt = store._param_types
    sql = """
        /* legacy_reshard_guard */
        SELECT
          JSON_VALUE(body, '$.workspace_id') AS workspace_id,
          JSON_VALUE(body, '$.key_hash') AS key_hash,
          COUNTIF(
            SAFE_CAST(JSON_VALUE(body, '$.created_at') AS TIMESTAMP) IS NULL
            OR SAFE_CAST(JSON_VALUE(body, '$.created_at') AS TIMESTAMP) >= @cutoff
          ) AS live_count,
          COUNTIF(
            SAFE_CAST(JSON_VALUE(body, '$.created_at') AS TIMESTAMP) < @cutoff
          ) AS stale_count
        FROM tr_entities
        WHERE kind='reservation' AND JSON_VALUE(body, '$.settled')='false'
        GROUP BY workspace_id, key_hash
    """
    result = LegacyReservationSnapshot()
    with store._database.snapshot() as snapshot:
        rows = snapshot.execute_sql(
            sql,
            params={"cutoff": cutoff},
            param_types={"cutoff": pt.TIMESTAMP},
        )
        for workspace_id, key_hash, live_count, stale_count in rows:
            live = int(live_count or 0)
            stale = int(stale_count or 0)
            if workspace_id:
                workspace = str(workspace_id)
                result.live_by_workspace[workspace] = (
                    result.live_by_workspace.get(workspace, 0) + live
                )
                result.stale_by_workspace[workspace] = (
                    result.stale_by_workspace.get(workspace, 0) + stale
                )
            if key_hash:
                key = str(key_hash)
                result.live_by_key[key] = result.live_by_key.get(key, 0) + live
                result.stale_by_key[key] = result.stale_by_key.get(key, 0) + stale

    setattr(store, _CACHE_ATTR, result)
    return result
