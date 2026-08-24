"""Backfill bounded Spanner generation lookup rows from the legacy Bigtable."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
from collections.abc import Iterable, Iterator
from typing import Any, TypeVar

from google.cloud import bigtable, spanner
from google.cloud.bigtable.row_filters import (
    CellsColumnLimitFilter,
    RowFilterChain,
    TimestampRange,
    TimestampRangeFilter,
)
from google.cloud.spanner_v1 import KeySet

from trusted_router.storage_gcp_generation_records import generation_record_body
from trusted_router.storage_models import Generation

PROJECT = "quill-cloud-proxy"
BIGTABLE_INSTANCE = "trusted-router-logs"
BIGTABLE_TABLE = "trustedrouter-generations"
SPANNER_INSTANCE = "trusted-router-nam6"
SPANNER_DATABASE = "trusted-router"
SPANNER_TABLE = "tr_generation"
FAMILIES = ("activity", "m")
T = TypeVar("T")


def _batches(items: Iterable[T], size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _created_at(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _generation(row: Any) -> Generation | None:
    for family in FAMILIES:
        cells = row.cells.get(family, {}).get(b"body", [])
        if not cells:
            continue
        try:
            payload = json.loads(cells[0].value.decode())
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        known = {field.name for field in dataclasses.fields(Generation)}
        try:
            return Generation(
                **{key: value for key, value in payload.items() if key in known}
            )
        except (TypeError, ValueError):
            return None
    return None


def _iter_recent(table: Any, *, cutoff: dt.datetime) -> Iterator[Generation]:
    filter_ = RowFilterChain(
        filters=[
            TimestampRangeFilter(TimestampRange(start=cutoff)),
            CellsColumnLimitFilter(1),
        ]
    )
    rows = table.read_rows(
        start_key=b"gen#",
        end_key=b"gen#~",
        filter_=filter_,
    )
    for row in rows:
        generation = _generation(row)
        if generation is not None and _created_at(generation.created_at) >= cutoff:
            yield generation


def _write(database: Any, generations: list[Generation]) -> None:
    columns = (
        "generation_id",
        "workspace_id",
        "key_hash",
        "created_at",
        "terminal_at",
        "payload",
    )
    values = []
    for generation in generations:
        created = _created_at(generation.created_at)
        values.append(
            (
                generation.id,
                generation.workspace_id,
                generation.key_hash,
                created,
                created,
                generation_record_body(generation),
            )
        )
    with database.batch() as batch:
        batch.insert_or_update(
            table=SPANNER_TABLE,
            columns=columns,
            values=values,
        )


def _existing_ids(database: Any, generation_ids: list[str]) -> set[str]:
    if not generation_ids:
        return set()
    with database.snapshot() as snapshot:
        rows = snapshot.read(
            SPANNER_TABLE,
            columns=("generation_id",),
            keyset=KeySet(keys=[(generation_id,) for generation_id in generation_ids]),
        )
        return {str(row[0]) for row in rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.days < 1 or args.batch_size < 1:
        raise SystemExit("--days and --batch-size must be positive")

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days)
    table = (
        bigtable.Client(project=PROJECT, admin=False)
        .instance(BIGTABLE_INSTANCE)
        .table(BIGTABLE_TABLE)
    )
    database = (
        spanner.Client(project=PROJECT, disable_builtin_metrics=True)
        .instance(SPANNER_INSTANCE)
        .database(SPANNER_DATABASE)
    )
    scanned = 0
    written = 0
    missing = 0
    for batch in _batches(_iter_recent(table, cutoff=cutoff), args.batch_size):
        scanned += len(batch)
        if args.apply:
            _write(database, batch)
            written += len(batch)
        if args.verify:
            expected = {generation.id for generation in batch}
            missing += len(expected - _existing_ids(database, sorted(expected)))
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "scanned": scanned,
                    "written": written,
                    "missing": missing,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    if scanned == 0:
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "scanned": 0,
                    "written": 0,
                    "missing": 0,
                }
            )
        )
    return 1 if args.verify and missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
