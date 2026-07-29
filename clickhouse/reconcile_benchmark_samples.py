"""Compare recent Bigtable benchmark rows with ClickHouse ``FINAL`` row sets."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from collections import defaultdict
from typing import Any

from google.cloud import bigtable
from google.cloud.bigtable.row_set import RowSet

from clickhouse.backfill_benchmark_samples import FAMILIES, ch, normalise

PROJECT = "quill-cloud-proxy"
BIGTABLE_INSTANCE = "trusted-router-logs"
BIGTABLE_TABLE = "trustedrouter-generations"
CLICKHOUSE_TABLE = "provider_benchmark_samples"

log = logging.getLogger("trusted_router.analytics_reconciler")

CLICKHOUSE_COLUMNS = (
    "id, created_at, provider, model, provider_name, status, usage_type, source, "
    "streamed, input_tokens, output_tokens, total_cost_microdollars, "
    "speed_tokens_per_second, elapsed_milliseconds, first_token_milliseconds, "
    "ttfb_milliseconds, finish_reason, error_type, error_status, error_message, "
    "region, app"
)


def _fingerprint(row: dict[str, Any]) -> str:
    encoded = json.dumps(
        row,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reverse_time_key(value: dt.datetime) -> str:
    epoch_ms = int(value.timestamp() * 1000)
    return f"{9_999_999_999_999 - epoch_ms:013d}"


def _add_row(
    target: dict[str, dict[str, str]],
    row: dict[str, Any],
    *,
    cutoff: dt.datetime,
    upper: dt.datetime,
) -> None:
    canonical = normalise(row)
    if canonical is None:
        return
    created = dt.datetime.fromisoformat(canonical["created_at"]).replace(tzinfo=dt.UTC)
    if created < cutoff or created >= upper:
        return
    target.setdefault(canonical["created_at"][:10], {})[canonical["id"]] = _fingerprint(
        canonical
    )


def bigtable_rows(
    *,
    project: str,
    instance: str,
    table_name: str,
    cutoff: dt.datetime,
    upper: dt.datetime,
    max_rows: int,
) -> tuple[dict[str, dict[str, str]], int, bool]:
    client = bigtable.Client(project=project, admin=False)
    table = client.instance(instance).table(table_name)
    row_set = RowSet()
    # The reverse timestamp is event time, so it is invalid as a live cursor.
    # It is useful for this bounded, periodic completeness window: ask
    # Bigtable to stop at the wall-clock cutoff instead of scanning all
    # history and silently hitting the safety cap.
    row_set.add_row_range_from_keys(
        b"benchmark_recent#",
        f"benchmark_recent#{_reverse_time_key(cutoff)}~".encode(),
    )
    by_day: dict[str, dict[str, str]] = defaultdict(dict)
    scanned = 0
    for row in table.read_rows(row_set=row_set, limit=max_rows + 1):
        scanned += 1
        if scanned > max_rows:
            return dict(by_day), scanned, True
        cells = []
        for family in FAMILIES:
            cells = row.cells.get(family, {}).get(b"body", [])
            if cells:
                break
        if not cells:
            continue
        try:
            raw = json.loads(cells[0].value.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(raw, dict):
            _add_row(by_day, raw, cutoff=cutoff, upper=upper)
    return dict(by_day), scanned, False


def clickhouse_rows(
    *,
    cutoff: dt.datetime,
    upper: dt.datetime,
) -> dict[str, dict[str, str]]:
    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    upper_text = upper.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    output = ch(
        f"SELECT {CLICKHOUSE_COLUMNS} FROM {CLICKHOUSE_TABLE} FINAL "  # noqa: S608
        f"WHERE created_at >= toDateTime64('{cutoff_text}', 3, 'UTC') "
        f"AND created_at < toDateTime64('{upper_text}', 3, 'UTC') "
        "FORMAT JSONEachRow"
    )
    by_day: dict[str, dict[str, str]] = defaultdict(dict)
    for line in output.splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if isinstance(raw, dict):
            _add_row(by_day, raw, cutoff=cutoff, upper=upper)
    return dict(by_day)


def report(
    source: dict[str, dict[str, str]],
    destination: dict[str, dict[str, str]],
) -> int:
    mismatched_days = 0
    for day in sorted(set(source) | set(destination), reverse=True):
        source_rows = source.get(day, {})
        destination_rows = destination.get(day, {})
        source_ids = set(source_rows)
        destination_ids = set(destination_rows)
        missing_in_clickhouse = source_ids - destination_ids
        missing_in_bigtable = destination_ids - source_ids
        changed = {
            event_id
            for event_id in source_ids & destination_ids
            if source_rows[event_id] != destination_rows[event_id]
        }
        if missing_in_clickhouse or missing_in_bigtable or changed:
            mismatched_days += 1
        log.info(
            "analytics_reconciler.daily day=%s bigtable_rows=%d clickhouse_rows=%d "
            "bigtable_minus_clickhouse=%d clickhouse_minus_bigtable=%d changed=%d",
            day,
            len(source_rows),
            len(destination_rows),
            len(missing_in_clickhouse),
            len(missing_in_bigtable),
            len(changed),
        )
    return mismatched_days


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--settle-delay-minutes", type=int, default=15)
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", PROJECT))
    parser.add_argument(
        "--bigtable-instance",
        default=os.environ.get("BIGTABLE_INSTANCE_ID", BIGTABLE_INSTANCE),
    )
    parser.add_argument(
        "--bigtable-table",
        default=os.environ.get("BIGTABLE_TABLE_ID", BIGTABLE_TABLE),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    upper = dt.datetime.now(dt.UTC) - dt.timedelta(
        minutes=max(args.settle_delay_minutes, 0)
    )
    cutoff = upper - dt.timedelta(days=max(args.days, 1))
    source, scanned, truncated = bigtable_rows(
        project=args.project,
        instance=args.bigtable_instance,
        table_name=args.bigtable_table,
        cutoff=cutoff,
        upper=upper,
        max_rows=args.max_rows,
    )
    if truncated:
        log.error(
            "analytics_reconciler.source_truncated scanned=%d max_rows=%d",
            scanned,
            args.max_rows,
        )
        return 2
    destination = clickhouse_rows(cutoff=cutoff, upper=upper)
    mismatched_days = report(source, destination)
    log.info(
        "analytics_reconciler.summary scanned=%d days=%d mismatched_days=%d "
        "window_start=%s window_end=%s",
        scanned,
        len(set(source) | set(destination)),
        mismatched_days,
        cutoff.isoformat(),
        upper.isoformat(),
    )
    return 1 if mismatched_days else 0


if __name__ == "__main__":
    raise SystemExit(main())
