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
        assert first_reply.done()
        worker = svc.publisher_worker
        assert not worker.done()
        next_job = next_cycle(svc, wall, mono)
        # Still-running I/O cannot fill an unbounded executor queue.
        svc.submit_publisher(next_job.slot, wall[0])
        assert "publisher_worker_busy" in caplog.text
        assert svc.state.job is None and svc.publisher_worker is worker
        assert entries(svc)[-1]["outcome"] == "failed"
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
