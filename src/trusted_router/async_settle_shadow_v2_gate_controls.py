"""The single control-row population selector; other subtypes remain originals."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import Key, Row


def manifests(rows: Iterable[Row] | Mapping[Key, dict[str, Any]]) -> list[dict[str, Any]]:
    """Select exact day keys and the frozen schema before any population join.

    Invalid rows are diagnosed by the original-row boundary, not converted to
    missing schemas here. Caps and retention fences are not population rosters.
    """
    result = []
    candidates = rows.items() if isinstance(rows, Mapping) else ((row.key, row.raw) for row in rows)
    for key, original in candidates:
        if key.kind != p1.KINDS[5] or not key.id.endswith('/manifest-v2'):
            continue
        try:
            if isinstance(original, bytes):
                value = a1.MANIFEST.parse(original)
            else:
                a1.MANIFEST(original)
                value = original
            if key.id == value['day'] + '/manifest-v2':
                result.append(value)
        except ValueError:
            continue
    return result


def traffic_regions(rows: Iterable[Row], inventory: Any, policy: dict[str, Any]) -> set[str]:
    """Reviewed seed subset plus terminal regions; the writer inventory stays full."""
    from trusted_router.async_settle_shadow_v2_schema import require
    reviewed = set(policy['required_regions'])
    writers = {b.boot: b.region for b in inventory.boots if b.role == 'router'}
    require(bool(reviewed) and reviewed <= set(writers.values()), 'evidence_schema_gap')
    terminal_regions = set()
    for row in rows:
        if row.key.kind != p1.KINDS[2]:
            continue
        page = a1.validate_row(row.key.kind, row.key.id, row.raw)
        if any(e['phase'] in ('settle', 'refund') for e in page['events']):
            require(page['boot'] in writers, 'evidence_schema_gap')
            terminal_regions.add(writers[page['boot']])
    require(terminal_regions <= reviewed, 'evidence_schema_gap')
    return reviewed | terminal_regions
