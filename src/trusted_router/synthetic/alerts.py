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
            limit=threshold + 2,
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
    # Fire at the transition into the streak. The window is TWO values, not
    # one: some probe series have two concurrent writers (GCP's two monitor
    # regions both record peer_monitor samples), and if both record a down
    # before either runs this check, the observed streak jumps 2 -> 4 and an
    # exact `== threshold` match would suppress the alert forever. A double
    # fire at 3 and 4 folds into one Sentry issue via the fingerprint; past
    # threshold+1 the streak stays silent until the probe recovers.
    if not (threshold <= streak <= threshold + 1):
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


def ops_alert(
    message: str,
    *,
    fingerprint: list[str],
    tags: dict[str, str] | None = None,
) -> bool:
    """A page-worthy operational alarm: logged AND captured as a Sentry issue.

    Exists because ``LoggingIntegration(event_level=None)`` deliberately keeps
    logger output out of Sentry issues (INFO->Axiom, WARNING+->Sentry logs) —
    which meant every ``logger.error("ALERT ...")`` money alarm in the outbox
    and dead-letter paths shipped to a log store nobody watches. Sites that
    mean "a human must look at this" call ops_alert instead of logger.error;
    the rendered message stays byte-identical so runbook greps keep working.

    Fingerprinted per alarm class so a recurring condition folds into one
    issue that re-opens, and exception-swallowed so alerting can never break
    the money path it is reporting on. Returns True when the capture reached
    the Sentry SDK.
    """
    logger.error(message)
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["ops-alert", *fingerprint]
            for key, value in (tags or {}).items():
                scope.set_tag(key, value)
            sentry_sdk.capture_message(message, level="error")
    except Exception:  # noqa: BLE001 - Sentry unavailable must not break the caller
        logger.exception("ops alert capture failed")
        return False
    return True
