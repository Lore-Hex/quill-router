"""Recompute converged workspace trust tiers on every active credit shard.

Runs as ``python -m trusted_router.trust_tier_cli --environment production``
inside the image. ``--environment`` is explicit because every Cloud Run job
carries ``TR_ENVIRONMENT=worker``; replicating ``trust_reconciled_through`` with
that value finds no marker and writes NULL to every shard.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from trusted_router.config import get_settings
from trusted_router.sentry_config import init_sentry
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


@dataclass(frozen=True, slots=True)
class TrustTierJobResult:
    attempted: int
    failed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> int:
        return self.attempted - len(self.failed)


def run(
    store: _TrustTierStore,
    settings: Any,
    *,
    environment: str = "production",
    now: datetime | None = None,
) -> TrustTierJobResult:
    """Visit every workspace; one raising workspace never skips the rest.

    Each workspace's replication and recompute run under their own try/except.
    A failure is logged with its workspace id and counted; the pass continues so
    a single bad row cannot leave every later workspace at a stale tier and a
    stale (or NULL) ``trust_reconciled_through``. The caller exits non-zero when
    ``failed`` is non-empty.
    """

    computed_at = now or datetime.now(UTC)
    if hasattr(store, "list_stale_trust_inbox"):
        alert_stale_trust_inbox(store, now=computed_at)
    workspace_ids = store.list_trust_tier_workspace_ids()
    failed: list[str] = []
    for workspace_id in workspace_ids:
        try:
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
        except Exception:
            failed.append(workspace_id)
            log.exception("trust.tier_job_workspace_failed workspace_id=%s", workspace_id)
    return TrustTierJobResult(attempted=len(workspace_ids), failed=tuple(failed))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="production")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    # ``alert_stale_trust_inbox`` pages through ops_alert, which reaches Sentry
    # only after init_sentry (the pattern every live job follows).
    init_sentry(settings)
    store = cast(_TrustTierStore, create_store(settings))
    result = run(store, settings, environment=args.environment)
    log.info(
        "trust.tier_job_complete workspaces=%d failed=%d environment=%s",
        result.attempted,
        len(result.failed),
        args.environment,
    )
    if result.failed:
        log.error("trust.tier_job_failures workspace_ids=%s", ",".join(result.failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
