"""Rebuild both directions of the owner inventory and publish its arm marker.

Runs as ``python -m trusted_router.owner_inventory_cli`` inside the image. The
marker identity (``owner_inventory``/``local``/``tr_entities.workspace``) and the
default source version come from ``trust_reconciliation``, the same constants
PR 2's ``MarkerRequirement`` reads. ``--environment`` defaults to production
because every Cloud Run job carries ``TR_ENVIRONMENT=worker``.
"""

from __future__ import annotations

import argparse
import logging
from typing import Protocol, cast

from trusted_router.config import get_settings
from trusted_router.storage import create_store
from trusted_router.trust_reconciliation import OWNER_INVENTORY_SOURCE_VERSION

log = logging.getLogger(__name__)


class _OwnerInventoryStore(Protocol):
    def backfill_owner_inventory(self, *, source_version: str, environment: str) -> int: ...


def run(
    store: _OwnerInventoryStore, *, source_version: str, environment: str
) -> int:
    changed = store.backfill_owner_inventory(
        source_version=source_version, environment=environment
    )
    log.info(
        "trust.owner_inventory_backfill_complete changed=%d source_version=%s environment=%s",
        changed,
        source_version,
        environment,
    )
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-version", default=OWNER_INVENTORY_SOURCE_VERSION)
    parser.add_argument("--environment", default="production")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    run(
        cast(_OwnerInventoryStore, create_store(settings)),
        source_version=args.source_version,
        environment=args.environment,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
