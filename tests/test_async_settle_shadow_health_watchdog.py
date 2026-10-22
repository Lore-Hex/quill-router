"""Publisher progress through the real owner and lifecycle with fake storage."""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    service,
)
from tests.test_async_settle_shadow_observation_r7 import entries, missed
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.async_settle_shadow_v2_a1_health import (
    PUBLISHER_SLOT_TRACE,
    validate_publisher_final,
)
from trusted_router.services import async_settle_shadow_health as shell


def owner(stamp=NOW_US):
    svc = service()
    wall, mono = [stamp], [100.]
    svc.store.wall, svc.store.clock = lambda: wall[0]/1e6, lambda: mono[0]
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    effects = svc.tick(wall[0])
    job = next(e.job for e in effects if isinstance(e, m.StartPublisherCycle))
    return svc, wall, mono, job


def accounting(svc):
    for predicate in (m.accounted_once, m.lifetime_coverage, m.conservation,
                      m.final_barrier, m.no_post_final_owners, m.never_reopens):
        assert predicate(svc.state), predicate.__name__
    for day in svc.state.days:
        assert day.scheduled == day.missed+day.attempted
        assert day.attempted == day.failed+day.published+day.lost
        value = dict(svc.counters[day.day]["publisher"])
        # Retained counters include the outstanding opportunity; final totals do not.
        assert value["scheduled_slots"] == day.scheduled+len(day.publishers)
        value["scheduled_slots"] -= len(day.publishers)
        validate_publisher_final(value)


def failed(svc, job):
    assert svc.state.job is None
    assert all(job.slot not in d.publishers for d in svc.state.days)
    trace = next(e for e in entries(svc) if e["slot"] == job.slot)
    PUBLISHER_SLOT_TRACE(trace)
    assert trace["outcome"] == "failed" and trace["unknown_outcome"] is True
    assert trace["rpc_count"] == 0
    day = next(d for d in svc.state.days if d.day == trace["day"])
    assert day.failed == 1 and day.attempted == 1
    assert svc.counters[day.day]["publisher"]["unknown_outcomes"] == 1
    assert not svc.publishing
    accounting(svc)


def next_cycle(svc, wall, mono, *, offset=4_000_000):
    wall[0], mono[0] = NOW_US+offset, 100.+offset/1e6
    svc.store.database.server_ns = wall[0]*1000
    effects = svc.tick(wall[0])
    job = next(e.job for e in effects if isinstance(e, m.StartPublisherCycle))
    accounting(svc)
    return job


@pytest.mark.parametrize("reason", ["unknown_slot", "invalid_start", "gone_day", "frozen_day"])
def test_rejected_current_reply_releases_and_schedules(reason):
    svc, wall, mono, job = owner(NOW_US+m.DAY_US-m.SLOT_US if reason == "frozen_day" else NOW_US)
    svc.publishing.add(m.day_name(NOW_US))
    event = m.SlotOutcome(wall[0], job.slot, "published", generation=job.generation,
                          actual_start_mono=100.)
    if reason == "unknown_slot":
        event = replace(event, outcome="invalid")
    elif reason == "invalid_start":
        event = replace(event, actual_start_mono=101.)
    elif reason == "gone_day":
        # An outstanding job normally prevents retirement; exercise the guard
        # with the owner's day absent, without losing any completed accounting.
        svc._state = replace(svc.state, days=())
    else:
        svc.accept(m.ObserverEnqueued(NOW_US, "read"))
        svc.accept(m.ObserverFolded(NOW_US, m.day_name(NOW_US), "read", success=False))
    effects = svc.accept(event)
    assert m.Fault(reason) in effects
    failed(svc, job)
    assert entries(svc)[-1]["release_reason"] == reason
    next_job = next_cycle(svc, wall, mono, offset=m.DAY_US if reason == "frozen_day" else 4_000_000)
    before, traces = svc.state, entries(svc)
    assert any(isinstance(e, m.Fault) for e in svc.accept(event))
    assert svc.state == before and entries(svc) == traces and svc.state.job == next_job
    svc.submit_publisher(next_job.slot, wall[0])
    assert svc.state.job is None and entries(svc)[-1]["outcome"] == "published"
    accounting(svc)


