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
readers deduplicate rows by (target, bucket, id). Resolution writes use a
stable id and have a small retry budget, but a backend that commits and then
raises can still leave duplicate physical rows. That is an accepted property
of this best-effort ops timeline, not evidence of two events.
status="down" means "condition present" and status="up" means that condition
cleared. remediation rows are in OPS_PROBE_TYPES: never a component, never an
SLO, never the freshness clock.

Only stale-heartbeat decisions can resolve. Heartbeat rows give positive,
subject-specific evidence that a previously stale scheduler is healthy now.
The other detectors infer their conditions from bounded or filtered reads: an
empty route evaluation or an absent real probe is absence of evidence, not
evidence of health. A condition inferred from absence of evidence must never
be reported as cleared.

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
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
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
MAX_RESOLUTION_ATTEMPTS = 3
MAX_PENDING_RESOLUTIONS = 128
_DECISION_MARKS: dict[str, int] = {}


@dataclass(frozen=True)
class Decision:
    playbook: str
    subject: str
    detail: str
    page: bool  # page-worthy conditions ops_alert in ANY mode; detection is not action


@dataclass(frozen=True)
class DetectionResult:
    decisions: tuple[Decision, ...]
    # Full durable keys positively observed healthy in this exact pass. Empty
    # is deliberately the default: resolution is an explicit detector opt-in.
    resolution_keys: frozenset[str] = frozenset()


Detector = Callable[[Settings], DetectionResult]
_ActiveKey = tuple[Detector, str]


@dataclass(frozen=True)
class _ActiveCondition:
    first_seen_at: float
    onset_recorded: bool = False
    pending_resolution: SyntheticProbeSample | None = None
    resolution_attempts: int = 0
    resolution_in_flight: bool = False
    recurrence: tuple[Detector, Decision] | None = None


_ACTIVE_CONDITIONS: dict[_ActiveKey, _ActiveCondition] = {}
_ACTIVE_LOCK = threading.Lock()


def _record_decision(decision: Decision, *, settings: Settings) -> bool:
    bucket = int(time.time() // DECISION_BUCKET_SECONDS)
    mark_key = f"{decision.playbook}:{decision.subject}"
    if _DECISION_MARKS.get(mark_key) == bucket:
        return True
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
        recorded = True
    except Exception:  # noqa: BLE001 - recording must never break the loop
        logger.exception("remediator decision record failed for %s", mark_key)
        recorded = False
    if decision.page:
        ops_alert(
            f"remediator[{settings.remediator_mode}] {decision.playbook}: "
            f"{decision.subject} — {decision.detail}",
            fingerprint=["remediator", decision.playbook, decision.subject],
            tags={"playbook": decision.playbook, "mode": settings.remediator_mode},
        )
    return recorded


def _finish_onset(
    active_key: _ActiveKey,
    active: _ActiveCondition,
    *,
    recorded: bool,
) -> None:
    with _ACTIVE_LOCK:
        if _ACTIVE_CONDITIONS.get(active_key) is not active:
            return
        if recorded:
            _ACTIVE_CONDITIONS[active_key] = replace(
                active,
                onset_recorded=True,
            )
        else:
            # No stored onset means this process has no occurrence it may
            # later resolve. A future stale pass can try the onset again.
            _ACTIVE_CONDITIONS.pop(active_key)


def _record_resolution(
    sample: SyntheticProbeSample,
) -> bool:
    try:
        from trusted_router.storage import STORE

        STORE.record_synthetic_probe_sample(sample)
    except Exception:  # noqa: BLE001 - recording must never break the loop
        logger.exception("remediator resolution record failed for %s", sample.target)
        return False
    return True


def _resolution_sample(
    mark_key: str,
    *,
    first_seen_at: float,
    settings: Settings,
) -> SyntheticProbeSample:
    now = time.time()
    present_seconds = int(max(now - first_seen_at, 0))
    # The same onset always produces the same id. If a backend commits and then
    # reports failure, retrying cannot create a second logical resolution row.
    resolution_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"trusted-router:remediation:{mark_key}:{first_seen_at.hex()}",
    )
    return SyntheticProbeSample(
        id=f"syn_rem_res_{resolution_id.hex}",
        probe_type=REMEDIATION_PROBE,
        target=mark_key,
        target_url="",
        monitor_region=settings.synthetic_monitor_region or settings.primary_region,
        status="up",  # condition cleared
        error_type=f"condition cleared after {present_seconds}s",
    )


