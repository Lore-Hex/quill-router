#!/usr/bin/env python3
"""Rebuild both directions of the owner inventory and publish its arm marker."""

from __future__ import annotations

import argparse
import logging
from typing import Protocol, cast

from trusted_router.config import get_settings
from trusted_router.storage import create_store

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--environment")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    run(
        cast(_OwnerInventoryStore, create_store(settings)),
        source_version=args.source_version,
        environment=args.environment or settings.environment,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