@pytest.mark.parametrize("malformation", ["object", "trace", "day", "durations", "start"])
def test_invalid_reply_releases_and_schedules(malformation):
    svc, wall, mono, job = owner()
    reply = shell.PublisherReply(missed(svc, job.slot), (), False, job.generation)
    if malformation == "object":
        reply = object()
    elif malformation == "trace":
        reply = replace(reply, trace={})
    elif malformation == "day":
        reply = replace(reply, trace=dict(reply.trace, day="2026-10-13"))
    elif malformation == "durations":
        reply = replace(reply, rpc_durations=(-1,))
    else:
        reply = replace(reply, trace=dict(reply.trace, outcome="failed", claim_start_us=NOW_US, cycle_us=0),
                        actual_start_mono=101.)
    effects = svc.publisher_reply(reply)
    assert any(isinstance(e, m.Fault) for e in effects)
    failed(svc, job)
    assert entries(svc)[-1]["release_reason"] == (
        "invalid_start" if malformation == "start" else "invalid_publisher_reply")
    next_job = next_cycle(svc, wall, mono)
    before, traces = svc.state, entries(svc)
    assert svc.publisher_reply(shell.PublisherReply(missed(svc, job.slot), (), False)) == (
        m.Fault("invalid_publisher_reply"),)
    assert svc.state == before and entries(svc) == traces
    svc.submit_publisher(next_job.slot, wall[0])
    assert svc.state.job is None and entries(svc)[-1]["outcome"] == "published"
    accounting(svc)


@pytest.mark.asyncio
async def test_escaping_publish_exception_delivers_and_schedules(monkeypatch, caplog):
    svc, wall, mono, job = owner()
    svc.loop = asyncio.get_running_loop()
    original = shell.sha256
    def escaping(*args):
        # Trace construction is outside publish_cycle's inner try.
        raise RuntimeError("trace construction failed")
    monkeypatch.setattr(shell, "sha256", escaping)
    svc.submit_publisher(job.slot, wall[0])
    # A queue barrier proves execute exited, independent of reply delivery.
    await asyncio.wrap_future(svc.publisher_executor.submit(lambda: None))
    await asyncio.sleep(0)
    assert svc.publisher_job.done()
    failed(svc, job)
    assert "trace construction failed" in caplog.text
    assert isinstance(svc.publisher_worker.exception(), RuntimeError)
    monkeypatch.setattr(shell, "sha256", original)
    next_job = next_cycle(svc, wall, mono)
    svc.submit_publisher(next_job.slot, wall[0])
    await svc.publisher_job
    assert svc.state.job is None and entries(svc)[-1]["outcome"] == "published"
    accounting(svc)


@pytest.mark.asyncio
async def test_delivered_publisher_can_submit_before_worker_returns(monkeypatch, caplog):
    svc, wall, mono, job = owner()
    svc.loop = asyncio.get_running_loop()
    scheduled, release = asyncio.Event(), threading.Event()
    original = svc.loop.call_soon_threadsafe
    def held_delivery(callback, *args, **kwargs):
        handle = original(callback, *args, **kwargs)
        if getattr(callback, "__name__", "") == "deliver" and not release.is_set():
            original(scheduled.set)
            assert release.wait(10), "test did not release delivery"
        return handle
    monkeypatch.setattr(svc.loop, "call_soon_threadsafe", held_delivery)
    svc.submit_publisher(job.slot, wall[0])
    worker = svc.publisher_worker
    try:
        await asyncio.wait_for(scheduled.wait(), 5)
        await svc.publisher_job
        assert not worker.done()
        assert entries(svc)[-1]["outcome"] == "published"
        next_job = next_cycle(svc, wall, mono)
        svc.submit_publisher(next_job.slot, wall[0])
        assert "publisher_worker_busy" not in caplog.text
        assert svc.state.job == next_job
        assert svc.publisher_worker is not worker
    finally:
        release.set()
        await asyncio.wrap_future(worker)
    await asyncio.wait_for(svc.publisher_job, 5)
    assert svc.state.job is None
    assert [e["outcome"] for e in entries(svc)] == ["published", "missed", "published"]
    accounting(svc)


