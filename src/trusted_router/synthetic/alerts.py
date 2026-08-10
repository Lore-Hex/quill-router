"""Sentry alerting for synthetic probe failure streaks.

A probe that fails once is noise; a probe that fails three times in a row is
an outage of whatever that probe proves. Before this module, deep inference
probes could fail for days with no page and no alert — the samples were
recorded faithfully and surfaced nowhere (2026-08-10: every model probe on
AWS and Azure was failing with 402 while both status pages showed green).

Alerts fire exactly at the transition into a streak (the Nth consecutive
failure) so a sustained outage produces one Sentry issue, not one event per
probe cycle. The fingerprint groups by (probe_type, target), so each broken
probe path is its own issue and re-opens if it regresses after recovery.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

STREAK_THRESHOLD = 3


def alert_on_failure_streak(
    store: Any,
    sample: Any,
    *,
    threshold: int = STREAK_THRESHOLD,
) -> bool:
    """Capture a Sentry event when `sample` is the `threshold`-th consecutive
    failure for its (probe_type, target). Returns True when an event fired.

    Called after the sample is recorded, so the store query includes it.
    """
    if getattr(sample, "status", None) == "up":
        return False
    try:
        recent = store.synthetic_probe_samples(
            probe_type=sample.probe_type,
            target=sample.target,
            limit=threshold + 1,
        )
    except Exception:  # noqa: BLE001 - alerting must never break ingestion
        logger.exception("synthetic alert streak query failed")
        return False

    # Newest first is the storage contract; tolerate oldest-first defensively.
    ordered = sorted(recent, key=lambda s: s.created_at, reverse=True)
    streak = 0
    for row in ordered:
        if row.status == "up":
            break
        streak += 1
    # Fire only at the exact transition into the streak: the (threshold+1)-th
    # failure and beyond stay silent until the probe recovers and breaks again.
    if streak != threshold:
        return False

    message = (
        f"synthetic probe down x{threshold}: {sample.probe_type} on {sample.target} "
        f"(monitor_region={sample.monitor_region}, error={sample.error_type}, "
        f"http_status={sample.http_status})"
    )
    logger.error(message)
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["synthetic-streak", sample.probe_type, sample.target]
            scope.set_tag("probe_type", sample.probe_type)
            scope.set_tag("target", sample.target)
            scope.set_tag("monitor_region", sample.monitor_region or "unknown")
            scope.set_tag("error_type", sample.error_type or "unknown")
            sentry_sdk.capture_message(message, level="error")
    except Exception:  # noqa: BLE001 - Sentry unavailable must not break ingestion
        logger.exception("synthetic alert capture failed")
        return False
    return True
