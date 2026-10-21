"""Re-enqueue Postgres generations that never reached ClickHouse.

The Postgres counterpart of ``trusted_router.activity_delivery_repair_cli``
(Spanner only). It finds generation entities in a ``created_at`` window, or the
ones named by ``--generation-id``, that ClickHouse ``activity_generations`` does
not hold, and inserts each one's activity row into
``tr_operational_analytics_outbox``. The drain then delivers it like any other.

Discovery reuses ``clickhouse.verify_postgres_delivery``'s readers, so this
tool and the verifier agree on what "missing" means, grace included. The write
is the settle path's own ``PostgresOperationalAnalyticsOutbox.enqueue_activity_tx``
applied to the stored ``Generation``: the same payload, shard and stable event
id, under the same ``ON CONFLICT DO NOTHING``. A replay is a no-op while the
row is queued, and a redelivery after the drain collapses in ClickHouse's
``ReplacingMergeTree`` instead of duplicating.

Dry run is the default and writes nothing. ``--apply`` writes outbox rows and
nothing else: no deletes, and no settlement, reservation or balance row.
``--limit`` bounds one run; resume with ``--after-id`` from ``next_after_id``.
No row values, payloads or driver error text are printed.

It reads whole generation entities and inserts outbox rows, which no scoped
role may do, so an operator runs it as the DSQL admin. The procedure and the
confirmation are in docs/clickhouse-reliability.md, "Repairing missing
Postgres activity".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, fields
from typing import Any

from clickhouse.ingest_operational_outbox_postgres import CLICKHOUSE_ENV_PREFIX
from clickhouse.operational_fingerprint import SAFE_ID
from clickhouse.verify_postgres_delivery import (
    ClickHouseGenerationSource,
    PostgresGenerationSource,
    _timestamp,
    clickhouse_from_env,
)
from trusted_router.storage_models import Generation
from trusted_router.storage_postgres_operational_analytics_outbox import (
    PostgresOperationalAnalyticsOutbox,
)

REPORT_IDS = 20
#: Generations per apply transaction. DSQL caps one transaction at 3,000
#: modified rows; one outbox row per generation keeps this far below that.
APPLY_CHUNK = 100
# The full stored entity, which is the Generation settle wrote. A point read
# through the (kind, id) primary key.
SELECT_GENERATION_SQL = "SELECT body FROM tr_entities WHERE kind = 'generation' AND id = %s"
_GENERATION_FIELDS = frozenset(field.name for field in fields(Generation))


def decode_generation(raw: Any) -> Generation:
    """The stored body as the ``Generation`` settle wrote.

    Decodes exactly as ``PostgresStore._read_entity_tx`` does. That module is
    not imported because it needs psycopg_pool, which the ClickHouse node's
    virtualenv, where this tool's siblings run, does not have. A field added to
    ``Generation`` after the entity was written takes its dataclass default,
    as it does on every other read of that entity.
    """
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return Generation(**{key: value for key, value in data.items() if key in _GENERATION_FIELDS})


class PostgresActivityRepairSource(PostgresGenerationSource):
    def enqueue(self, ids: list[str]) -> list[str]:
        """Enqueue each id's activity row; return the ids with no entity."""
        # Only the *_tx method is used, on this source's own transaction, so
        # the outbox's own runner is never called.
        outbox = PostgresOperationalAnalyticsOutbox(self._run)
        absent: list[str] = []
        for offset in range(0, len(ids), APPLY_CHUNK):
            chunk = ids[offset : offset + APPLY_CHUNK]

            def operation(conn: Any, chunk: list[str] = chunk) -> list[str]:
                # Local per attempt: a retried OCC abort starts clean.
                not_found = []
                for generation_id in chunk:
                    row = conn.execute(SELECT_GENERATION_SQL, (generation_id,)).fetchone()
                    if row is None:
                        not_found.append(generation_id)
                        continue
                    generation = decode_generation(row[0])
                    if generation.id != generation_id:
                        raise ValueError("stored generation id differs from its key")
                    outbox.enqueue_activity_tx(conn, generation)
                return not_found

            absent.extend(self._run(operation))
        return absent


@dataclass
class Candidates:
    missing: list[str]
    pending: int
    not_found: list[str]
    scanned: int
    sampled: int
    complete: bool


def find_missing(
    source: PostgresGenerationSource,
    clickhouse: ClickHouseGenerationSource,
    *,
    start: dt.datetime | None,
    end: dt.datetime | None,
    generation_ids: list[str],
    scan_limit: int,
    grace_seconds: int,
    now: dt.datetime,
) -> Candidates:
    """Generation ids that Postgres holds and ClickHouse does not, sorted."""
    if generation_ids:
        requested = sorted(set(generation_ids))
        present = source.existing(requested)
        stored = clickhouse.fetch_ids(sorted(present))
        return Candidates(
            missing=sorted(present - set(stored)),
            pending=0,
            not_found=[gid for gid in requested if gid not in present],
            scanned=len(requested),
            sampled=len(present),
            complete=True,
        )
    if start is None or end is None:
        raise ValueError("a window needs both start and end")
    window = source.fetch(start=start, end=end, limit=scan_limit, scan_limit=scan_limit)
    stored = clickhouse.fetch_ids(list(window.rows))
    cutoff = now - dt.timedelta(seconds=grace_seconds)
    absent = set(window.rows) - set(stored)
    missing = sorted(gid for gid in absent if window.settled_at[gid] <= cutoff)
    return Candidates(
        missing=missing,
        pending=len(absent) - len(missing),
        not_found=[],
        scanned=window.scanned,
        sampled=len(window.rows),
        complete=window.complete,
    )