@pytest.mark.asyncio
async def test_watchdog_releases_never_delivered_reply_and_schedules(monkeypatch, caplog):
    svc, wall, mono, job = owner()
    svc.loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original = shell.publish_cycle
    def held(*args):
        svc.loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "test did not release publisher"
        return original(*args)
    monkeypatch.setattr(shell, "publish_cycle", held)
    svc.submit_publisher(job.slot, wall[0])
    first_reply = svc.publisher_job
    worker = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        deadline = job.latest_start_mono+shell.CYCLE_SECONDS+2.
        mono[0], wall[0] = deadline, NOW_US+2_750_000
        svc.tick(wall[0])
        assert svc.state.job == job and not entries(svc)
        mono[0] += .000001
        wall[0] += 1
        effects = svc.tick(wall[0])
        assert m.Fault("publisher_watchdog") in effects
        failed(svc, job)
        assert next(e for e in entries(svc) if e["slot"] == job.slot)["release_reason"] == "publisher_watchdog"
        assert first_reply.done()
        worker = svc.publisher_worker
        assert not worker.done()
        next_job = next_cycle(svc, wall, mono)
        # Still-running I/O cannot fill an unbounded executor queue.
        svc.submit_publisher(next_job.slot, wall[0])
        assert "publisher_worker_busy" in caplog.text
        assert svc.state.job is None and svc.publisher_worker is worker
        assert entries(svc)[-1]["outcome"] == "failed"
        assert entries(svc)[-1]["release_reason"] == "publisher_worker_busy"
        accounting(svc)
        next_job = next_cycle(svc, wall, mono, offset=6_000_000)
        before, traces = svc.state, entries(svc)
    finally:
        release.set()
        if worker is not None:
            await asyncio.wrap_future(worker)
        else:
            await asyncio.wrap_future(svc.publisher_executor.submit(lambda: None))
        await asyncio.sleep(0)
    assert svc.state == before and entries(svc) == traces
    monkeypatch.setattr(shell, "publish_cycle", original)
    wall[0], mono[0] = wall[0]+250_000, mono[0]+.25
    svc.store.database.server_ns = wall[0]*1000
    svc.submit_publisher(next_job.slot, wall[0])
    await svc.publisher_job
    assert svc.state.job is None and entries(svc)[-1]["outcome"] == "published"
    accounting(svc)


def test_release_signature_does_not_prove_worker_did_no_io():
    svc, wall, _, job = owner()
    reply = shell.publish_cycle(svc.store, svc.journal.boot,
        svc.settings.effective_serving_region, svc.settings.source_revision,
        job, wall[0], svc, incumbent=True)
    assert reply.trace["outcome"] == "published"
    assert svc.store.database.commits == 2  # Claim and publication already landed.
    assert reply.trace["claim_at_ns"] is not None and reply.trace["ack_at_us"] is not None
    # An invalid returned start releases the owner, erasing even a successful
    # publication's timings. The old zero-RPC signature cannot identify B.
    svc.publisher_reply(replace(reply, actual_start_mono=job.latest_start_mono+1))
    failed(svc, job)
    trace = entries(svc)[-1]
    assert trace["release_reason"] == "invalid_start"
    assert trace["claim_at_ns"] is trace["ack_at_us"] is None
    assert trace["cycle_us"] == trace["rpc_count"] == 0


def test_release_reason_is_optional_bounded_and_only_on_unknown_release():
    svc, _, _, job = owner()
    current = shell.failed_publisher_reply(svc.journal.boot, job, "publisher_worker_busy").trace
    historical = {k: v for k, v in current.items() if k != "release_reason"}
    for trace in (current, historical):
        assert PUBLISHER_SLOT_TRACE.parse(PUBLISHER_SLOT_TRACE.serialize(trace)) == trace
    for change in ({"release_reason": "x"*65}, {"release_reason": None},
                   {"unknown_outcome": False}, {"rpc_count": 1}, {"cycle_us": 1}):
        with pytest.raises(ValueError):
            PUBLISHER_SLOT_TRACE({**current, **change})