def _detect_stale_heartbeats(settings: Settings) -> DetectionResult:
    from trusted_router.synthetic.fleet import _heartbeat_rows

    decisions = []
    healthy_keys = set()
    rows = _heartbeat_rows()
    for row in rows:
        subject = str(row["name"])
        if row.get("stale"):
            decisions.append(
                Decision(
                    playbook="heartbeat-stale",
                    subject=subject,
                    detail=(
                        f"no beat for {row.get('age_seconds')}s "
                        f"(last {row.get('last_beat_at')}); the scheduler behind this "
                        "job is dead or wedged"
                    ),
                    page=True,
                )
            )
        elif row.get("stale") is False:
            # Resolution requires this subject to be present and explicitly
            # healthy in the non-empty row set evaluated by this pass.
            healthy_keys.add(f"heartbeat-stale:{subject}")
    return DetectionResult(tuple(decisions), frozenset(healthy_keys))


def _detect_route_quarantine(settings: Settings) -> DetectionResult:
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
    return DetectionResult(tuple(decisions))


def _detect_monitor_stale(settings: Settings) -> DetectionResult:
    import datetime as dt

    from trusted_router.storage import STORE
    from trusted_router.storage_models import utcnow
    from trusted_router.synthetic.components import OPS_PROBE_TYPES
    from trusted_router.synthetic.status import CURRENT_SAMPLE_TTL_SECONDS

    samples = STORE.synthetic_probe_samples(limit=50)
    probe_samples = [s for s in samples if s.probe_type not in OPS_PROBE_TYPES]
    if not probe_samples:
        # The store API can select one exact probe type but cannot exclude all
        # ops types. An empty filtered window can therefore mean provisioning
        # OR that fresh ops rows crowded a dead probe fleet out of the read.
        return DetectionResult(())
    newest = max(probe_samples, key=lambda s: s.created_at)
    try:
        created = dt.datetime.fromisoformat(newest.created_at.replace("Z", "+00:00"))
    except ValueError:
        return DetectionResult(())
    age = (utcnow() - created).total_seconds()
    if age <= CURRENT_SAMPLE_TTL_SECONDS:
        return DetectionResult(())
    return DetectionResult(
        (
            Decision(
                playbook="monitor-stale",
                subject="probe-fleet",
                detail=(
                    f"newest real probe sample is {int(age)}s old "
                    f"(freshness contract {CURRENT_SAMPLE_TTL_SECONDS}s); this "
                    "deployment is serving blind"
                ),
                page=True,
            ),
        ),
    )


