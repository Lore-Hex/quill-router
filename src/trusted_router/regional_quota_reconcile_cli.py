"""One-shot regional quota lease reconciler for Cloud Run Jobs."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

from trusted_router.config import get_settings
from trusted_router.sentry_config import init_sentry
from trusted_router.storage import configure_store, create_store
from trusted_router.synthetic.fleet import record_heartbeat

logger = logging.getLogger(__name__)
_LOCK_TTL_SECONDS = 90


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    started_at = time.monotonic()
    logger.info("regional_quota.reconciler_start")
    settings = get_settings()
    logger.info(
        "regional_quota.reconciler_settings_loaded elapsed_ms=%.1f",
        _elapsed_ms(started_at),
    )
    init_sentry(settings)
    if not settings.regional_quota_reconciler_worker:
        logger.info("regional_quota.reconciler_disabled")
        return 0
    if not settings.regional_quota_leases_enabled:
        logger.error("regional_quota.reconciler_ledger_disabled")
        return 1

    logger.info(
        "regional_quota.reconciler_store_open_start elapsed_ms=%.1f",
        _elapsed_ms(started_at),
    )
    store = create_store(settings)
    configure_store(store)
    logger.info(
        "regional_quota.reconciler_store_open_complete elapsed_ms=%.1f",
        _elapsed_ms(started_at),
    )
    acquire = cast(
        Callable[..., Any] | None,
        getattr(store, "acquire_regional_quota_reconciler_lock", None),
    )
    release = cast(
        Callable[..., bool] | None,
        getattr(store, "release_regional_quota_reconciler_lock", None),
    )
    if acquire is None or release is None:
        logger.error("regional_quota.reconciler_lock_unsupported")
        return 1
    owner = f"rqrec-{uuid.uuid4().hex}"
    logger.info(
        "regional_quota.reconciler_lock_acquire_start elapsed_ms=%.1f",
        _elapsed_ms(started_at),
    )
    lock = acquire(owner=owner, ttl_seconds=_LOCK_TTL_SECONDS)
    if lock is None:
        logger.info("regional_quota.reconciler_lock_busy")
        return 0
    logger.info(
        "regional_quota.reconciler_lock_acquired owner=%s fencing_token=%d ttl_seconds=%d",
        owner,
        int(lock.fencing_token),
        _LOCK_TTL_SECONDS,
    )
    previous_owner = getattr(lock, "previous_owner", None)
    previous_fencing_token = getattr(lock, "previous_fencing_token", None)
    if previous_owner is not None and previous_fencing_token is not None:
        logger.info(
            "regional_quota.reconciler_lock_takeover owner=%s fencing_token=%d "
            "previous_owner=%s previous_fencing_token=%d",
            owner,
            int(lock.fencing_token),
            previous_owner,
            int(previous_fencing_token),
        )

    exit_code = 1
    try:
        exit_code = _run_reconcile(settings, store)
    except Exception:
        logger.exception("regional_quota.reconciler_failed")
        exit_code = 1
    finally:
        try:
            released = release(
                owner=owner,
                fencing_token=int(lock.fencing_token),
            )
        except Exception:
            logger.exception("regional_quota.reconciler_lock_release_failed")
            exit_code = 1
        else:
            if released:
                logger.info(
                    "regional_quota.reconciler_lock_released owner=%s fencing_token=%d",
                    owner,
                    int(lock.fencing_token),
                )
            else:
                logger.error("regional_quota.reconciler_lock_release_lost")
                exit_code = 1
    if exit_code == 0:
        record_heartbeat("job:regional-quota-reconcile", settings=settings)
        logger.info(
            "regional_quota.reconciler_complete elapsed_ms=%.1f",
            _elapsed_ms(started_at),
        )
    return exit_code


def _elapsed_ms(started_at: float) -> float:
    return (time.monotonic() - started_at) * 1_000.0


def _run_reconcile(settings: Any, store: Any) -> int:
    verify = cast(
        Callable[[], tuple[str, ...]] | None,
        getattr(store, "verify_regional_quota_ledger", None),
    )
    if verify is None:
        logger.error("regional_quota.reconciler_health_check_unsupported")
        return 1
    verified_regions = verify()
    if not verified_regions:
        logger.error("regional_quota.reconciler_has_no_regions")
        return 1

    reconcile = cast(
        Callable[..., dict[str, int]] | None,
        getattr(store, "reconcile_regional_quota_leases", None),
    )
    if reconcile is None:
        logger.error("regional_quota.reconciler_store_unsupported")
        return 1

    result: dict[str, Any] = reconcile(limit=settings.regional_quota_reconcile_limit)
    logger.info(
        "regional_quota.reconcile_complete inspected=%d reconciled=%d closed=%d errors=%d",
        int(result.get("inspected", 0)),
        int(result.get("reconciled", 0)),
        int(result.get("closed", 0)),
        int(result.get("errors", 0)),
    )
    if int(result.get("errors", 0)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
