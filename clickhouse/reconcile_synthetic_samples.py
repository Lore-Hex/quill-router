"""Repair bounded Bigtable synthetic metadata gaps in ClickHouse.

The Spanner outbox is the live delivery path. Bigtable remains a bounded
shadow during the migration soak, so this worker uses it only as a repair
source when an immutable synthetic sample ID is absent from ClickHouse.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from google.cloud import bigtable
from google.cloud.bigtable.row_filters import CellsColumnLimitFilter

from clickhouse.backfill_operational_analytics import ClickHouse
from clickhouse.ingest_operational_outbox import (
    CanonicalOperationalEvent,
    ClickHouseOperationalWriter,
)
from clickhouse.operational_fingerprint import SAFE_ID
from clickhouse.verify_operational_parity import _body, _parse, _stable_source_write
from trusted_router.storage_gcp_operational_analytics_outbox import synthetic_payload
from trusted_router.storage_models import SyntheticProbeSample

PROJECT = "quill-cloud-proxy"
INSTANCE = "trusted-router-logs"
TABLE = "trustedrouter-generations"
DATABASE = "tr"
DEFAULT_DAYS = 3
DEFAULT_PER_DAY_LIMIT = 100_000
DEFAULT_GRACE_SECONDS = 10 * 60
DEFAULT_BATCH_SIZE = 5_000


class EventWriter(Protocol):
    def insert(self, events: list[CanonicalOperationalEvent]) -> None: ...


class ClickHouseQuery(Protocol):
    def query(
        self,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        external_ids: bool = False,
    ) -> str: ...


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int
    present: int
    missing_before: int
    repaired: int
    unresolved: int
    missing_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.unresolved == 0


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _day_prefix(day: dt.date) -> bytes:
    return f"synthetic_day_recent#{day.isoformat()}#".encode()


def source_samples(
    table: Any,
    *,
    now: dt.datetime,
    days: int,
    per_day_limit: int,
    grace_seconds: int,
) -> dict[str, dict[str, Any]]:
    """Read complete recent day indexes and return immutable canonical rows."""
    if days < 1:
        raise ValueError("days must be positive")
    if per_day_limit < 1:
        raise ValueError("per_day_limit must be positive")
    if grace_seconds < 0:
        raise ValueError("grace_seconds cannot be negative")

    cutoff = _utc(now) - dt.timedelta(seconds=grace_seconds)
    result: dict[str, dict[str, Any]] = {}
    for offset in range(days):
        day = cutoff.date() - dt.timedelta(days=offset)
        prefix = _day_prefix(day)
        rows = table.read_rows(
            start_key=prefix,
            end_key=prefix + b"~",
            limit=per_day_limit + 1,
            filter_=CellsColumnLimitFilter(1),
        )
        scanned_for_day = 0
        for row in rows:
            scanned_for_day += 1
            if scanned_for_day > per_day_limit:
                raise RuntimeError(
                    f"synthetic source day {day.isoformat()} exceeded "
                    f"the {per_day_limit} row safety limit"
                )
            if not _stable_source_write(
                row,
                families=("synthetic", "m"),
                cutoff=cutoff,
            ):
                continue
            sample = _parse(
                SyntheticProbeSample,
                _body(row, ("synthetic", "m")),
            )
            if sample is None:
                continue
            try:
                created_at = dt.datetime.fromisoformat(
                    sample.created_at.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if _utc(created_at) > cutoff:
                continue
            result.setdefault(sample.id, synthetic_payload(sample))
    return result


def clickhouse_ids(clickhouse: ClickHouseQuery, ids: Iterable[str]) -> set[str]:
    wanted = list(ids)
    if not wanted:
        return set()
    if any(SAFE_ID.fullmatch(item) is None for item in wanted):
        raise ValueError("source contains an invalid record ID")
    payload = ("\n".join(wanted) + "\n").encode()
    output = clickhouse.query(
        "SELECT id FROM synthetic_probe_samples FINAL "
        "WHERE id IN (SELECT id FROM wanted) FORMAT TabSeparated",
        input_bytes=payload,
        external_ids=True,
    )
    return {line.strip() for line in output.splitlines() if line.strip()}


def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def reconcile(
    *,
    source: dict[str, dict[str, Any]],
    clickhouse: ClickHouseQuery,
    writer: EventWriter,
    apply: bool,
    batch_size: int,
    now: dt.datetime,
) -> ReconcileResult:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    present = clickhouse_ids(clickhouse, source)
    missing = sorted(set(source) - present)
    if apply:
        ingest_version = _utc(now).isoformat()
        for batch in _batches(missing, batch_size):
            writer.insert(
                [
                    CanonicalOperationalEvent(
                        event_kind="synthetic",
                        row={**source[sample_id], "ingest_version": ingest_version},
                    )
                    for sample_id in batch
                ]
            )
    unresolved = sorted(set(missing) - clickhouse_ids(clickhouse, missing))
    repaired = len(missing) - len(unresolved) if apply else 0
    return ReconcileResult(
        scanned=len(source),
        present=len(present),
        missing_before=len(missing),
        repaired=repaired,
        unresolved=len(unresolved),
        missing_ids=tuple(missing[:20]),
        unresolved_ids=tuple(unresolved[:20]),
    )


def _write_history(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=16)
    retained: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                checked_at = dt.datetime.fromisoformat(
                    str(row["checked_at"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                continue
            if _utc(checked_at) >= cutoff:
                retained.append(row)
    retained.append(payload)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in retained)
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--per-day-limit", type=int, default=DEFAULT_PER_DAY_LIMIT)
    parser.add_argument("--grace-seconds", type=int, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--history-file",
        default="/var/lib/tr-clickhouse-ingest/synthetic-reconcile.jsonl",
    )
    args = parser.parse_args()
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")

    now = dt.datetime.now(dt.UTC)
    table = (
        bigtable.Client(project=PROJECT, admin=False)
        .instance(INSTANCE)
        .table(TABLE)
    )
    clickhouse = ClickHouse(password=password)
    writer = ClickHouseOperationalWriter(password=password, database=DATABASE)
    source = source_samples(
        table,
        now=now,
        days=args.days,
        per_day_limit=args.per_day_limit,
        grace_seconds=args.grace_seconds,
    )
    result = reconcile(
        source=source,
        clickhouse=clickhouse,
        writer=writer,
        apply=args.apply,
        batch_size=args.batch_size,
        now=now,
    )
    payload = {
        "checked_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "mode": "apply" if args.apply else "dry-run",
        "ok": result.ok,
        "scanned": result.scanned,
        "present": result.present,
        "missing_before": result.missing_before,
        "repaired": result.repaired,
        "unresolved": result.unresolved,
        "missing_ids": list(result.missing_ids),
        "unresolved_ids": list(result.unresolved_ids),
        # The five-minute rollup worker consumes repaired raw rows. Keeping
        # repair and rollup as separate single-purpose services prevents two
        # full 14-day rollup rebuilds from racing each other.
        "rollup_rebuild_pending": bool(result.repaired),
    }
    _write_history(Path(args.history_file), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
