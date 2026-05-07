from __future__ import annotations

import json
from typing import Any

from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup, utcnow
from trusted_router.synthetic.rollups import (
    apply_sample_to_rollup,
    new_rollup_for_sample,
    rollup_is_within_retention,
    sample_rollup_ids,
)


def write_synthetic_rollups(table: Any, family: str, sample: SyntheticProbeSample) -> None:
    for period, component in sample_rollup_ids(sample):
        update = new_rollup_for_sample(sample, period=period, component=component)
        marker_key = _seen_key(update, sample.id)
        if _row_exists(table, family, marker_key):
            continue
        existing = _read_rollup(table, family, _rollup_key(update))
        if existing is None:
            existing = update
        else:
            apply_sample_to_rollup(existing, sample)
        _write_json_row(table, family, _rollup_key(existing), existing)
        _write_json_row(table, family, marker_key, {"seen": True})


def synthetic_rollups(
    table: Any,
    family: str,
    *,
    period: str | None,
    limit: int,
) -> list[SyntheticRollup]:
    prefix = f"synthetic_rollup#{period}#" if period else "synthetic_rollup#"
    rows = table.read_rows(start_key=prefix.encode("utf-8"), end_key=(prefix + "~").encode("utf-8"), limit=limit)
    rollups = _rollups_from_rows(rows, family)
    filtered = [
        rollup
        for rollup in rollups
        if (period is None or rollup.period == period)
        and rollup_is_within_retention(rollup, now=utcnow())
    ]
    filtered.sort(key=lambda rollup: rollup.period_start, reverse=True)
    return filtered[:limit]


def _rollup_key(rollup: SyntheticRollup) -> bytes:
    parts = [
        "synthetic_rollup",
        rollup.period,
        rollup.period_start,
        rollup.component,
        rollup.target,
        rollup.probe_type,
        rollup.monitor_region,
        rollup.target_region or "-",
    ]
    return "#".join(parts).encode("utf-8")


def _seen_key(rollup: SyntheticRollup, sample_id: str) -> bytes:
    return _rollup_key(rollup).replace(b"synthetic_rollup#", b"synthetic_rollup_seen#", 1) + b"#" + sample_id.encode("utf-8")


def _row_exists(table: Any, family: str, key: bytes) -> bool:
    rows = table.read_rows(start_key=key, end_key=key + b"\x00", limit=1)
    for row in rows:
        cells = row.cells.get(family, {}).get(b"body", [])
        if cells:
            return True
    return False


def _read_rollup(table: Any, family: str, key: bytes) -> SyntheticRollup | None:
    rows = table.read_rows(start_key=key, end_key=key + b"\x00", limit=1)
    rollups = _rollups_from_rows(rows, family)
    return rollups[0] if rollups else None


def _rollups_from_rows(rows: Any, family: str) -> list[SyntheticRollup]:
    rollups: list[SyntheticRollup] = []
    for row in rows:
        cells = row.cells.get(family, {}).get(b"body", [])
        if not cells:
            continue
        try:
            payload = json.loads(cells[0].value.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("period") not in {"hour", "day", "month"}:
                continue
            rollups.append(SyntheticRollup(**payload))
        except (TypeError, ValueError):
            continue
    return rollups


def _write_json_row(table: Any, family: str, key: bytes, value: Any) -> None:
    row = table.direct_row(key)
    row.set_cell(family, b"body", json_body(value).encode("utf-8"))
    row.commit()
