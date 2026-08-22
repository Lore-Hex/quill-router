"""Tests for the standing remediator (command-center Increment 3).

Observe-mode contract: detectors run, decisions are recorded as remediation
samples and page-worthy ones alert — and nothing else happens. A broken
detector loses its own signal, never the pass.
"""

from __future__ import annotations

import datetime as dt

import pytest

from trusted_router.config import Settings
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import SyntheticProbeSample, utcnow
from trusted_router.synthetic import fleet, remediator
from trusted_router.synthetic.remediator import (
    Decision,
    recent_decisions,
    run_remediator_pass,
)


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

    def detector(settings: Settings) -> list[Decision]:
        return [decision]

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

    def detector(settings: Settings) -> list[Decision]:
        return decisions

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

    def detector(settings: Settings) -> list[Decision]:
        return decisions

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
    def detector(settings: Settings) -> list[Decision]:
        return []

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


def test_resolution_recording_failure_does_not_break_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision("test-playbook", "record-fails", "present", page=False)
    decisions = [decision]

    def detector(settings: Settings) -> list[Decision]:
        return decisions

    original = InMemoryStore.record_synthetic_probe_sample
    attempted_statuses: list[str] = []

    def fail_resolution(
        self: InMemoryStore,
        sample: SyntheticProbeSample,
    ) -> None:
        attempted_statuses.append(sample.status)
        if sample.status == "up":
            raise RuntimeError("resolution store unavailable")
        original(self, sample)

    monkeypatch.setattr(remediator, "DETECTORS", (detector,))
    monkeypatch.setattr(InMemoryStore, "record_synthetic_probe_sample", fail_resolution)
    run_remediator_pass(_settings())

    decisions.clear()
    assert run_remediator_pass(_settings()) == []
    assert run_remediator_pass(_settings()) == []

    decisions.append(decision)
    run_remediator_pass(_settings())
    assert attempted_statuses == ["down", "up", "down"]
    assert [sample.status for sample in _remediation_samples("test-playbook:record-fails")] == [
        "down",
        "down",
    ]


def test_broken_detector_never_kills_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def broken(settings: Settings) -> list[Decision]:
        raise RuntimeError("detector exploded")

    def healthy(settings: Settings) -> list[Decision]:
        calls.append("healthy")
        return []

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
