"""Roll settled reservation overruns from Spanner into ClickHouse each hour.

The 2026-08-25 settle convoy showed that billing contention needs a warehouse
view without adding work to the settlement transaction itself. Over 31 days,
12.2% of settlements exceeded their hold ($45.57/day), while 96% of overrun
dollars came from roughly 0.7% of settlements. This job therefore recomputes
the two most recently closed UTC hours from authoritative ``tr_reservation``
rows; the overlap and ``ReplacingMergeTree(refreshed_at)`` make reruns
idempotent while keeping analytics entirely off the money path.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

PROJECT = "quill-cloud-proxy"
SPANNER_INSTANCE = "trusted-router-nam6"
SPANNER_DATABASE = "trusted-router"
CLICKHOUSE_URL = "http://localhost:8123/"
OVERRUN_TABLE = "tr.reservation_overruns"
SPANNER_SCOPE = "https://www.googleapis.com/auth/spanner.data"


class ReservationSource(Protocol):
    def fetch(
        self,
        *,
        window_start: dt.datetime,
        window_end: dt.datetime,
    ) -> list[Mapping[str, Any]]: ...


class OverrunWriter(Protocol):
    def upsert(self, rows: list[dict[str, Any]]) -> None: ...


@dataclass(frozen=True)
class OverrunAggregate:
    settled_n: int
    overrun_n: int
    overrun_micro: int
    max_single_overrun_micro: int


@dataclass(frozen=True)
class RollupResult:
    source_rows: int
    rollup_rows: int
    window_start: dt.datetime
    window_end: dt.datetime


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _parse_timestamp(value: Any, *, field: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"reservation {field} is not an ISO timestamp") from exc
    else:
        raise ValueError(f"reservation {field} is not an ISO timestamp")
    return _utc(parsed)


def _spanner_timestamp(value: dt.datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _clickhouse_datetime(value: dt.datetime) -> str:
    return _utc(value).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def aggregate_reservation_overruns(
    rows: list[Mapping[str, Any]],
) -> dict[tuple[dt.datetime, str], OverrunAggregate]:
    """Aggregate settled reservation rows by UTC hour and hold usage type."""
    counters: dict[tuple[dt.datetime, str], list[int]] = {}
    for row in rows:
        if not bool(row.get("settled")):
            continue
        terminal_at = row.get("terminal_at")
        if terminal_at is None:
            continue
        hour = _parse_timestamp(terminal_at, field="terminal_at").replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        hold_usage_type = str(row.get("hold_usage_type") or "unknown")
        actual_micro = int(row.get("actual_micro") or 0)
        reserved_micro = int(row.get("credit_reserved_micro") or 0)
        overrun = max(0, actual_micro - reserved_micro)
        values = counters.setdefault((hour, hold_usage_type), [0, 0, 0, 0])
        values[0] += 1
        if overrun > 0:
            values[1] += 1
            values[2] += overrun
            values[3] = max(values[3], overrun)
    return {
        key: OverrunAggregate(
            settled_n=values[0],
            overrun_n=values[1],
            overrun_micro=values[2],
            max_single_overrun_micro=values[3],
        )
        for key, values in counters.items()
    }


def build_clickhouse_rows(
    aggregates: Mapping[tuple[dt.datetime, str], OverrunAggregate],
    *,
    refreshed_at: dt.datetime,
) -> list[dict[str, Any]]:
    """Construct stable JSONEachRow payloads from pure aggregate values."""
    refreshed = _clickhouse_datetime(refreshed_at)
    return [
        {
            "hour": _clickhouse_datetime(hour),
            "hold_usage_type": hold_usage_type,
            "settled_n": aggregate.settled_n,
            "overrun_n": aggregate.overrun_n,
            "overrun_micro": aggregate.overrun_micro,
            "max_single_overrun_micro": aggregate.max_single_overrun_micro,
            "refreshed_at": refreshed,
        }
        for (hour, hold_usage_type), aggregate in sorted(aggregates.items())
    ]


class SpannerReservationSource:
    """Read settled reservations through Spanner REST using ADC credentials."""

    def __init__(self, *, project: str, instance: str, database: str) -> None:
        from google.auth import default

        self._database_url = (
            "https://spanner.googleapis.com/v1/projects/"
            f"{urllib.parse.quote(project, safe='')}/instances/"
            f"{urllib.parse.quote(instance, safe='')}/databases/"
            f"{urllib.parse.quote(database, safe='')}"
        )
        self._credentials, _ = default(scopes=[SPANNER_SCOPE])

    def _token(self) -> str:
        from google.auth.transport.requests import Request

        if not self._credentials.valid:
            self._credentials.refresh(Request())
        token = self._credentials.token
        if not token:
            raise RuntimeError("ADC did not provide a Spanner access token")
        return str(token)

    def _request_json(
        self,
        url: str,
        *,
        method: str = "POST",
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = urllib.request.Request(  # noqa: S310 - fixed Google API origin
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
            payload = response.read()
        if not payload:
            return {}
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("Spanner REST response is not a JSON object")
        return parsed

    def fetch(
        self,
        *,
        window_start: dt.datetime,
        window_end: dt.datetime,
    ) -> list[Mapping[str, Any]]:
        session = self._request_json(f"{self._database_url}/sessions", body={})
        session_name = str(session.get("name") or "")
        if not session_name:
            raise ValueError("Spanner createSession response omitted the session name")
        session_url = "https://spanner.googleapis.com/v1/" + session_name
        try:
            result = self._request_json(
                f"{session_url}:executeSql",
                body={
                    "sql": (
                        "SELECT terminal_at, hold_usage_type, actual_micro, "
                        "credit_reserved_micro, settled FROM tr_reservation "
                        "WHERE settled = true AND terminal_at >= @window_start "
                        "AND terminal_at < @window_end"
                    ),
                    "params": {
                        "window_start": _spanner_timestamp(window_start),
                        "window_end": _spanner_timestamp(window_end),
                    },
                    "paramTypes": {
                        "window_start": {"code": "TIMESTAMP"},
                        "window_end": {"code": "TIMESTAMP"},
                    },
                    "transaction": {"singleUse": {"readOnly": {"strong": True}}},
                },
            )
        finally:
            self._request_json(session_url, method="DELETE")

        metadata = result.get("metadata")
        row_type = metadata.get("rowType") if isinstance(metadata, dict) else None
        fields = row_type.get("fields") if isinstance(row_type, dict) else None
        if not isinstance(fields, list):
            raise ValueError("Spanner executeSql response omitted row metadata")
        names = [str(field["name"]) for field in fields]
        raw_rows = result.get("rows", [])
        if not isinstance(raw_rows, list):
            raise ValueError("Spanner executeSql rows are not a list")
        return [dict(zip(names, row, strict=True)) for row in raw_rows]


class ClickHouseOverrunWriter:
    """Synchronous JSONEachRow inserts through the ClickHouse HTTP interface."""

    def __init__(self, *, url: str, password: str) -> None:
        self._url = url
        self._password = password

    def upsert(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payload = (
            "\n".join(json.dumps(row, separators=(",", ":"), sort_keys=True) for row in rows)
            + "\n"
        ).encode()
        separator = "&" if "?" in self._url else "?"
        endpoint = self._url + separator + urllib.parse.urlencode(
            {"query": f"INSERT INTO {OVERRUN_TABLE} FORMAT JSONEachRow"}
        )
        request = urllib.request.Request(  # noqa: S310 - configured node URL
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/x-ndjson",
                "X-ClickHouse-User": "tr",
                "X-ClickHouse-Key": self._password,
            },
        )
        with urllib.request.urlopen(request, timeout=300):  # noqa: S310 - configured node URL
            pass


def rollup_reservation_overruns(
    source: ReservationSource,
    writer: OverrunWriter | None,
    *,
    dry_run: bool = False,
    started_at: dt.datetime | None = None,
) -> RollupResult:
    """Read and replace the two most recently closed UTC hourly buckets."""
    job_start = _utc(started_at or dt.datetime.now(dt.UTC)).replace(microsecond=0)
    window_end = job_start.replace(minute=0, second=0, microsecond=0)
    window_start = window_end - dt.timedelta(hours=2)
    source_rows = source.fetch(window_start=window_start, window_end=window_end)
    rows = build_clickhouse_rows(
        aggregate_reservation_overruns(source_rows),
        refreshed_at=job_start,
    )
    if not dry_run:
        if writer is None:
            raise ValueError("writer is required unless dry-run is selected")
        writer.upsert(rows)
    return RollupResult(
        source_rows=len(source_rows),
        rollup_rows=len(rows),
        window_start=window_start,
        window_end=window_end,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", PROJECT))
    parser.add_argument(
        "--spanner-instance",
        default=os.environ.get("SPANNER_INSTANCE_ID", SPANNER_INSTANCE),
    )
    parser.add_argument(
        "--spanner-database",
        default=os.environ.get("SPANNER_DATABASE_ID", SPANNER_DATABASE),
    )
    parser.add_argument(
        "--clickhouse-url",
        default=os.environ.get("CLICKHOUSE_URL", CLICKHOUSE_URL),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    source: ReservationSource | None = None,
    writer: OverrunWriter | None = None,
) -> int:
    args = _parse_args(argv)
    selected_source = source or SpannerReservationSource(
        project=args.project,
        instance=args.spanner_instance,
        database=args.spanner_database,
    )
    selected_writer = writer
    if not args.dry_run and selected_writer is None:
        selected_writer = ClickHouseOverrunWriter(
            url=args.clickhouse_url,
            password=os.environ["CH_PASSWORD"],
        )
    result = rollup_reservation_overruns(
        selected_source,
        selected_writer,
        dry_run=args.dry_run,
        started_at=dt.datetime.now(dt.UTC),
    )
    print(
        f"source_rows={result.source_rows} "
        f"reservation_overrun_rows={result.rollup_rows} "
        f"window_start={_spanner_timestamp(result.window_start)} "
        f"window_end={_spanner_timestamp(result.window_end)} "
        f"dry_run={str(args.dry_run).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
