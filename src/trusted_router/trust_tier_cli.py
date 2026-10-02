"""Recompute converged workspace trust tiers on every active credit shard.

Runs as ``python -m trusted_router.trust_tier_cli --environment production``
inside the image. ``--environment`` is explicit because every Cloud Run job
carries ``TR_ENVIRONMENT=worker``; replicating ``trust_reconciled_through`` with
that value finds no marker and writes NULL to every shard.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from trusted_router.config import get_settings
from trusted_router.sentry_config import init_sentry
from trusted_router.services.trust_recovery import alert_stale_trust_inbox
from trusted_router.storage import create_store
from trusted_router.storage_trust_reconciliation import (
    replicate_tier_job_watermark,
    tier_job_replicates_watermark,
)
from trusted_router.trust_tier_bulk import (
    TrustTierSelection,
    load_trust_tier_bulk,
    select_trust_tier_candidates,
)

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
        observe: Callable[[str, str], None] | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class TrustTierJobResult:
    attempted: int
    failed: tuple[str, ...] = field(default_factory=tuple)
    owner_budget_failed: bool = False

    @property
    def succeeded(self) -> int:
        return self.attempted - len(self.failed)


# What the per-workspace pass reports, and the digest it is compared with.
_OBSERVED = {"tier_written": "tier", "watermark_changed": "watermark"}


class _Shadow:
    """The bulk selection, checked against what the per-workspace pass did.

    A workspace the pass changed, outside the selection, is a race when its
    inputs differ from what the snapshot saw (or it was not in the snapshot),
    and a defect otherwise. A failure outside the selection is logged on its
    own: a transient error is not a selection defect.
    """

    def __init__(self, selection: TrustTierSelection) -> None:
        self.selection = selection
        self._lock = threading.Lock()
        self._acted: dict[str, dict[str, str]] = {}

    def observer(self, workspace_id: str) -> Callable[[str, str], None]:
        def observe(kind: str, digest: str) -> None:
            with self._lock:
                self._acted.setdefault(workspace_id, {})[kind] = digest

        return observe

    def report(self, failed: Iterable[str]) -> int:
        failed_set = set(failed)
        selected = self.selection.candidates
        defects = races = failed_outside = 0
        for workspace_id in sorted(set(self._acted) | failed_set):
            if workspace_id in selected:
                continue
            acted = self._acted.get(workspace_id, {})
            if not acted:
                failed_outside += 1
                log.warning("trust.tier_shadow_failed_outside workspace_id=%s", workspace_id)
                continue
            seen = self.selection.digests.get(workspace_id)
            raced = seen is None or any(
                seen.get(_OBSERVED[kind]) != digest for kind, digest in acted.items()
            )
            if raced:
                races += 1
                log.info(
                    "trust.tier_shadow_race workspace_id=%s acted=%s",
                    workspace_id, ",".join(sorted(acted)),
                )
            else:
                defects += 1
                log.error(
                    "trust.tier_shadow_defect workspace_id=%s acted=%s",
                    workspace_id, ",".join(sorted(acted)),
                )
        unacted = len(set(selected) - set(self._acted) - failed_set)
        log.info(
            "trust.tier_shadow_complete candidates=%d acted=%d defects=%d races=%d "
            "failed_outside=%d unacted_candidates=%d",
            len(selected), len(self._acted), defects, races, failed_outside, unacted,
        )
        return defects


def _select(store: Any, settings: Any, *, environment: str, now: datetime) -> _Shadow | None:
    """The bulk selection for the shadow, or None when it is off or unavailable."""

    if not getattr(settings, "trust_tier_shadow_enabled", False):
        return None
    target = getattr(store, "_backend", store)
    if not all(hasattr(target, name) for name in ("_database", "_param_types", "_read_entity_tx")):
        return None
    started = time.monotonic()
    try:
        bulk = load_trust_tier_bulk(target._database, target._param_types, environment=environment)
        selection = select_trust_tier_candidates(
            bulk,
            param_types=target._param_types,
            read_entity_tx=target._read_entity_tx,
            qualifying_providers=settings.trust_qualifying_provider_set,
            tier3_min_days=settings.trust_tier3_min_days,
            tier3_min_paid_microdollars=settings.trust_tier3_min_paid_microdollars,
            now=now,
            watermark_replicated=tier_job_replicates_watermark(store),
        )
    except Exception:
        # The shadow only observes: its failure never changes the pass.
        log.exception("trust.tier_shadow_unavailable")
        return None
    log.info(
        "trust.tier_shadow_selected workspaces=%d candidates=%d elapsed_seconds=%.3f",
        len(selection.workspaces), len(selection.candidates), time.monotonic() - started,
    )
    return _Shadow(selection)


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

    started = time.monotonic()
    computed_at = now or datetime.now(UTC)
    owner_budget_failed = False
    # Global admission evidence must not wait behind the fleet-sized workspace
    # sweep: a platform timeout would otherwise starve every owner's leases.
    # Only typed Spanner consumes this proof; legacy tier work stays unchanged.
    if hasattr(store, "_owner_shard_counts_tx"):
        from trusted_router.trust_owner_budget import recompute_owner_budget

        try:
            # Let the scan stamp its own start, never the end of the tier pass.
            verdict = recompute_owner_budget(store, environment=environment, now=now)
            owner_budget_failed = not verdict["scan_complete"] or bool(verdict["violating_owners"])
        except Exception:
            owner_budget_failed = True
            log.exception("trust.owner_budget_persist_failed")
        log.info(
            "trust.owner_budget_phase_complete failed=%s elapsed_seconds=%.3f",
            owner_budget_failed, time.monotonic() - started,
        )
    if hasattr(store, "list_stale_trust_inbox"):
        from trusted_router.services.paypal_inbox import reconcile_uncredited_paypal_inbox

        try:
            reconcile_uncredited_paypal_inbox(store, settings, now=computed_at)
        except Exception:
            log.exception("trust.inbox_reconciliation_failed")
        alert_stale_trust_inbox(store, now=computed_at)
    shadow = _select(store, settings, environment=environment, now=computed_at)
    workspace_ids = store.list_trust_tier_workspace_ids()
    concurrency = max(1, int(getattr(settings, "trust_tier_job_concurrency", 1)))
    failed: list[str] = []
    log.info(
        "trust.tier_job_started workspaces=%d environment=%s concurrency=%d",
        len(workspace_ids), environment, concurrency,
    )

    # Set when the pass is stopping (a platform stop in any worker). A running
    # worker cannot be interrupted, so it checks this between its database
    # phases and writes nothing after it is set; each phase is a bounded
    # Spanner call, so the threads Python joins at exit finish promptly.
    stop = threading.Event()

    def recompute(workspace_id: str) -> None:
        if stop.is_set():
            return
        try:
            observe = shadow.observer(workspace_id) if shadow is not None else None
            replicated, reconciled_through = replicate_tier_job_watermark(
                store,
                workspace_id,
                settings.trust_qualifying_provider_set,
                environment=environment,
                observe=observe,
            )
            if replicated:
                log.info(
                    "trust.reconciled_through workspace_id=%s value=%s",
                    workspace_id,
                    reconciled_through,
                )
            if stop.is_set():
                return
            policy: dict[str, Any] = {
                "qualifying_providers": settings.trust_qualifying_provider_set,
                "tier3_min_days": settings.trust_tier3_min_days,
                "tier3_min_paid_microdollars": settings.trust_tier3_min_paid_microdollars,
                "now": computed_at,
            }
            if observe is not None:
                policy["observe"] = observe
            tier = store.recompute_workspace_trust_tier(workspace_id, **policy)
            log.info("trust.tier_computed workspace_id=%s tier=%d", workspace_id, tier)
        except Exception:
            with failed_lock:
                failed.append(workspace_id)
            log.exception("trust.tier_job_workspace_failed workspace_id=%s", workspace_id)

    completed = 0

    def progress() -> None:
        if completed % 100 == 0 or completed == len(workspace_ids):
            log.info(
                "trust.tier_job_progress completed=%d total=%d failed=%d elapsed_seconds=%.3f",
                completed, len(workspace_ids), len(failed), time.monotonic() - started,
            )

    failed_lock = threading.Lock()
    if concurrency == 1:
        for workspace_id in workspace_ids:
            recompute(workspace_id)
            completed += 1
            progress()
    else:
        # Workspaces are independent: each recompute runs its own reads and
        # transaction. A worker's ordinary failure is recorded by recompute();
        # anything else (a platform stop) stops the pass and propagates.
        executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="trust-tier")
        pending: set[Future[None]] = set()

        def collect(done: set[Future[None]]) -> None:
            nonlocal completed
            for future in done:
                future.result()  # re-raises a worker's BaseException
                completed += 1
                progress()

        try:
            for workspace_id in workspace_ids:
                pending.add(executor.submit(recompute, workspace_id))
                if len(pending) >= concurrency:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
            # Drain in completion order, so a stop raised by any worker is
            # seen at once, not after an unrelated slow one finishes.
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)
        except BaseException:
            stop.set()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        executor.shutdown(wait=True)
    if shadow is not None:
        try:
            shadow.report(failed)
        except Exception:
            log.exception("trust.tier_shadow_report_failed")
    return TrustTierJobResult(
        attempted=len(workspace_ids), failed=tuple(failed), owner_budget_failed=owner_budget_failed
    )


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
    if result.failed or result.owner_budget_failed:
        log.error("trust.tier_job_failures workspace_ids=%s", ",".join(result.failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
