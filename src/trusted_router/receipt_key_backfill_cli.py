"""Backfill receipt-key version projection columns in bounded, resumable pages."""

from __future__ import annotations

import argparse
import logging
from typing import Protocol, cast

from trusted_router.config import get_settings
from trusted_router.storage import create_store

log = logging.getLogger(__name__)


class _ReceiptKeyBackfillStore(Protocol):
    def backfill_receipt_key_versions_page(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> list[str]: ...


def run(
    store: _ReceiptKeyBackfillStore,
    *,
    batch_size: int = 100,
    after: str | None = None,
) -> int:
    if not 1 <= batch_size <= 1_000:
        raise ValueError("batch size must be between 1 and 1000")
    updated = 0
    cursor = after
    while True:
        entity_ids = store.backfill_receipt_key_versions_page(
            after=cursor,
            limit=batch_size,
        )
        if not entity_ids:
            log.info(
                "receipt_key.version_backfill_complete updated=%d after=%s",
                updated,
                cursor or "",
            )
            return updated
        updated += len(entity_ids)
        cursor = entity_ids[-1]
        log.info(
            "receipt_key.version_backfill_checkpoint updated=%d after=%s",
            updated,
            cursor,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--after", help="resume after this legacy entity ID")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    run(
        cast(_ReceiptKeyBackfillStore, create_store(get_settings())),
        batch_size=args.batch_size,
        after=args.after,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
