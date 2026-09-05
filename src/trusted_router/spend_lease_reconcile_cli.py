"""One-shot spend-lease reconciler and dead-row operator command."""

from __future__ import annotations

import argparse
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any, cast

from trusted_router.config import get_settings
from trusted_router.sentry_config import init_sentry
from trusted_router.spend_lease_ledger import SpendLeaseLedgerUnprovisioned
from trusted_router.storage import configure_store, create_store
from trusted_router.synthetic.fleet import record_heartbeat

logger = logging.getLogger(__name__)
_LOCK_TTL_SECONDS = 55


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("reconcile", help="run one bounded reconciliation pass")
    requeue = subcommands.add_parser("requeue-dead", help="requeue dead open rows")
    requeue.add_argument("lease_ids", nargs="*", help="specific lease IDs; default is all")
    requeue.add_argument("--limit", type=int, default=1000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = _parser().parse_args(argv)
    command = args.command or "reconcile"
    started_at = time.monotonic()
    settings = get_settings()
    init_sentry(settings)
    store = create_store(settings)
    configure_store(store)

    verify = cast(
        Callable[[], tuple[str, ...]] | None,
        getattr(store, "verify_spend_lease_ledger", None),
    )
    if verify is None:
        logger.error("spend_lease.reconciler_health_check_unsupported")
        return 1

    if command == "requeue-dead":
        try:
            regions = verify()
        except SpendLeaseLedgerUnprovisioned as exc:
            logger.error(
                "spend_lease.requeue_ledger_unprovisioned "
                "table=%s profile=%s region=%s nothing_to_requeue=true",
                exc.table_id,
                exc.profile,
                exc.region,
            )
            return 1
        except Exception:
            logger.exception("spend_lease.reconciler_health_check_failed")
            return 1
        if not regions:
            logger.error("spend_lease.reconciler_has_no_regions")
            return 1
        requeue = cast(
            Callable[..., int] | None,
            getattr(store, "requeue_dead_spend_leases", None),
        )
        if requeue is None:
            logger.error("spend_lease.reconciler_requeue_unsupported")
            return 1
        count = requeue(lease_ids=tuple(args.lease_ids), limit=int(args.limit))
        logger.info("spend_lease.requeue_complete count=%d", count)
        return 0

    if not settings.spend_lease_reconciler_worker:
        logger.info("spend_lease.reconciler_disabled")
        return 0
    acquire = cast(
        Callable[..., Any] | None,
        getattr(store, "acquire_spend_lease_reconciler_lock", None),
    )
    release = cast(
        Callable[..., bool] | None,
        getattr(store, "release_spend_lease_reconciler_lock", None),
    )
    reconcile = cast(
        Callable[..., dict[str, int | float]] | None,
        getattr(store, "reconcile_spend_leases", None),
    )
    if acquire is None or release is None or reconcile is None:
        logger.error("spend_lease.reconciler_store_unsupported")
        return 1

    owner = f"slrec-{uuid.uuid4().hex}"
    lock = acquire(owner=owner, ttl_seconds=_LOCK_TTL_SECONDS)
    if lock is None:
        logger.info("spend_lease.reconciler_lock_busy")
        return 0
    logger.info(
        "spend_lease.reconciler_lock_acquired owner=%s fencing_token=%d",
        owner,
        int(lock.fencing_token),
    )
    exit_code = 1
    try:
        try:
            regions = verify()
        except SpendLeaseLedgerUnprovisioned as exc:
            if settings.spend_lease_binding_enabled:
                logger.error(
                    "spend_lease.reconcile.ledger_unprovisioned "
                    "table=%s profile=%s region=%s",
                    exc.table_id,
                    exc.profile,
                    exc.region,
                )
            else:
                logger.info(
                    "spend_lease.reconciler_ledger_unprovisioned "
                    "table=%s profile=%s region=%s",
                    exc.table_id,
                    exc.profile,
                    exc.region,
                )
                exit_code = 0
        except Exception:
            logger.exception("spend_lease.reconciler_health_check_failed")
        else:
            if not regions:
                logger.error("spend_lease.reconciler_has_no_regions")
            else:
                result = reconcile(
                    limit=settings.spend_lease_reconcile_limit,
                    max_attempts=settings.spend_lease_reconcile_max_attempts,
                )
                logger.info(
                    "spend_lease.reconcile_complete candidates=%d open=%d recovered=%d "
                    "bound=%d closed=%d deferred=%d errors=%d dead=%d",
                    *(int(result.get(key, 0)) for key in (
                        "candidates", "open", "recovered", "bound", "closed",
                        "deferred", "errors", "dead",
                    )),
                )
                exit_code = int(bool(result.get("errors", 0) or result.get("dead", 0)))
    except Exception:
        logger.exception("spend_lease.reconciler_failed")
    finally:
        try:
            released = release(owner=owner, fencing_token=int(lock.fencing_token))
        except Exception:
            logger.exception("spend_lease.reconciler_lock_release_failed")
            exit_code = 1
        else:
            if not released:
                logger.error("spend_lease.reconciler_lock_release_lost")
                exit_code = 1
    if exit_code == 0:
        record_heartbeat("job:spend-lease-reconcile", settings=settings)
        logger.info(
            "spend_lease.reconciler_complete elapsed_ms=%.1f",
            (time.monotonic() - started_at) * 1000.0,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
