#!/usr/bin/env python3
"""Recompute converged workspace trust tiers on every active credit shard."""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from trusted_router.config import get_settings
from trusted_router.services.trust_recovery import alert_stale_trust_inbox
from trusted_router.storage import create_store
from trusted_router.storage_trust_reconciliation import replicate_tier_job_watermark

log = logging.getLogger(__name__)


class _TrustTierStore(Protocol):
    def list_trust_tier_workspace_ids(self) -> tuple[str, ...]: ...

    def recompute_workspace_trust_tier(
        self,
        workspace_id: str,
        *,
        qualifying_providers: frozenset[str],
        tier3_min_days: int,
        tier3_min_paid_microdollars: int,
        now: datetime,
    ) -> int: ...


def run(store: _TrustTierStore, settings: Any, *, environment: str = "production", now: datetime | None = None) -> int:
    computed_at = now or datetime.now(UTC)
    if hasattr(store, "list_stale_trust_inbox"):
        alert_stale_trust_inbox(store, now=computed_at)
    workspace_ids = store.list_trust_tier_workspace_ids()
    for workspace_id in workspace_ids:
        replicated, reconciled_through = replicate_tier_job_watermark(
            store,
            workspace_id,
            settings.trust_qualifying_provider_set,
            environment=environment,
        )
        if replicated:
            log.info(
                "trust.reconciled_through workspace_id=%s value=%s",
                workspace_id,
                reconciled_through,
            )
        tier = store.recompute_workspace_trust_tier(
            workspace_id,
            qualifying_providers=settings.trust_qualifying_provider_set,
            tier3_min_days=settings.trust_tier3_min_days,
            tier3_min_paid_microdollars=settings.trust_tier3_min_paid_microdollars,
            now=computed_at,
        )
        log.info("trust.tier_computed workspace_id=%s tier=%d", workspace_id, tier)
    return len(workspace_ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="production")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    store = cast(_TrustTierStore, create_store(settings))
    count = run(store, settings, environment=args.environment)
    log.info("trust.tier_job_complete workspaces=%d", count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
