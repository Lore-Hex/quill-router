"""Verify Postgres generation records reached the plane's ClickHouse."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clickhouse.ingest_operational_outbox_postgres import (
    CLICKHOUSE_ENV_PREFIX,
    PostgresOperationalOutboxSource,
)
from clickhouse.operational_fingerprint import SAFE_ID
from clickhouse.verify_spanner_delivery import _write_history
from trusted_router.operational_analytics import OperationalAnalyticsClient

FIELDS = (
    "total_cost_microdollars",
    "tokens_prompt",
    "tokens_completion",
    "cached_input_tokens",
    "reasoning_tokens",
    "model",
    "provider",
    "status",
    "created_at",
)
PAGE_SIZE = 500
QUERY_BOUNDS = (
    " SETTINGS max_rows_to_read = 1000000, read_overflow_mode = 'throw', "
    "max_execution_time = 30 FORMAT JSON"
)
# Generation writes do not populate indexed_at. Filter only after this bounded
# primary-key read; a WHERE predicate on body.created_at would scan all history.
SELECT_PAGE_SQL = (
    "SELECT id, body ->> 'total_cost_microdollars', "
    "body ->> 'tokens_prompt', body ->> 'tokens_completion', "
    "body ->> 'cached_input_tokens', body ->> 'reasoning_tokens', "
    "body ->> 'model', body ->> 'provider', body ->> 'status', "
    "body ->> 'created_at', updated_at "
    "FROM tr_entities WHERE kind = 'generation' AND id > %s ORDER BY id LIMIT %s"
)


def _timestamp(value: Any) -> dt.datetime:
    parsed = (
        value
        if isinstance(value, dt.datetime)
        else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)


@dataclass
class GenerationWindow:
    rows: dict[str, dict[str, Any]]
    settled_at: dict[str, dt.datetime]
    known_ids: set[str]
    scanned: int
    complete: bool


class PostgresGenerationSource(PostgresOperationalOutboxSource):
    def fetch(
        self,
        *,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
        scan_limit: int,
    ) -> GenerationWindow:
        result = GenerationWindow({}, {}, set(), 0, False)
        after = ""
        while result.scanned < scan_limit:
            page_limit = min(PAGE_SIZE, scan_limit - result.scanned)
            page = self._run(
                lambda conn, after=after, page_limit=page_limit: conn.execute(
                    SELECT_PAGE_SQL, (after, page_limit)
                ).fetchall()
            )
            result.scanned += len(page)
            for record in page:
                generation_id, updated_at = record[0], record[-1]
                if SAFE_ID.fullmatch(generation_id) is None:
                    raise ValueError("source contains an invalid generation ID")
                result.known_ids.add(generation_id)
                row = dict(zip(FIELDS, record[1:-1], strict=True))
                after = generation_id
                created_at = _timestamp(row["created_at"])
                if not start <= created_at < end:
                    continue
                if len(result.rows) == limit:
                    return result
                row["provider"] = row.get("provider") or ""
                for field in ("cached_input_tokens", "reasoning_tokens"):
                    row[field] = row.get(field) or 0
                for field in FIELDS[:5]:
                    row[field] = int(row[field])
                result.rows[generation_id] = row
                # A late settle can have an old event time. The grace starts
                # at its Postgres write, not just at the request's created_at.
                result.settled_at[generation_id] = max(created_at, _timestamp(updated_at))
            if len(page) < page_limit:
                result.complete = True
                return result
        # One key-only probe distinguishes an exact fit from a truncated scan.
        result.complete = not self._run(
            lambda conn: conn.execute(
                "SELECT id FROM tr_entities WHERE kind = 'generation' AND id > %s "
                "ORDER BY id LIMIT 1",
                (after,),
            ).fetchall()
        )
        return result

    def existing(self, ids: list[str]) -> set[str]:
        """The ids among `ids` that are generation entities now: point reads
        by primary key, for rows the window scan passed before they were
        written (a settle inside the window that landed after the scan)."""
        found: set[str] = set()
        for offset in range(0, len(ids), PAGE_SIZE):
            chunk = ids[offset : offset + PAGE_SIZE]
            rows = self._run(
                lambda conn, chunk=chunk: conn.execute(
                    "SELECT id FROM tr_entities WHERE kind = 'generation' AND id = ANY(%s)",
                    (chunk,),
                ).fetchall()
            )
            found.update(str(row[0]) for row in rows)
        return found


class ClickHouseGenerationSource(OperationalAnalyticsClient):
    def fetch_ids(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        rows: dict[str, list[dict[str, Any]]] = {}
        if any(SAFE_ID.fullmatch(gid) is None for gid in ids):
            raise ValueError("source contains an invalid generation ID")
        for offset in range(0, len(ids), PAGE_SIZE):
            chunk = ids[offset : offset + PAGE_SIZE]
            params = {f"id_{index}": gid for index, gid in enumerate(chunk)}
            placeholders = ", ".join("{" + key + ":String}" for key in params)
            # The inner lookup uses by_generation_id; the outer read uses the
            # complete sort key and keeps duplicates surviving FINAL visible.
            stored = self._query(
                "SELECT generation_id, "  # noqa: S608 - fixed fields, server-bound IDs
                + ", ".join(FIELDS)
                + " FROM activity_generations FINAL "
                "WHERE (tenant_id, created_at, generation_id) IN ("
                "SELECT tenant_id, created_at, generation_id FROM activity_generations "
                "WHERE generation_id IN (" + placeholders + "))" + QUERY_BOUNDS,
                params=params,
                timeout_seconds=40,
            )
            for row in stored:
                gid = str(row["generation_id"])
                if gid not in chunk:
                    raise ValueError("ClickHouse returned an unrequested generation ID")
                rows.setdefault(gid, []).append(row)
        return rows

    def fetch_window(
        self,
        *,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self._query(
            "SELECT DISTINCT generation_id, created_at FROM activity_generations FINAL "  # noqa: S608
            "WHERE created_at >= parseDateTime64BestEffort({start:String}, 6, 'UTC') "
            "AND created_at < parseDateTime64BestEffort({end:String}, 6, 'UTC') "
            "ORDER BY created_at, generation_id LIMIT {limit:UInt32}" + QUERY_BOUNDS,
            params={"start": start.isoformat(), "end": end.isoformat(), "limit": limit},
            timeout_seconds=40,
        )


def verify_delivery(
    source: PostgresGenerationSource,
    clickhouse: ClickHouseGenerationSource,
    *,
    start: dt.datetime,
    end: dt.datetime,
    limit: int,
    scan_limit: int = 50000,
    grace_seconds: int = 300,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if start >= end or limit < 1 or scan_limit < 1 or grace_seconds < 0:
        raise ValueError("invalid window, limit, scan limit or grace")
    cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(seconds=grace_seconds)
    window = source.fetch(start=start, end=end, limit=limit, scan_limit=scan_limit)
    expected = window.rows
    stored = clickhouse.fetch_ids(list(expected))
    absent = set(expected) - set(stored)
    missing = sorted(gid for gid in absent if window.settled_at[gid] <= cutoff)
    pending = sorted(absent - set(missing))
    duplicated = sorted(gid for gid, rows in stored.items() if len(rows) > 1)
    mismatched: list[str] = []
    mismatch_fields: collections.Counter[str] = collections.Counter()
    for gid in sorted(set(expected) & set(stored)):
        if len(stored[gid]) != 1:
            continue
        actual = stored[gid][0]
        differences = []
        for field in FIELDS:
            left, right = expected[gid].get(field), actual.get(field)
            # FORMAT JSON quotes UInt64 values by default; preserve integer
            # precision rather than round costs or token counts through float.
            if field in FIELDS[:5] and isinstance(right, str):
                try:
                    right = int(right)
                except ValueError:
                    pass
            if field == "created_at":
                differs = (
                    right is None or abs((_timestamp(left) - _timestamp(right)).total_seconds()) > 1
                )
            else:
                differs = left != right
            if differs:
                differences.append(field)
        if differences:
            mismatched.append(gid)
            mismatch_fields.update(differences)

    extra: list[str] = []
    settled_after_scan = 0
    complete = window.complete
    if complete:
        candidates = clickhouse.fetch_window(start=start, end=end, limit=limit + 1)
        complete = len(candidates) <= limit
        unknown = sorted(
            {
                str(row["generation_id"])
                for row in candidates[:limit]
                if start <= _timestamp(row["created_at"]) < end
                and str(row["generation_id"]) not in window.known_ids
            }
        )
        if any(SAFE_ID.fullmatch(gid) is None for gid in unknown):
            raise ValueError("ClickHouse contains an invalid generation ID")
        # A record the scan did not see is extra only if Postgres has no
        # such generation now: one settled inside the window after the scan
        # passed its id is ordinary delivery, not a row from nowhere.
        settled_after = source.existing(unknown)
        settled_after_scan = len(settled_after)
        extra = [gid for gid in unknown if gid not in settled_after]
    return {
        "sampled": len(expected),
        "found": len(stored),
        "missing": len(missing),
        "pending": len(pending),
        "extra": len(extra),
        "mismatched": len(mismatched),
        "duplicated": len(duplicated),
        "missing_ids": missing[:20],
        "pending_ids": pending[:20],
        "extra_ids": extra[:20],
        "mismatched_ids": mismatched[:20],
        "duplicated_ids": duplicated[:20],
        "mismatch_fields": dict(sorted(mismatch_fields.items())),
        "settled_after_scan": settled_after_scan,
        "scanned": window.scanned,
        "complete": complete,
        "extra_checked": window.complete,
        "ok": complete and not missing and not extra and not mismatched and not duplicated,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("TR_POSTGRES_DSN", ""))
    parser.add_argument("--iam-auth", default=os.environ.get("TR_POSTGRES_IAM_AUTH", ""))
    parser.add_argument("--iam-region", default=os.environ.get("TR_POSTGRES_IAM_REGION", ""))
    parser.add_argument("--start", type=_timestamp)
    parser.add_argument("--end", type=_timestamp)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--scan-limit", type=int, default=50000)
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--grace-seconds", type=int, default=300)
    parser.add_argument(
        "--history-file",
        type=Path,
        default=Path("/var/lib/tr-clickhouse-ingest/postgres-delivery.jsonl"),
    )
    args = parser.parse_args()
    now = dt.datetime.now(dt.UTC)
    end = args.end or now
    start = args.start or end - dt.timedelta(hours=args.lookback_hours)
    if (
        args.limit < 1
        or args.scan_limit < 1
        or args.lookback_hours < 1
        or args.grace_seconds < 0
        or start >= end
    ):
        parser.error("limits/lookback must be positive, grace nonnegative and start before end")
    if not args.dsn:
        parser.error("TR_POSTGRES_DSN (or --dsn) is required")
    password = os.environ.get(f"{CLICKHOUSE_ENV_PREFIX}_PASSWORD") or os.environ.get("CH_PASSWORD")
    if not password:
        parser.error("CH_PASSWORD or TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD is required")
    source = PostgresGenerationSource(
        dsn=args.dsn,
        iam_auth=args.iam_auth,
        iam_region=args.iam_region,
    )
    try:
        result = verify_delivery(
            source,
            ClickHouseGenerationSource(
                base_url=os.environ.get(f"{CLICKHOUSE_ENV_PREFIX}_URL", "http://127.0.0.1:8123"),
                user=os.environ.get(f"{CLICKHOUSE_ENV_PREFIX}_USER", "tr"),
                password=password,
                database=os.environ.get(f"{CLICKHOUSE_ENV_PREFIX}_DATABASE", "tr"),
            ),
            start=start,
            end=end,
            limit=args.limit,
            scan_limit=args.scan_limit,
            grace_seconds=args.grace_seconds,
            now=now,
        )
        result["checked_at"] = now.isoformat().replace("+00:00", "Z")
        result["window_start"] = start.isoformat().replace("+00:00", "Z")
        result["window_end"] = end.isoformat().replace("+00:00", "Z")
        result["grace_seconds"] = args.grace_seconds
        _write_history(args.history_file, result)
    except Exception as exc:
        # Driver/HTTP exceptions can contain credentials or entity contents.
        print(json.dumps({"ok": False, "error": type(exc).__name__}, sort_keys=True))
        return 1
    finally:
        source.close()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
