#!/usr/bin/env python3
"""Drain the alerted identity-demotion invariant fallback in bounded chunks."""

from __future__ import annotations

import argparse
import logging
from typing import Protocol, cast

from trusted_router.config import get_settings
from trusted_router.storage import create_store

log = logging.getLogger(__name__)


class _RemainderStore(Protocol):
    def process_trust_demotion_remainders(self, *, limit: int = 25) -> int: ...


def run(store: _RemainderStore, *, limit: int) -> int:
    completed = store.process_trust_demotion_remainders(limit=limit)
    if completed:
        log.error("trust.identity_demotion_remainder_drained count=%d", completed)
    return completed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    logging.basicConfig(level=logging.INFO)
    run(cast(_RemainderStore, create_store(get_settings())), limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
