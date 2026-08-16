"""Tests for the standing remediator (command-center Increment 3).

Observe-mode contract: detectors run, decisions are recorded as remediation
samples and page-worthy ones alert — and nothing else happens. A broken
detector loses its own signal, never the pass.
"""

from __future__ import annotations

import datetime as dt

import pytest

from trusted_router.config import Settings
from trusted_router.storage import STORE
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


def test_remediation_samples_stay_out_of_public_surfaces() -> None:
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
        status="down",
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
