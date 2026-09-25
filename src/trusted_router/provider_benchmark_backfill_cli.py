"""Set indexed_at on legacy provider_benchmark rows in bounded, resumable pages.

PostgresStore (AWS DSQL, Azure Postgres) bounds the route-health benchmark read
by indexed_at. Rows written before record_provider_benchmark set that column
keep it NULL and are outside the bounded read until this command fills them.
Run it once per Postgres deployment after a release that contains the bounded
read; it is safe to rerun and to resume with --after.
"""

from __future__ import annotations

import argparse
import logging
from typing import Protocol, cast

from trusted_router.config import get_settings
from trusted_router.storage import create_store

log = logging.getLogger(__name__)


class _BenchmarkBackfillStore(Protocol):
    def backfill_provider_benchmark_indexed_at_page(
        self,
        *,
        after: str | None = None,
        limit: int = 500,
    ) -> list[str]: ...


def run(
    store: _BenchmarkBackfillStore,
    *,
    batch_size: int = 500,
    after: str | None = None,
) -> int:
    if not 1 <= batch_size <= 1_000:
        raise ValueError("batch size must be between 1 and 1000")
    examined = 0
    cursor = after
    while True:
        entity_ids = store.backfill_provider_benchmark_indexed_at_page(
            after=cursor,
            limit=batch_size,
        )
        if not entity_ids:
            log.info(
                "provider_benchmark.indexed_at_backfill_complete examined=%d after=%s",
                examined,
                cursor or "",
            )
            return examined
        examined += len(entity_ids)
        cursor = entity_ids[-1]
        log.info(
            "provider_benchmark.indexed_at_backfill_checkpoint examined=%d after=%s",
            examined,
            cursor,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--after", help="resume after this provider_benchmark entity ID")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    run(
        cast(_BenchmarkBackfillStore, create_store(get_settings())),
        batch_size=args.batch_size,
        after=args.after,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
