"""Tests for the standing remediator (command-center Increment 3).

Observe-mode contract: detectors run, decisions are recorded as remediation
samples and page-worthy ones alert — and nothing else happens. A broken
detector loses its own signal, never the pass.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from trusted_router.config import Settings
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import SyntheticProbeSample, utcnow
from trusted_router.synthetic import fleet, remediator
from trusted_router.synthetic.remediator import (
    Decision,
    DetectionResult,
    recent_decisions,
    run_remediator_pass,
)
from trusted_router.synthetic.route_health import RouteHealthFlag


@pytest.fixture(autouse=True)
def _reset() -> None:
    remediator.reset_for_tests()
    fleet.reset_for_tests()


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import sentry_sdk

    events: list[str] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda message, level=None: events.append(message),
    )
    return events


def _settings(**kwargs: object) -> Settings:
    return Settings(environment="test", **kwargs)  # type: ignore[arg-type]


def _old_iso(minutes: int) -> str:
    return (utcnow() - dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _record(sample: SyntheticProbeSample) -> None:
    STORE.record_synthetic_probe_sample(sample)


def _remediation_samples(target: str) -> list[SyntheticProbeSample]:
    return sorted(
        (
            sample
            for sample in STORE.synthetic_probe_samples(
                probe_type=remediator.REMEDIATION_PROBE,
                limit=100,
            )
            if sample.target == target
        ),
        key=lambda sample: sample.created_at,
    )


def test_stale_heartbeat_produces_paged_decision(sentry_events: list[str]) -> None:
    fleet.register_heartbeat_target("scheduler:dead")
    _record(
        SyntheticProbeSample(
            id="syn_hb_dead_sched_rem",
            probe_type="heartbeat",
            target="scheduler:dead",
            target_url="",
            monitor_region="test-1",
            status="up",
            created_at=_old_iso(60),
        )
    )

    decisions = run_remediator_pass(_settings())

    stale = [d for d in decisions if d.playbook == "heartbeat-stale"]
    assert [d.subject for d in stale] == ["scheduler:dead"]
    assert any("remediator[observe] heartbeat-stale" in e for e in sentry_events)
    recorded = recent_decisions()
    assert any(row["decision"] == "heartbeat-stale:scheduler:dead" for row in recorded)


def test_heartbeat_resolution_requires_subject_present_and_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "heartbeat-stale:scheduler:subject"
    rows = [
        {
            "name": "scheduler:subject",
            "stale": True,
            "age_seconds": 999,
            "last_beat_at": _old_iso(30),
        }
    ]
    monkeypatch.setattr(fleet, "_heartbeat_rows", lambda: rows)
    run_remediator_pass(_settings())

    rows[:] = [{"name": "scheduler:other", "stale": False}]
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples(target)] == ["down"]

    rows[:] = [{"name": "scheduler:subject", "stale": False}]
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples(target)] == ["down", "up"]


def test_heartbeat_empty_or_raising_read_resolves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "heartbeat-stale:scheduler:uncertain"
    rows = [
        {
            "name": "scheduler:uncertain",
            "stale": True,
            "age_seconds": 999,
            "last_beat_at": _old_iso(30),
        }
    ]
    monkeypatch.setattr(fleet, "_heartbeat_rows", lambda: rows)
    run_remediator_pass(_settings())

    rows.clear()
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples(target)] == ["down"]

    def raising_rows() -> list[dict[str, object]]:
        raise RuntimeError("heartbeat read unavailable")

    monkeypatch.setattr(fleet, "_heartbeat_rows", raising_rows)
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples(target)] == ["down"]


def test_monitor_stale_pages_when_probe_fleet_dies(sentry_events: list[str]) -> None:
    # Only OLD real probe samples + a FRESH heartbeat: the remediator must
    # call the probe fleet dead (heartbeats are not probes).
    _record(
        SyntheticProbeSample(
            id="syn_tls_old_rem",
            probe_type="tls_health",
            target="canonical",
            target_url="https://api.trustedrouter.com/v1",
            monitor_region="test-1",
            status="up",
            created_at=_old_iso(30),
        )
    )
    _record(
        SyntheticProbeSample(
            id="syn_hb_fresh_rem",
            probe_type="heartbeat",
            target="scheduler:synthetic",
            target_url="",
            monitor_region="test-1",
            status="up",
        )
    )

    decisions = run_remediator_pass(_settings())

    assert any(d.playbook == "monitor-stale" for d in decisions)
    assert any("monitor-stale" in e for e in sentry_events)


def test_ops_rows_crowding_probe_window_do_not_resolve_dead_fleet(
    sentry_events: list[str],
) -> None:
    target = "monitor-stale:probe-fleet"
    _record(
        SyntheticProbeSample(
            id="syn_tls_old_crowded_rem",
            probe_type="tls_health",
            target="canonical",
            target_url="https://api.trustedrouter.com/v1",
            monitor_region="test-1",
            status="up",
            created_at=_old_iso(30),
        )
    )
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples(target)] == ["down"]

    # The store cannot express probe_type NOT IN OPS_PROBE_TYPES. Fill its
    # newest-50 mixed window with fresh operational rows while the only real
    # probe remains stale and outside the window.
    for index in range(50):
        _record(
            SyntheticProbeSample(
                id=f"syn_ops_crowd_{index}",
                probe_type="heartbeat",
                target=f"scheduler:crowd-{index}",
                target_url="",
                monitor_region="test-1",
                status="up",
            )
        )

    assert not any(
        decision.playbook == "monitor-stale"
        for decision in run_remediator_pass(_settings())
    )
    assert [sample.status for sample in _remediation_samples(target)] == ["down"]


def test_fresh_probes_produce_no_monitor_decision(sentry_events: list[str]) -> None:
    _record(
        SyntheticProbeSample(
            id="syn_tls_fresh_rem",
            probe_type="tls_health",
            target="canonical",
            target_url="https://api.trustedrouter.com/v1",
            monitor_region="test-1",
            status="up",
        )
    )

    decisions = run_remediator_pass(_settings())

    assert not any(d.playbook == "monitor-stale" for d in decisions)


def test_monitor_stale_never_resolves_even_with_fresh_positive_sample() -> None:
    target = "monitor-stale:probe-fleet"
    _record(
        SyntheticProbeSample(
            id="syn_tls_old_never_resolves",
            probe_type="tls_health",
            target="canonical",
            target_url="https://api.trustedrouter.com/v1",
            monitor_region="test-1",
            status="up",
            created_at=_old_iso(30),
        )
    )
    run_remediator_pass(_settings())

    _record(
        SyntheticProbeSample(
            id="syn_tls_fresh_never_resolves",
            probe_type="tls_health",
            target="canonical",
            target_url="https://api.trustedrouter.com/v1",
            monitor_region="test-1",
            status="up",
        )
    )
    run_remediator_pass(_settings())

    assert [sample.status for sample in _remediation_samples(target)] == ["down"]


def test_route_quarantine_never_resolves_for_empty_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import route_health

    target = "route-quarantine:provider/model"
    flags = [
        RouteHealthFlag(
            provider="provider",
            model="model",
            samples=10,
            failures=10,
            failure_rate=1.0,
            newest_error_type="BadRequest",
            newest_error_message="gone",
        )
    ]
    monkeypatch.setattr(route_health, "evaluate_route_health", lambda store: flags)
    run_remediator_pass(_settings())

    flags.clear()
    run_remediator_pass(_settings())
    run_remediator_pass(_settings())

    assert [sample.status for sample in _remediation_samples(target)] == ["down"]


def test_decision_rows_bucket_and_dedupe(sentry_events: list[str]) -> None:
    fleet.register_heartbeat_target("scheduler:bucketed")
    _record(
        SyntheticProbeSample(
            id="syn_hb_dead_bucket",
            probe_type="heartbeat",
            target="scheduler:bucketed",
            target_url="",
            monitor_region="test-1",
            status="up",
            created_at=_old_iso(90),
        )
    )
    run_remediator_pass(_settings())
    first = [r for r in recent_decisions() if "bucketed" in r["decision"]]
    run_remediator_pass(_settings())  # same bucket: no second row
    second = [r for r in recent_decisions() if "bucketed" in r["decision"]]
    assert len(first) == len(second) == 1


def test_persistent_condition_records_bucketed_onsets_without_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "persistent", "still present", page=False)

    def detector(settings: Settings) -> DetectionResult:
        return DetectionResult((decision,))

    now = [100.0]
    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    monkeypatch.setattr(remediator.time, "time", lambda: now[0])

    run_remediator_pass(_settings())
    now[0] += remediator.DECISION_BUCKET_SECONDS
    run_remediator_pass(_settings())
    now[0] += remediator.DECISION_BUCKET_SECONDS
    run_remediator_pass(_settings())

    samples = _remediation_samples("test-playbook:persistent")
    assert [sample.status for sample in samples] == ["down", "down", "down"]
    assert not any("cleared" in (sample.error_type or "") for sample in samples)


def test_cleared_condition_records_exactly_one_resolution(
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[str],
) -> None:
    decisions = [Decision("test-playbook", "clears", "present", page=True)]
    resolution_key = "test-playbook:clears"

    def detector(settings: Settings) -> DetectionResult:
        return DetectionResult(
            tuple(decisions),
            frozenset() if decisions else frozenset({resolution_key}),
        )

    now = [100.0]
    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    monkeypatch.setattr(remediator.time, "time", lambda: now[0])
    run_remediator_pass(_settings())

    decisions.clear()
    now[0] = 1334.0
    run_remediator_pass(_settings())
    now[0] = 1400.0
    run_remediator_pass(_settings())

    samples = _remediation_samples("test-playbook:clears")
    assert [sample.status for sample in samples] == ["down", "up"]
    assert samples[-1].error_type == "condition cleared after 1234s"
    assert len([event for event in sentry_events if "test-playbook" in event]) == 1


def test_recurrence_after_resolution_records_fresh_onset_in_same_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "recurs", "present again", page=False)
    decisions = [decision]
    resolution_key = "test-playbook:recurs"

    def detector(settings: Settings) -> DetectionResult:
        return DetectionResult(
            tuple(decisions),
            frozenset() if decisions else frozenset({resolution_key}),
        )

    now = [100.0]
    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    monkeypatch.setattr(remediator.time, "time", lambda: now[0])
    run_remediator_pass(_settings())

    decisions.clear()
    now[0] = 200.0
    run_remediator_pass(_settings())
    decisions.append(decision)
    now[0] = 300.0
    run_remediator_pass(_settings())

    samples = _remediation_samples("test-playbook:recurs")
    assert [sample.status for sample in samples] == ["down", "up", "down"]


def test_fresh_process_does_not_resolve_absent_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def detector(settings: Settings) -> DetectionResult:
        return DetectionResult((), frozenset({"test-playbook:never-seen"}))

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    _record(
        SyntheticProbeSample(
            id="syn_rem_previous_process",
            probe_type=remediator.REMEDIATION_PROBE,
            target="test-playbook:never-seen",
            target_url="",
            monitor_region="test-1",
            status="down",
            error_type="recorded by a previous process",
        )
    )

    assert run_remediator_pass(_settings()) == []
    assert [sample.status for sample in _remediation_samples("test-playbook:never-seen")] == [
        "down"
    ]


def test_only_explicit_positive_evidence_resolves_active_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "unknown", "present", page=False)
    resolution_key = "test-playbook:unknown"
    result = [DetectionResult((decision,))]

    def detector(settings: Settings) -> DetectionResult:
        return result[0]

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    run_remediator_pass(_settings())

    result[0] = DetectionResult(())
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples("test-playbook:unknown")] == [
        "down"
    ]

    result[0] = DetectionResult((), frozenset({resolution_key}))
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples("test-playbook:unknown")] == [
        "down",
        "up",
    ]


def test_detector_exception_does_not_resolve_or_forget_active_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "raises", "present", page=False)
    mode = ["present"]
    resolution_key = "test-playbook:raises"

    def detector(settings: Settings) -> DetectionResult:
        if mode[0] == "raises":
            raise RuntimeError("detector exploded")
        return DetectionResult(
            (decision,) if mode[0] == "present" else (),
            frozenset({resolution_key}) if mode[0] == "clear" else frozenset(),
        )

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    run_remediator_pass(_settings())

    mode[0] = "raises"
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples("test-playbook:raises")] == [
        "down"
    ]

    mode[0] = "clear"
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples("test-playbook:raises")] == [
        "down",
        "up",
    ]


def test_detectors_with_same_target_do_not_resolve_while_one_still_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "shared", "present", page=False)
    first_present = [True]
    second_present = [True]

    def first(settings: Settings) -> DetectionResult:
        return DetectionResult(
            (decision,) if first_present[0] else (),
            frozenset() if first_present[0] else frozenset({"test-playbook:shared"}),
        )

    def second(settings: Settings) -> DetectionResult:
        return DetectionResult(
            (decision,) if second_present[0] else (),
            frozenset() if second_present[0] else frozenset({"test-playbook:shared"}),
        )

    monkeypatch.setattr(remediator, "DETECTORS", (first, second))
    run_remediator_pass(_settings())

    first_present[0] = False
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples("test-playbook:shared")] == [
        "down"
    ]

    second_present[0] = False
    run_remediator_pass(_settings())
    assert [sample.status for sample in _remediation_samples("test-playbook:shared")] == [
        "down",
        "up",
    ]


def test_overlapping_passes_record_exactly_one_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "overlap", "present", page=False)
    present = [True]
    clear_barrier: list[threading.Barrier] = []
    resolution_key = "test-playbook:overlap"

    def detector(settings: Settings) -> DetectionResult:
        if clear_barrier:
            clear_barrier[0].wait(timeout=5)
        return DetectionResult(
            (decision,) if present[0] else (),
            frozenset() if present[0] else frozenset({resolution_key}),
        )

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    run_remediator_pass(_settings())

    present[0] = False
    clear_barrier.append(threading.Barrier(2))
    errors: list[BaseException] = []

    def run() -> None:
        try:
            run_remediator_pass(_settings())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert [sample.status for sample in _remediation_samples("test-playbook:overlap")] == [
        "down",
        "up",
    ]


def test_recurrence_while_resolution_in_flight_records_onset_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "clear-recur", "present", page=False)
    mode = ["present"]
    resolution_key = "test-playbook:clear-recur"

    def detector(settings: Settings) -> DetectionResult:
        if mode[0] == "present":
            return DetectionResult((decision,))
        if mode[0] == "healthy":
            return DetectionResult((), frozenset({resolution_key}))
        return DetectionResult((decision,))

    original = InMemoryStore.record_synthetic_probe_sample
    resolution_started = threading.Event()
    release_resolution = threading.Event()

    def block_resolution(
        self: InMemoryStore,
        sample: SyntheticProbeSample,
    ) -> None:
        if sample.status == "up":
            resolution_started.set()
            assert release_resolution.wait(timeout=5)
        original(self, sample)

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    monkeypatch.setattr(InMemoryStore, "record_synthetic_probe_sample", block_resolution)
    run_remediator_pass(_settings())

    mode[0] = "healthy"
    errors: list[BaseException] = []

    def clear() -> None:
        try:
            run_remediator_pass(_settings())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=clear)
    thread.start()
    assert resolution_started.wait(timeout=5)

    mode[0] = "recurred"
    run_remediator_pass(_settings())
    release_resolution.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert [sample.status for sample in _remediation_samples(resolution_key)] == [
        "down",
        "up",
        "down",
    ]


def test_resolution_recording_failure_retries_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "record-fails", "present", page=False)
    decisions = [decision]
    resolution_key = "test-playbook:record-fails"

    def detector(settings: Settings) -> DetectionResult:
        return DetectionResult(
            tuple(decisions),
            frozenset() if decisions else frozenset({resolution_key}),
        )

    original = InMemoryStore.record_synthetic_probe_sample
    attempted_statuses: list[str] = []
    attempted_resolution_ids: list[str] = []
    fail_next_resolution = [True]

    def fail_resolution(
        self: InMemoryStore,
        sample: SyntheticProbeSample,
    ) -> None:
        attempted_statuses.append(sample.status)
        if sample.status == "up":
            attempted_resolution_ids.append(sample.id)
        if sample.status == "up" and fail_next_resolution[0]:
            fail_next_resolution[0] = False
            raise RuntimeError("resolution store unavailable")
        original(self, sample)

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    monkeypatch.setattr(InMemoryStore, "record_synthetic_probe_sample", fail_resolution)
    run_remediator_pass(_settings())

    decisions.clear()
    assert run_remediator_pass(_settings()) == []
    assert run_remediator_pass(_settings()) == []
    assert attempted_statuses == ["down", "up", "up"]
    assert len(attempted_resolution_ids) == 2
    assert len(set(attempted_resolution_ids)) == 1
    assert [sample.status for sample in _remediation_samples("test-playbook:record-fails")] == [
        "down",
        "up",
    ]


def test_failed_onset_is_never_followed_by_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "onset-fails", "present", page=False)
    decisions = [decision]
    resolution_key = "test-playbook:onset-fails"

    def detector(settings: Settings) -> DetectionResult:
        return DetectionResult(
            tuple(decisions),
            frozenset() if decisions else frozenset({resolution_key}),
        )

    original = InMemoryStore.record_synthetic_probe_sample
    attempted_statuses: list[str] = []

    def fail_onset(
        self: InMemoryStore,
        sample: SyntheticProbeSample,
    ) -> None:
        attempted_statuses.append(sample.status)
        if sample.status == "down":
            raise RuntimeError("onset store unavailable")
        original(self, sample)

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    monkeypatch.setattr(InMemoryStore, "record_synthetic_probe_sample", fail_onset)
    run_remediator_pass(_settings())

    decisions.clear()
    run_remediator_pass(_settings())

    assert attempted_statuses == ["down"]
    assert _remediation_samples(resolution_key) == []


def test_resolution_retry_and_pending_state_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "always-fails", "present", page=False)
    decisions = [decision]
    resolution_key = "test-playbook:always-fails"

    def detector(settings: Settings) -> DetectionResult:
        return DetectionResult(
            tuple(decisions),
            frozenset() if decisions else frozenset({resolution_key}),
        )

    original = InMemoryStore.record_synthetic_probe_sample
    attempted_resolution_ids: list[str] = []

    def fail_resolution(
        self: InMemoryStore,
        sample: SyntheticProbeSample,
    ) -> None:
        if sample.status == "up":
            attempted_resolution_ids.append(sample.id)
            raise RuntimeError("resolution store permanently unavailable")
        original(self, sample)

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    monkeypatch.setattr(InMemoryStore, "record_synthetic_probe_sample", fail_resolution)
    run_remediator_pass(_settings())

    decisions.clear()
    for _ in range(remediator.MAX_RESOLUTION_ATTEMPTS + 5):
        run_remediator_pass(_settings())

    assert len(attempted_resolution_ids) == remediator.MAX_RESOLUTION_ATTEMPTS
    assert len(set(attempted_resolution_ids)) == 1
    assert remediator._ACTIVE_CONDITIONS == {}
    assert [sample.status for sample in _remediation_samples(resolution_key)] == ["down"]


def test_broken_detector_never_kills_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def broken(settings: Settings) -> DetectionResult:
        raise RuntimeError("detector exploded")

    def healthy(settings: Settings) -> DetectionResult:
        calls.append("healthy")
        return DetectionResult(())

    monkeypatch.setattr(remediator, "DETECTORS", (broken, healthy))
    assert run_remediator_pass(_settings()) == []
    assert calls == ["healthy"]


@pytest.mark.parametrize("status", ["down", "up"])
def test_remediation_samples_stay_out_of_public_surfaces(status: str) -> None:
    from trusted_router.synthetic.components import (
        OPS_PROBE_TYPES,
        sample_component_ids,
        sample_slo_class_ids,
    )

    assert remediator.REMEDIATION_PROBE in OPS_PROBE_TYPES
    sample = SyntheticProbeSample(
        id="syn_rem_scope",
        probe_type="remediation",
        target="route-quarantine:x/y",
        target_url="",
        monitor_region="test-1",
        status=status,
    )
    assert sample_component_ids(sample) == []
    assert sample_slo_class_ids(sample) == []


def test_fleet_snapshot_carries_remediator_section() -> None:
    import asyncio

    fleet.register_heartbeat_target("scheduler:fleet-dead")
    _record(
        SyntheticProbeSample(
            id="syn_hb_dead_fleet_rem",
            probe_type="heartbeat",
            target="scheduler:fleet-dead",
            target_url="",
            monitor_region="test-1",
            status="up",
            created_at=_old_iso(45),
        )
    )
    run_remediator_pass(_settings())
    snapshot = asyncio.run(fleet.fleet_snapshot(_settings()))
    assert snapshot["remediator"]["mode"] == "observe"
    assert any(
        "heartbeat-stale:scheduler:fleet-dead" == row["decision"]
        for row in snapshot["remediator"]["decisions"]
    )
