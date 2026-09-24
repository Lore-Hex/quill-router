"""Re-mirror a workspace's generations into the Bigtable activity index.

The settlement path mirrors each generation into Bigtable with one bounded
retry. When that still fails, the router logs
``bigtable.activity_index_write_failed`` with the workspace and day; Spanner
and the ClickHouse activity outbox already hold the generation, so nothing is
lost, but the Bigtable fallback/shadow index is missing rows until this
command re-mirrors them from Spanner::

    python -m trusted_router.activity_mirror_reconcile_cli \\
        --workspace-id 358d80a4-... --date 2026-09-24

Without ``--date`` every generation of the workspace (up to ``--limit``) is
re-mirrored. Writes are idempotent: an existing row is rewritten in place.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from typing import cast

from trusted_router.config import get_settings
from trusted_router.sentry_config import init_sentry
from trusted_router.storage import configure_store, create_store

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="activity_mirror_reconcile",
        description="Re-mirror generations into the Bigtable activity index from Spanner.",
    )
    parser.add_argument("--workspace-id", required=True, help="workspace whose rows to re-mirror")
    parser.add_argument("--date", default=None, help="UTC day (YYYY-MM-DD); default: all days")
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="maximum generations to re-mirror (default 1000)",
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.date is not None and (
        len(args.date) != 10 or args.date[4] != "-" or args.date[7] != "-"
    ):
        parser.error("--date must look like YYYY-MM-DD")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    init_sentry(settings)
    store = create_store(settings)
    configure_store(store)
    reconcile = cast(
        Callable[..., int] | None,
        getattr(store, "reconcile_generation_activity", None),
    )
    if reconcile is None:
        logger.error("activity_mirror.reconcile_unsupported backend=%s", type(store).__name__)
        return 1
    repaired = reconcile(args.workspace_id, date=args.date, limit=args.limit)
    logger.info(
        "activity_mirror.reconcile_complete workspace_id=%s date=%s limit=%d repaired=%d",
        args.workspace_id,
        args.date or "all",
        args.limit,
        repaired,
    )
    print(f"repaired={repaired}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
