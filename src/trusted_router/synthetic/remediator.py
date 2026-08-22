"""The standing remediator: detect -> decide -> (observe | act) -> record.

Command-center Increment 3. Before this module every remediation-relevant
consumer of health signals ran only inside a deploy window (the watchdog) or
terminated at a human (Sentry). This is the standing control loop: it runs
in-process on EVERY control plane, polices its OWN cloud's signals on a
fixed cadence, and records every decision it makes as a durable, queryable
row — so automation history is evidence, not folklore.

MODES (settings.remediator_mode):
  * "off"      — loop does not start.
  * "observe"  — detectors run, decisions are recorded and page-worthy ones
                 alert, but NOTHING that would move traffic or mutate state
                 executes. This is the calibration mode: a week of decision
                 rows tells us the flap rate before any actuator goes live.
  * "act"      — reserved for the actuator increment (route-quarantine
                 overrides et al.). Until actuators land, "act" behaves as
                 "observe" — the mode gate exists now so flipping later is
                 config, not code.

DECISIONS ARE SAMPLES. Each decision is recorded as a synthetic sample
(probe_type="remediation", target="<playbook>:<subject>") — the same store,
retention, and /fleet rendering path as every other ops signal. Each process
records at most one onset row per (playbook, subject, 30-minute bucket), so
the store can receive one row per control-plane instance for the same bucket;
readers counting conditions should deduplicate by (target, bucket). A process
also records at most one resolution row for each onset it observed.
status="down" means "condition present" and status="up" means that condition
cleared. remediation rows are in OPS_PROBE_TYPES: never a component, never an
SLO, never the freshness clock.

DETECTORS (v1, all T0 blast radius — detection and paging only):
  * stale heartbeats     — a scheduler this deployment expected to beat has
                           gone quiet (the /fleet render of this was
                           informational; the remediator makes it page).
  * route health         — routes the evaluator says deserve quarantine
                           (>=95% structural failure over 48h). In observe
                           mode this records the would-quarantine decision
                           the hourly Sentry report already implies; the
                           actuator lands in the next increment.
  * monitor freshness    — this deployment's own probe fleet has stopped
                           reporting (peers will also catch this within
                           three cadences; local detection is faster).

Every detector is exception-isolated: a broken detector loses its own
signal, never the loop, and the loop itself heartbeats so /fleet shows the
remediator's own liveness.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trusted_router.config import Settings
from trusted_router.storage_models import SyntheticProbeSample
from trusted_router.synthetic.alerts import ops_alert

logger = logging.getLogger(__name__)

REMEDIATION_PROBE = "remediation"
# At most one onset row per (playbook, subject, bucket) per process. A
# persistent condition therefore produces about 12 rows per process over six
# hours, and the store's row count scales with the control-plane instance
# count. Readers counting conditions should deduplicate by (target, bucket).
DECISION_BUCKET_SECONDS = 30 * 60
_DECISION_MARKS: dict[str, int] = {}


@dataclass(frozen=True)
class Decision:
    playbook: str
    subject: str
    detail: str
    page: bool  # page-worthy conditions ops_alert in ANY mode; detection is not action


@dataclass(frozen=True)
class _ActiveCondition:
    first_seen_at: float
    detector: Callable[[Settings], list[Decision]]


_ACTIVE_CONDITIONS: dict[str, _ActiveCondition] = {}


def _record_decision(decision: Decision, *, settings: Settings) -> None:
    bucket = int(time.time() // DECISION_BUCKET_SECONDS)
    mark_key = f"{decision.playbook}:{decision.subject}"
    if _DECISION_MARKS.get(mark_key) == bucket:
        return
    try:
        from trusted_router.storage import STORE

        STORE.record_synthetic_probe_sample(
            SyntheticProbeSample(
                id=f"syn_rem_{uuid.uuid4().hex[:12]}_{bucket}",
                probe_type=REMEDIATION_PROBE,
                target=mark_key,
                target_url="",
                monitor_region=settings.synthetic_monitor_region or settings.primary_region,
                status="down",  # condition present
                error_type=decision.detail[:200],
            )
        )
        _DECISION_MARKS[mark_key] = bucket
    except Exception:  # noqa: BLE001 - recording must never break the loop
        logger.exception("remediator decision record failed for %s", mark_key)
    if decision.page:
        ops_alert(
            f"remediator[{settings.remediator_mode}] {decision.playbook}: "
            f"{decision.subject} — {decision.detail}",
            fingerprint=["remediator", decision.playbook, decision.subject],
            tags={"playbook": decision.playbook, "mode": settings.remediator_mode},
        )


def _record_resolution(
    mark_key: str,
    *,
    first_seen_at: float,
    settings: Settings,
) -> None:
    now = time.time()
    bucket = int(now // DECISION_BUCKET_SECONDS)
    present_seconds = int(max(now - first_seen_at, 0))
    try:
        from trusted_router.storage import STORE

        STORE.record_synthetic_probe_sample(
            SyntheticProbeSample(
                id=f"syn_rem_{uuid.uuid4().hex[:12]}_{bucket}",
                probe_type=REMEDIATION_PROBE,
                target=mark_key,
                target_url="",
                monitor_region=settings.synthetic_monitor_region or settings.primary_region,
                status="up",  # condition cleared
                error_type=f"condition cleared after {present_seconds}s",
            )
        )
    except Exception:  # noqa: BLE001 - recording must never break the loop
        logger.exception("remediator resolution record failed for %s", mark_key)


def _detect_stale_heartbeats(settings: Settings) -> list[Decision]:
    from trusted_router.synthetic.fleet import _heartbeat_rows

    decisions = []
    for row in _heartbeat_rows():
        if row.get("stale"):
            decisions.append(
                Decision(
                    playbook="heartbeat-stale",
                    subject=str(row["name"]),
                    detail=(
                        f"no beat for {row.get('age_seconds')}s "
                        f"(last {row.get('last_beat_at')}); the scheduler behind this "
                        "job is dead or wedged"
                    ),
                    page=True,
                )
            )
    return decisions


def _detect_route_quarantine(settings: Settings) -> list[Decision]:
    from trusted_router.storage import STORE
    from trusted_router.synthetic.route_health import evaluate_route_health

    decisions = []
    for flag in evaluate_route_health(STORE):
        decisions.append(
            Decision(
                playbook="route-quarantine",
                subject=f"{flag.provider}/{flag.model}",
                detail=(
                    f"structural failure rate {flag.failure_rate:.0%} over "
                    f"{flag.samples} samples (newest: {flag.newest_error_type}); "
                    "would quarantine"
                    if settings.remediator_mode != "act"
                    else "quarantine actuator not yet shipped; recording only"
                ),
                # The hourly route-health report already pages this class;
                # the decision row is the remediator's contribution here.
                page=False,
            )
        )
    return decisions


def _detect_monitor_stale(settings: Settings) -> list[Decision]:
    import datetime as dt

    from trusted_router.storage import STORE
    from trusted_router.storage_models import utcnow
    from trusted_router.synthetic.components import OPS_PROBE_TYPES
    from trusted_router.synthetic.status import CURRENT_SAMPLE_TTL_SECONDS

    samples = STORE.synthetic_probe_samples(limit=50)
    probe_samples = [s for s in samples if s.probe_type not in OPS_PROBE_TYPES]
    if not probe_samples:
        return []  # nothing ever recorded: provisioning, not an outage
    newest = max(probe_samples, key=lambda s: s.created_at)
    try:
        created = dt.datetime.fromisoformat(newest.created_at.replace("Z", "+00:00"))
    except ValueError:
        return []
    age = (utcnow() - created).total_seconds()
    if age <= CURRENT_SAMPLE_TTL_SECONDS:
        return []
    return [
        Decision(
            playbook="monitor-stale",
            subject="probe-fleet",
            detail=(
                f"newest real probe sample is {int(age)}s old "
                f"(freshness contract {CURRENT_SAMPLE_TTL_SECONDS}s); this "
                "deployment is serving blind"
            ),
            page=True,
        )
    ]


DETECTORS = (
    _detect_stale_heartbeats,
    _detect_route_quarantine,
    _detect_monitor_stale,
)


def run_remediator_pass(settings: Settings) -> list[Decision]:
    """One detect->decide->record pass. Returns the decisions made (for
    tests and for callers that want to render them immediately)."""
    all_decisions: list[Decision] = []
    for detector in DETECTORS:
        try:
            decisions = detector(settings)
        except Exception:  # noqa: BLE001 - one broken detector must not kill the rest
            logger.exception("remediator detector %s failed", detector.__name__)
            continue
        present_keys = {f"{decision.playbook}:{decision.subject}" for decision in decisions}
        for decision in decisions:
            mark_key = f"{decision.playbook}:{decision.subject}"
            if mark_key not in _ACTIVE_CONDITIONS:
                _ACTIVE_CONDITIONS[mark_key] = _ActiveCondition(
                    first_seen_at=time.time(),
                    detector=detector,
                )
            _record_decision(decision, settings=settings)
        resolved = [
            (mark_key, active)
            for mark_key, active in _ACTIVE_CONDITIONS.items()
            if active.detector is detector and mark_key not in present_keys
        ]
        for mark_key, active in resolved:
            _record_resolution(
                mark_key,
                first_seen_at=active.first_seen_at,
                settings=settings,
            )
            # Forget even when storage fails: recording remains best-effort,
            # and one clear observation must produce at most one attempt.
            _ACTIVE_CONDITIONS.pop(mark_key, None)
            _DECISION_MARKS.pop(mark_key, None)
        all_decisions.extend(decisions)
    return all_decisions


def recent_decisions(limit: int = 20) -> list[dict[str, Any]]:
    """Latest decision rows for /fleet — the automation's visible memory."""
    from trusted_router.storage import STORE

    samples = STORE.synthetic_probe_samples(probe_type=REMEDIATION_PROBE, limit=limit)
    rows = sorted(samples, key=lambda s: s.created_at, reverse=True)[:limit]
    return [
        {
            "at": row.created_at,
            "decision": row.target,
            "detail": row.error_type,
        }
        for row in rows
    ]


def reset_for_tests() -> None:
    _DECISION_MARKS.clear()
    _ACTIVE_CONDITIONS.clear()
