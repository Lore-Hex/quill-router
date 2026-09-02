"""Canonical fingerprints for operational source-to-ClickHouse delivery checks."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import struct
from typing import Any, Protocol

from clickhouse.ingest_operational_outbox import (
    ACTIVITY_BOOLEAN_COLUMNS,
    ACTIVITY_OPTIONAL_DEFAULTS,
)
from trusted_router.synthetic.rollups import ROLLUP_HISTOGRAM_FIELDS, compact_histogram

SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class ClickHouseQuery(Protocol):
    def query(
        self,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        external_ids: bool = False,
    ) -> str: ...


def canonical_fingerprint(payload: dict[str, Any], *, surface: str) -> str:
    canonical = dict(payload)
    canonical.pop("ingest_version", None)
    canonical.pop("updated_at", None)
    for field in ("created_at", "period_start", "last_checked_at"):
        if canonical.get(field) is not None:
            canonical[field] = _iso(canonical[field])
    if surface == "synthetic":
        for field in ("connection_reused", "output_match"):
            if canonical.get(field) is not None:
                canonical[field] = bool(canonical[field])
    if surface == "activity":
        for field, default in ACTIVITY_OPTIONAL_DEFAULTS.items():
            if canonical.get(field) is None and default is not None:
                canonical[field] = default
        for field in ACTIVITY_BOOLEAN_COLUMNS:
            if canonical.get(field) is not None:
                canonical[field] = bool(canonical[field])
        if canonical.get("speed_tokens_per_second") is not None:
            canonical["speed_tokens_per_second"] = float(
                canonical["speed_tokens_per_second"]
            )
    if surface == "rollup":
        if canonical.get("target_region") is None:
            canonical["target_region"] = ""
        # A source row for a closed period keeps its pre-bucketing exact keys
        # (never rewritten) while the rebuilt row is bucketed; fold both so
        # the parity timer compares shape, not key granularity.
        for field in ROLLUP_HISTOGRAM_FIELDS:
            histogram = canonical.get(field)
            if not isinstance(histogram, dict):
                continue
            try:
                canonical[field] = compact_histogram(histogram)
            except (TypeError, ValueError):
                continue
    if surface == "benchmark" and canonical.get("speed_tokens_per_second") is not None:
        canonical["speed_tokens_per_second"] = struct.unpack(
            "!f",
            struct.pack("!f", float(canonical["speed_tokens_per_second"])),
        )[0]
    return hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def clickhouse_rows(
    clickhouse: ClickHouseQuery,
    *,
    table: str,
    id_column: str,
    ids: list[str],
) -> dict[str, dict[str, Any]]:
    allowed = {
        ("provider_benchmark_samples", "id"),
        ("activity_generations", "generation_id"),
        ("synthetic_probe_samples", "id"),
        ("synthetic_status_rollups", "id"),
    }
    if (table, id_column) not in allowed:
        raise ValueError("unsupported parity table")
    if not ids:
        return {}
    if any(SAFE_ID.fullmatch(item) is None for item in ids):
        raise ValueError("source contains an invalid record ID")
    payload = ("\n".join(ids) + "\n").encode()
    result = clickhouse.query(
        f"SELECT * EXCEPT ingest_version FROM {table} FINAL "  # noqa: S608
        f"WHERE {id_column} IN (SELECT id FROM wanted) "
        "FORMAT JSONEachRow",
        input_bytes=payload,
        external_ids=True,
    )
    rows: dict[str, dict[str, Any]] = {}
    for line in result.splitlines():
        row = json.loads(line)
        if isinstance(row, dict):
            rows[str(row[id_column])] = row
    return rows


def _iso(value: Any) -> str:
    text = str(value).replace(" ", "T")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text if text.endswith("Z") else text + "Z"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return (
        parsed.astimezone(dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