# This is deliberately a module constant. Detectors are not added, removed, or
# replaced while a process is running, so an active key cannot be orphaned by a
# live registry mutation. Including the detector in each active-map key keeps
# two detectors that emit the same durable target from overwriting each other.
DETECTORS: tuple[Detector, ...] = (
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
            result = detector(settings)
        except Exception:  # noqa: BLE001 - one broken detector must not kill the rest
            logger.exception("remediator detector %s failed", detector.__name__)
            continue
        decisions = result.decisions
        present_keys = {f"{decision.playbook}:{decision.subject}" for decision in decisions}
        deferred_keys: set[str] = set()
        new_onsets: dict[str, tuple[_ActiveKey, _ActiveCondition]] = {}
        resolutions: list[tuple[_ActiveKey, SyntheticProbeSample]] = []
        with _ACTIVE_LOCK:
            for decision in decisions:
                mark_key = f"{decision.playbook}:{decision.subject}"
                active_key = (detector, mark_key)
                pending_owner = next(
                    (
                        (key, active)
                        for key, active in _ACTIVE_CONDITIONS.items()
                        if key[1] == mark_key and active.pending_resolution is not None
                    ),
                    None,
                )
                if pending_owner is not None:
                    owner_key, owner = pending_owner
                    _ACTIVE_CONDITIONS[owner_key] = replace(
                        owner,
                        recurrence=(detector, decision),
                    )
                    deferred_keys.add(mark_key)
                    continue
                if active_key not in _ACTIVE_CONDITIONS:
                    active = _ActiveCondition(
                        first_seen_at=time.time(),
                    )
                    _ACTIVE_CONDITIONS[active_key] = active
                    new_onsets[mark_key] = (active_key, active)
                elif not _ACTIVE_CONDITIONS[active_key].onset_recorded:
                    deferred_keys.add(mark_key)

            pending_count = sum(
                active.pending_resolution is not None
                for active in _ACTIVE_CONDITIONS.values()
            )
            for mark_key in result.resolution_keys - present_keys:
                active_key = (detector, mark_key)
                candidate = _ACTIVE_CONDITIONS.get(active_key)
                if (
                    candidate is None
                    or not candidate.onset_recorded
                    or candidate.pending_resolution is not None
                ):
                    continue
                if any(key != active_key and key[1] == mark_key for key in _ACTIVE_CONDITIONS):
                    _ACTIVE_CONDITIONS.pop(active_key)
                    continue
                if pending_count >= MAX_PENDING_RESOLUTIONS:
                    continue
                _ACTIVE_CONDITIONS[active_key] = replace(
                    candidate,
                    pending_resolution=_resolution_sample(
                        mark_key,
                        first_seen_at=candidate.first_seen_at,
                        settings=settings,
                    ),
                )
                pending_count += 1

            # Retry only while this pass again contains positive healthy
            # evidence for the same subject. Empty/failed reads resolve
            # nothing, including previously pending work.
            for active_key, active in list(_ACTIVE_CONDITIONS.items()):
                if (
                    active_key[0] is not detector
                    or active_key[1] not in result.resolution_keys
                    or active.pending_resolution is None
                    or active.resolution_in_flight
                    or active.resolution_attempts >= MAX_RESOLUTION_ATTEMPTS
                ):
                    continue
                _ACTIVE_CONDITIONS[active_key] = replace(
                    active,
                    resolution_in_flight=True,
                )
                resolutions.append((active_key, active.pending_resolution))

        for decision in decisions:
            mark_key = f"{decision.playbook}:{decision.subject}"
            if mark_key not in deferred_keys:
                recorded = _record_decision(decision, settings=settings)
                if mark_key in new_onsets:
                    active_key, active = new_onsets[mark_key]
                    _finish_onset(active_key, active, recorded=recorded)
        for active_key, resolution in resolutions:
            recorded = _record_resolution(resolution)
            recurrence: tuple[Detector, Decision] | None = None
            recurring_onset: tuple[_ActiveKey, _ActiveCondition, Decision] | None = None
            with _ACTIVE_LOCK:
                current = _ACTIVE_CONDITIONS.get(active_key)
                if current is None or current.pending_resolution != resolution:
                    continue
                attempts = current.resolution_attempts + 1
                if recorded or attempts >= MAX_RESOLUTION_ATTEMPTS:
                    _ACTIVE_CONDITIONS.pop(active_key)
                    recurrence = current.recurrence
                    # A completed (or exhausted best-effort) occurrence forgets
                    # its bucket before a recurrence writes. Thus each subject's
                    # occurrence ordering is onset, resolution, onset: never a
                    # resolution followed by a current condition with no onset.
                    _DECISION_MARKS.pop(active_key[1], None)
                    if recurrence is not None:
                        recurring_detector, recurring_decision = recurrence
                        recurring_key = (recurring_detector, active_key[1])
                        recurring_active = _ActiveCondition(
                            first_seen_at=time.time(),
                        )
                        _ACTIVE_CONDITIONS[recurring_key] = recurring_active
                        recurring_onset = (
                            recurring_key,
                            recurring_active,
                            recurring_decision,
                        )
                else:
                    _ACTIVE_CONDITIONS[active_key] = replace(
                        current,
                        resolution_attempts=attempts,
                        resolution_in_flight=False,
                    )
            if recurring_onset is not None:
                recurring_key, recurring_active, recurring_decision = recurring_onset
                onset_recorded = _record_decision(recurring_decision, settings=settings)
                _finish_onset(
                    recurring_key,
                    recurring_active,
                    recorded=onset_recorded,
                )
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
    with _ACTIVE_LOCK:
        _DECISION_MARKS.clear()
        _ACTIVE_CONDITIONS.clear()
