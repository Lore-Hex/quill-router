"""Early single-flight admission for the regional quota reconciler."""

from __future__ import annotations

import logging
import os

from trusted_router.gcs_singleflight import GCSGenerationLease, GCSLeaseConfig

logger = logging.getLogger(__name__)
_TASK_TIMEOUT_SECONDS = 180.0


def _lease_from_environment() -> GCSGenerationLease:
    lease_seconds = float(
        os.environ.get("TR_REGIONAL_QUOTA_RECONCILER_LOCK_LEASE_SECONDS", "240")
    )
    if lease_seconds <= _TASK_TIMEOUT_SECONDS:
        raise ValueError(
            "regional quota reconciler lease must exceed the Cloud Run task timeout"
        )
    return GCSGenerationLease(
        GCSLeaseConfig(
            bucket=os.environ.get(
                "TR_REGIONAL_QUOTA_RECONCILER_LOCK_BUCKET", ""
            ).strip(),
            object_name=os.environ.get(
                "TR_REGIONAL_QUOTA_RECONCILER_LOCK_OBJECT",
                "regional-quota-reconciler/singleflight.json",
            ).strip(),
            lease_seconds=lease_seconds,
            min_interval_seconds=float(
                os.environ.get(
                    "TR_REGIONAL_QUOTA_RECONCILER_MIN_INTERVAL_SECONDS", "50"
                )
            ),
            failure_cooldown_seconds=float(
                os.environ.get(
                    "TR_REGIONAL_QUOTA_RECONCILER_FAILURE_COOLDOWN_SECONDS", "30"
                )
            ),
        )
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        singleflight = _lease_from_environment()
        lease = singleflight.acquire()
    except Exception:
        logger.exception("regional_quota.reconciler_singleflight_failed")
        return 1
    if lease is None:
        logger.info("regional_quota.reconciler_singleflight_skip")
        return 0

    logger.info(
        "regional_quota.reconciler_singleflight_acquired owner=%s generation=%d",
        lease.owner,
        lease.generation,
    )
    exit_code = 1
    try:
        # Keep this import behind admission. Importing application config and
        # the GCP clients is part of the expensive initialization this gate
        # prevents duplicate executions from performing.
        from trusted_router.regional_quota_reconcile_cli import main as reconcile

        exit_code = reconcile()
    except Exception:
        logger.exception("regional_quota.reconciler_unhandled_failure")
        exit_code = 1
    try:
        cooldown_seconds = singleflight.finish(
            lease,
            succeeded=exit_code == 0,
        )
    except Exception:
        logger.exception("regional_quota.reconciler_singleflight_release_failed")
        return 1
    logger.info(
        "regional_quota.reconciler_singleflight_released success=%s cooldown_seconds=%.1f",
        exit_code == 0,
        cooldown_seconds,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