@pytest.mark.parametrize("change,rule", [
    ({"claim_at_ns": NOW_US*1000-1_000_000}, "a1-server-slot"),
    ({"observed_at_ns": NOW_US*1000+251_000_000,
      "publisher_heartbeat_ns": NOW_US*1000+251_000_000}, "trace_published_window"),
    ({"claim_at_ns": NOW_US*1000+1}, "trace_claim_observed_order"),
    ({"cycle_us": 12345.0}, "native-json-domain"),
    ({"rpc_elapsed_us": 1500.0}, "native-json-domain"),
    ({"cycle_us": 500001}, "trace_published_cycle_bound"),
    ({"ack_at_us": None}, "trace_published_fields"),
    ({"publisher_heartbeat_ns": NOW_US*1000+1}, "trace_heartbeat_equal"),
    ({"secret-extra-key": "secret-extra-value"}, "evidence_schema_gap"),
])
def test_rejected_contract_diagnostic_excludes_measurement_values(change, rule, caplog):
    svc, wall, _, job = owner()
    reply = shell.publish_cycle(svc.store, svc.journal.boot, svc.settings.effective_serving_region,
                                svc.settings.source_revision, job, wall[0], svc, True, True)
    assert reply.trace["outcome"] == "published"
    trace = dict(reply.trace, cycle_us=12345, rpc_count=2, rpc_elapsed_us=1500)
    trace.update(change)
    reply = replace(reply, trace=trace, rpc_durations=(700, 800))
    svc.publisher_reply(reply, job=job)
    diagnostic, = [r for r in caplog.records if r.msg.startswith("shadow_v2_publisher_reply_rejected")]
    message = diagnostic.getMessage()
    assert diagnostic.levelname == "WARNING"
    assert "group=contract exception_type=ValueError" in message
    assert f"exception_message={rule}" in message
    assert "outcome=published" in message
    assert "claimed_type=bool generation_type=int" in message
    assert "rpc_duration_element_types=['int', 'int']" in message
    for field, value in trace.items():
        if field in PUBLISHER_SLOT_TRACE.fields:
            assert f"'{field}': '{type(value).__name__}'" in message
    for field in ("rpc_count", "rpc_elapsed_us", "cycle_us"):
        assert f"'{field}': {trace[field]}" not in message
    for secret in (svc.journal.boot, str(job.slot), str(NOW_US), reply.trace["body_sha256"],
                   "secret-extra-key", "secret-extra-value", reply.trace["day"]):
        assert secret not in message
    failed(svc, job)
    released = entries(svc)[-1]
    assert released["release_reason"] == "invalid_publisher_reply"
    assert released["rpc_count"] == released["rpc_elapsed_us"] == released["cycle_us"] == 0


@pytest.mark.parametrize("field,value,group", [
    ("generation", True, "type"), ("claimed", 1, "type"),
    ("rpc_durations", (1, -1), "type"), ("rpc_durations", (1.0,), "type"),
    ("rpc_durations", [1], "type"), ("trace", None, "type"),
    ("day", "secret-day", "identity"),
])
def test_reply_diagnostic_groups(field, value, group, caplog):
    svc, _, _, job = owner()
    reply = shell.PublisherReply(missed(svc, job.slot), (), False, job.generation)
    reply = (replace(reply, trace=dict(reply.trace, day=value)) if field == "day"
             else replace(reply, **{field: value}))
    svc.publisher_reply(reply, job=job)
    diagnostic, = [r.getMessage() for r in caplog.records
                   if r.msg.startswith("shadow_v2_publisher_reply_rejected")]
    assert f"group={group}" in diagnostic
    assert "secret-day" not in diagnostic
    failed(svc, job)


@pytest.mark.parametrize("error,message", [
    (TypeError("secret-body"), "redacted_non_schema_message"),
    (KeyError("secret-body"), "redacted_non_schema_message"),
    (KeyError("ack_at_us"), "'ack_at_us'"),
])
def test_unexpected_validator_exception_cannot_log_payload(monkeypatch, caplog, error, message):
    svc, _, _, job = owner()
    def reject(value):
        raise error
    monkeypatch.setattr(shell, "PUBLISHER_SLOT_TRACE", type("Validator", (), {
        "fields": PUBLISHER_SLOT_TRACE.fields,
        "optional_fields": PUBLISHER_SLOT_TRACE.optional_fields,
        "__call__": staticmethod(reject),
    })())
    svc.publisher_reply(shell.PublisherReply(missed(svc, job.slot), (), False, job.generation), job=job)
    diagnostic, = [r.getMessage() for r in caplog.records
                   if r.msg.startswith("shadow_v2_publisher_reply_rejected")]
    assert f"exception_type={type(error).__name__} exception_message={message}" in diagnostic
    assert "secret-body" not in diagnostic
    failed(svc, job)


