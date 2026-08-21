"""One-shot regional quota lease reconciler for Cloud Run Jobs."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, cast

from trusted_router.config import get_settings
from trusted_router.storage import create_store

logger = logging.getLogger(__name__)


def _reconcile_limit() -> int:
    raw = os.environ.get("TR_REGIONAL_QUOTA_RECONCILE_LIMIT", "250")
    try:
        return max(1, min(int(raw), 1_000))
    except ValueError as exc:
        raise ValueError("TR_REGIONAL_QUOTA_RECONCILE_LIMIT must be an integer") from exc


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.regional_quota_leases_enabled:
        logger.info("regional_quota.reconciler_disabled")
        return 0

    store = create_store(settings)
    reconcile = cast(
        Callable[..., dict[str, int]] | None,
        getattr(store, "reconcile_regional_quota_leases", None),
    )
    if reconcile is None:
        logger.error("regional_quota.reconciler_store_unsupported")
        return 1

    result: dict[str, Any] = reconcile(limit=_reconcile_limit())
    logger.info(
        "regional_quota.reconcile_complete inspected=%d reconciled=%d closed=%d errors=%d",
        int(result.get("inspected", 0)),
        int(result.get("reconciled", 0)),
        int(result.get("closed", 0)),
        int(result.get("errors", 0)),
    )
    return 1 if int(result.get("errors", 0)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
