"""One-shot regional quota lease reconciler for Cloud Run Jobs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from trusted_router.config import get_settings
from trusted_router.sentry_config import init_sentry
from trusted_router.storage import configure_store, create_store
from trusted_router.synthetic.fleet import record_heartbeat

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    init_sentry(settings)
    if not settings.regional_quota_reconciler_worker:
        logger.info("regional_quota.reconciler_disabled")
        return 0
    if not settings.regional_quota_leases_enabled:
        logger.error("regional_quota.reconciler_ledger_disabled")
        return 1

    store = create_store(settings)
    configure_store(store)
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
    record_heartbeat("job:regional-quota-reconcile", settings=settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