def repair(
    source: PostgresActivityRepairSource,
    clickhouse: ClickHouseGenerationSource,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    generation_ids: list[str] | None = None,
    limit: int,
    after_id: str | None = None,
    apply: bool = False,
    scan_limit: int = 1_000_000,
    grace_seconds: int = 300,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    candidates = find_missing(
        source,
        clickhouse,
        start=start,
        end=end,
        generation_ids=generation_ids or [],
        scan_limit=scan_limit,
        grace_seconds=grace_seconds,
        now=now or dt.datetime.now(dt.UTC),
    )
    remaining = [gid for gid in candidates.missing if after_id is None or gid > after_id]
    selected = remaining[:limit]
    next_after_id = selected[-1] if len(remaining) > limit else None
    vanished = source.enqueue(selected) if apply else []
    not_found = candidates.not_found + vanished
    return {
        "mode": "apply" if apply else "dry_run",
        "scanned": candidates.scanned,
        "sampled": candidates.sampled,
        "complete": candidates.complete,
        "missing": len(candidates.missing),
        "pending": candidates.pending,
        "after_id": after_id,
        "selected": len(selected),
        "selected_ids": selected[:REPORT_IDS],
        "enqueued": len(selected) - len(vanished) if apply else 0,
        "not_found": len(not_found),
        "not_found_ids": not_found[:REPORT_IDS],
        "next_after_id": next_after_id,
        "resume": None if next_after_id is None else f"rerun with --after-id {next_after_id}",
        "ok": candidates.complete and not not_found,
    }


def _generation_id(value: str) -> str:
    if SAFE_ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("invalid generation id")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="repair_postgres_activity",
        description="Re-enqueue Postgres generations missing from ClickHouse "
        "into the activity outbox. Dry run unless --apply.",
    )
    parser.add_argument("--dsn", default=os.environ.get("TR_POSTGRES_DSN", ""))
    parser.add_argument("--iam-auth", default=os.environ.get("TR_POSTGRES_IAM_AUTH", ""))
    parser.add_argument("--iam-region", default=os.environ.get("TR_POSTGRES_IAM_REGION", ""))
    parser.add_argument("--start", type=_timestamp, help="window start, inclusive (created_at)")
    parser.add_argument("--end", type=_timestamp, help="window end, exclusive (created_at)")
    parser.add_argument(
        "--generation-id",
        action="append",
        type=_generation_id,
        default=[],
        help="repair this generation instead of a window; repeatable",
    )
    parser.add_argument("--limit", type=int, default=500, help="most generations per run")
    parser.add_argument("--after-id", type=_generation_id, help="resume past next_after_id")
    parser.add_argument("--scan-limit", type=int, default=1_000_000)
    parser.add_argument("--grace-seconds", type=int, default=300)
    parser.add_argument("--apply", action="store_true", help="write outbox rows (default: dry run)")
    args = parser.parse_args(argv)
    window = args.start is not None or args.end is not None
    if args.generation_id and window:
        parser.error("use either --generation-id or --start/--end, not both")
    if not args.generation_id and (args.start is None or args.end is None):
        parser.error("--start and --end are required unless --generation-id is given")
    if window and args.start >= args.end:
        parser.error("--start must be before --end")
    if args.limit < 1 or args.scan_limit < 1 or args.grace_seconds < 0:
        parser.error("limits must be positive and grace nonnegative")
    if not args.dsn:
        parser.error("TR_POSTGRES_DSN (or --dsn) is required")
    password = os.environ.get(f"{CLICKHOUSE_ENV_PREFIX}_PASSWORD") or os.environ.get("CH_PASSWORD")
    if not password:
        parser.error("CH_PASSWORD or TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD is required")
    source = PostgresActivityRepairSource(
        dsn=args.dsn,
        iam_auth=args.iam_auth,
        iam_region=args.iam_region,
    )
    try:
        result = repair(
            source,
            clickhouse_from_env(password),
            start=args.start,
            end=args.end,
            generation_ids=args.generation_id,
            limit=args.limit,
            after_id=args.after_id,
            apply=args.apply,
            scan_limit=args.scan_limit,
            grace_seconds=args.grace_seconds,
        )
    except Exception as exc:
        # Driver/HTTP exceptions can contain credentials or entity contents.
        print(json.dumps({"ok": False, "error": type(exc).__name__}, sort_keys=True))
        return 1
    finally:
        source.close()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