@pytest.mark.parametrize("float_field", ["cycle_us", "rpc_elapsed_us"])
def test_round_five_single_field_probes_have_eight_distinct_identifiers(float_field, caplog):
    probes = [
        ("claim_at_ns", NOW_US*1000-1_000_000, "a1-server-slot"),
        ("observed_at_ns", NOW_US*1000+251_000_000, "trace_published_window"),
        ("claim_at_ns", NOW_US*1000+1, "trace_claim_observed_order"),
        (float_field, 12345.0, "native-json-domain"),
        ("cycle_us", 500001, "trace_published_cycle_bound"),
        ("ack_at_us", None, "trace_published_fields"),
        ("publisher_heartbeat_ns", NOW_US*1000+1, "trace_heartbeat_equal"),
        ("secret-extra-key", "secret-extra-value", "evidence_schema_gap"),
    ]
    identifiers = []
    for field, value, expected in probes:
        svc, wall, _, job = owner()
        reply = shell.publish_cycle(svc.store, svc.journal.boot, svc.settings.effective_serving_region,
                                    svc.settings.source_revision, job, wall[0], svc, True, True)
        PUBLISHER_SLOT_TRACE(reply.trace)
        trace = {**reply.trace, field: value}
        assert sum(trace.get(k) != reply.trace.get(k) for k in trace) == 1
        with pytest.raises(ValueError) as rejection:
            PUBLISHER_SLOT_TRACE(trace)
        identifiers.append(str(rejection.value))
        assert identifiers[-1] == expected
        caplog.clear()
        svc.publisher_reply(replace(reply, trace=trace), job=job)
        diagnostic, = [r.getMessage() for r in caplog.records
                       if r.msg.startswith("shadow_v2_publisher_reply_rejected")]
        assert f"exception_message={expected} " in diagnostic
        failed(svc, job)
    assert len(identifiers) == len(set(identifiers)) == 8


@pytest.mark.parametrize("path", ["type", "identity", "stale", "contract"])
@pytest.mark.parametrize("measurement", ["rpc_count", "rpc_elapsed_us", "cycle_us"])
@pytest.mark.parametrize("payload", ["slot", "timestamp"])
def test_rejected_reply_never_logs_measurement_payload(path, measurement, payload, caplog):
    svc, _, _, job = owner()
    secret = job.slot if payload == "slot" else NOW_US*1000
    trace = {**missed(svc, job.slot), measurement: secret}
    reply = shell.PublisherReply(trace, (), False, job.generation)
    dispatch = job
    if path == "type":
        reply = replace(reply, claimed=1)
    elif path == "identity":
        reply = replace(reply, trace={**trace, "day": "secret-day"})
    elif path == "stale":
        dispatch = replace(job, generation=job.generation+1)
    svc.publisher_reply(reply, job=dispatch)
    diagnostic, = [r.getMessage() for r in caplog.records
                   if r.msg.startswith("shadow_v2_publisher_reply_rejected")]
    assert f"group={'identity' if path == 'stale' else path}" in diagnostic
    assert str(secret) not in diagnostic
    assert "measurements=" not in diagnostic
    assert f"'{measurement}': 'int'" in diagnostic
    if path == "stale":
        assert svc.state.job == job
        accounting(svc)
    else:
        failed(svc, job)


@pytest.mark.parametrize("reason", sorted(shell.PUBLISHER_RELEASE_REASONS) + ["a"*64, "private-slot-896313600"])
def test_release_log_uses_closed_vocabulary_but_keeps_operational_identity(reason, caplog):
    svc, _, _, job = owner()
    reply = shell.failed_publisher_reply(svc.journal.boot, job, reason)
    PUBLISHER_SLOT_TRACE(reply.trace)
    svc.publisher_reply(reply, job=job)
    diagnostic, = [r.getMessage() for r in caplog.records
                   if r.msg.startswith("shadow_v2_publisher_released")]
    expected = reason if reason in shell.PUBLISHER_RELEASE_REASONS else "other"
    assert diagnostic == (f"shadow_v2_publisher_released slot={job.slot} "
                          f"generation={job.generation} reason={expected}")
    assert entries(svc)[-1]["release_reason"] == reason
    failed(svc, job)


@pytest.mark.parametrize("outcome", ["missed", "lost_claim", "published", "failed",
                                     "private-outcome-896313600", 896313600, ["private-outcome"]])
def test_rejection_logs_only_literal_trace_outcomes(outcome, caplog):
    svc, _, _, job = owner()
    reply = shell.PublisherReply({**missed(svc, job.slot), "outcome": outcome}, (), 1, job.generation)
    svc.publisher_reply(reply, job=job)
    diagnostic, = [r.getMessage() for r in caplog.records
                   if r.msg.startswith("shadow_v2_publisher_reply_rejected")]
    expected = outcome if type(outcome) is str and outcome in (
        "missed", "lost_claim", "published", "failed") else "invalid_or_missing"
    assert f"outcome={expected} " in diagnostic
    assert "private-outcome" not in diagnostic and "896313600" not in diagnostic
    failed(svc, job)
