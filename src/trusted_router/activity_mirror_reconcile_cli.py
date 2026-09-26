"""Repair Bigtable activity mirrors from Spanner.

Typed settlements can be repaired by --generation-id. Workspace/day scans
cover only the legacy generation_by_workspace index, bounded by --limit.
Resume truncated scans with --after-id from next_after_id.
Writes are idempotent: existing rows are rewritten in place.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from trusted_router.config import get_settings
from trusted_router.sentry_config import init_sentry
from trusted_router.storage import configure_store, create_store
from trusted_router.storage_gcp_generations import SpannerGenerations

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="activity_mirror_reconcile",
        description="Re-mirror generations into the Bigtable activity index from Spanner.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--workspace-id", help="workspace whose legacy index rows to re-mirror")
    target.add_argument("--generation-id", help="generation to repair, including typed settlements")
    parser.add_argument("--date", default=None, help="UTC day (YYYY-MM-DD); default: all days")
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="maximum generations to re-mirror (default 1000)",
    )
    parser.add_argument("--after-id", help="resume after the previous next_after_id index key")
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.generation_id is not None and (args.date is not None or args.after_id is not None):
        parser.error("--date and --after-id require --workspace-id")
    if args.date is not None:
        try:
            if dt.date.fromisoformat(args.date).isoformat() != args.date:
                raise ValueError("noncanonical date")
        except ValueError:
            parser.error("--date must be a calendar date in YYYY-MM-DD format")
    if args.after_id is not None:
        prefix = f"{args.workspace_id}#{args.date}#" if args.date else f"{args.workspace_id}#"
        if not args.after_id.startswith(prefix):
            parser.error("--after-id must belong to the requested workspace/day")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    init_sentry(settings)
    store = create_store(settings)
    configure_store(store)
    generations = getattr(store, "generation_store", None)
    if not isinstance(generations, SpannerGenerations):
        logger.error("activity_mirror.reconcile_unsupported backend=%s", type(store).__name__)
        return 1
    result = generations.reconcile_activity(
        args.workspace_id, date=args.date, limit=args.limit,
        generation_id=args.generation_id, detailed=True, after_id=args.after_id,
    )
    report = {
        "repaired": result.mirror_repaired,
        "durable_repaired": result.durable_repaired,
        "mirror_failed": result.mirror_failed,
        "durable_failed": result.durable_failed,
        "missing": result.missing,
        "mirror_skipped": result.mirror_skipped,
        "scanned": result.scanned,
        "truncated": result.truncated,
        "next_after_id": result.next_after_id,
    }
    logger.info("activity_mirror.reconcile_complete %s", json.dumps(report))
    print(json.dumps(report))
    return int(bool(result.mirror_failed or result.durable_failed or result.missing))


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
