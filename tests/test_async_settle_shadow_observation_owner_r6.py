"""A few event-synchronised shell checks; lifecycle permutations live separately."""
from __future__ import annotations

import asyncio
import threading

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (  # noqa: F401
    NOW_US,
    deterministic_scheduler,
    service,
)
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.async_settle_shadow_journal import RESERVATION_BLOCK


@pytest.mark.asyncio
async def test_state_is_never_touched_off_owner_thread(monkeypatch):
    svc = service()
    origin = threading.get_ident()
    loop = asyncio.get_running_loop()
    accepted = []
    worker_errors = []
    original = svc.accept
    def accept(event):
        assert threading.get_ident() == origin
        accepted.append(event)
        return original(event)
    monkeypatch.setattr(svc, "accept", accept)
    entered = asyncio.Event()
    release = threading.Event()
    flush = svc.store.flush
    def work(*args):
        assert threading.get_ident() != origin
        try:
            with pytest.raises(RuntimeError, match="off_owner"):
                _ = svc.state
            with pytest.raises(RuntimeError, match="off_owner"):
                original(m.ObserverEnqueued(NOW_US, "worker-admission"))
        except BaseException as error:
            worker_errors.append(error)
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return flush(*args)
    monkeypatch.setattr(svc.store, "flush", work)
    job = asyncio.create_task(svc.drive_async(svc._flush(svc.store.clock()+4)))
    signal = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait({signal, job}, return_when=asyncio.FIRST_COMPLETED)
        assert signal in done, "worker failed before delivering its I/O rendezvous"
        assert svc.state.days and not job.done()
        svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
        assert svc.port.reserve(NOW_US) is not None
    finally:
        release.set()
        signal.cancel()
        await asyncio.gather(signal, return_exceptions=True)
        await job
    assert not worker_errors, worker_errors
    assert any(isinstance(e, m.FlushReply) for e in accepted)


@pytest.mark.asyncio
async def test_publisher_worker_delivers_old_day_outcome_to_loop():
    svc = service()
    svc.loop = asyncio.get_running_loop()
    stamp = [NOW_US+86_398_000_000]
    svc.store.wall = lambda: stamp[0]/1e6
    svc.schedule_cycle(stamp[0]//m.SLOT_US, stamp[0])
    old = stamp[0]
    entered = asyncio.Event()
    release = threading.Event()
    def block():
        svc.loop.call_soon_threadsafe(entered.set)
        release.wait()
    blocker = svc.publisher_executor.submit(block)
    try:
        await entered.wait()
        svc.submit_publisher(old//m.SLOT_US, old)
        stamp[0] += 2_500_000
        svc.tick(stamp[0])
        assert svc.state.days[0].publishers == {old//m.SLOT_US}
        assert not svc.state.days[0].final
    finally:
        release.set()
    await svc.publisher_job
    blocker.result()
    assert svc.state.days[0].missed == 1
    assert not svc.state.days[0].publishers
    assert svc.counters["2026-10-12"]["publisher"]["missed_slots"] == 1


@pytest.mark.asyncio
async def test_observer_worker_only_returns_data(monkeypatch):
    svc = service()
    svc.loop = asyncio.get_running_loop()
    svc.observer.evidence_wall = svc.store.wall
    completed = asyncio.Event()
    original = svc.observer.finish
    def finish(*args):
        assert threading.get_ident() == svc.owner_thread
        original(*args)
        completed.set()
    monkeypatch.setattr(svc.observer, "finish", finish)
    monkeypatch.setattr(svc.observer, "health_read", lambda: None)
    with svc.observer.lock:
        svc.observer._count("health_reads")
        started = svc.observer.clock()
        svc.observer.inflight_ticks[None] = started
    assert svc.state.days[0].observers
    job = svc.publisher_executor.submit(svc.observer._health, started)
    await completed.wait()
    job.result()
    assert not svc.state.days[0].observers
    assert svc.counters["2026-10-12"]["admission_observer"]["health_reads"] == 1


def test_midnight_shell_accounts_gap_before_counter_closure():
    from tests.test_async_settle_shadow_observation_a1 import MemoryAttachments
    from trusted_router.async_settle_shadow_journal import CLOSE_RESULT_KIND, COUNTER_KIND
    from trusted_router.storage_gcp_shadow_observation import Budget
    svc = service()
    stamp = [NOW_US+86_396_000_000]
    svc.store.wall = svc.observer.evidence_wall = lambda: stamp[0]/1e6
    svc.observer.clock = lambda: 0.
    svc.observer.daily_since = 0.
    svc.traces.transport = MemoryAttachments()
    svc.publish_cycle(stamp[0]//m.SLOT_US, stamp[0])
    svc.drive(svc.upload(svc.traces, Budget(svc.store.clock()+4, svc.store.clock)))
    svc.traces.next_operation = float("-inf")
    stamp[0] += 4_500_000
    svc.flush(svc.store.clock()+4)
    key = "2026-10-12/"+svc.journal.boot
    row = svc.store.read(COUNTER_KIND, key, Budget(svc.store.clock()+1, svc.store.clock))
    assert row.value()["closed"]
    p = row.value()["publisher"]
    assert p["scheduled_slots"] == 2 and p["missed_slots"] == 1 and p["claim_attempts"] == 1
    assert svc.store.read(CLOSE_RESULT_KIND, key, Budget(svc.store.clock()+1, svc.store.clock))
    before = svc.state
    effects = svc.tick(stamp[0]-3_000_000)
    assert svc.state == before and any(isinstance(e, m.Fault) for e in effects)
    svc.tick(stamp[0]+1)


@pytest.mark.asyncio
async def test_start_dispatches_first_publisher_before_observer(monkeypatch):
    svc = service()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    svc.store.wall = lambda: NOW_US/1e6
    from trusted_router.services import async_settle_shadow_health as shell
    # Preparation is asynchronous. Preserve the actual owner loop and prove it
    # dispatches before starting the observer, without advancing the fake clock.
    monkeypatch.setattr(shell, "asyncio", asyncio)
    observed = []
    def observer_start():
        assert svc.publisher_job is not None
        assert svc.runtime_stop.available
        observed.append(True)
    monkeypatch.setattr(svc.observer, "start", observer_start)
    svc.start()
    try:
        async with asyncio.timeout(3.):
            while svc.publisher_job is None:
                await asyncio.sleep(.005)
            await svc.publisher_job
        assert observed
        assert not svc.state.days[0].publishers
        assert svc.state.days[0].attempted == 1
    finally:
        await svc.close()
        # close() never joins the owner's workers (native I/O must not spend
        # the evidence grace); the test does, so an idle worker still exiting
        # on a loaded runner is not counted as leaked.
        for executor in (svc.publisher_executor, svc.evidence_executor, svc.session_executor):
            executor.shutdown(wait=True)



def test_final_flag_and_transition_share_one_port_try_lock(monkeypatch):
    svc = service()
    stamp = int(svc.store.wall()*1_000_000)
    svc.tick(stamp)
    svc.fold(stamp)
    svc.accept(m.ShutdownRequested(stamp))
    day = "2026-10-12"
    svc.journal.grant(day, RESERVATION_BLOCK)
    svc.journal.days[day].closing = True
    svc.closing_reason, svc.interval_end_us = "shutdown", stamp
    svc.termination_deadline = svc.store.clock()+9
    entered, release = threading.Event(), threading.Event()
    original = m.step
    def step(state, event):
        result = original(state, event)
        if any(isinstance(e, m.StartFinalClose) for e in result[1]):
            entered.set()
            release.wait()
        return result
    monkeypatch.setattr(m, "step", step)
    outcomes = []
    def request():
        entered.wait()
        try:
            # The state machine has decided, but the flag assignment has not
            # executed. This must try and refuse the already-held journal lock.
            outcomes.append(svc.port.capture_prediction("ws", stamp))
        finally:
            release.set()
    job = svc.publisher_executor.submit(request)
    try:
        svc.accept(m.FlushReply(stamp, day, True, True))
    finally:
        release.set()
    job.result()
    assert outcomes == [None] and svc.port.lost.is_set()
    assert svc.journal.days[day].sealing
    assert svc.port.reserve(NOW_US) is None

    # The frozen payload cannot be revised by the refused late request's sticky
    # loss or by another observer fold before the actual transaction starts.
    frozen = svc.final_plans[day][1]
    svc.flush(svc.store.clock()+4)
    from trusted_router.storage_gcp_shadow_observation import Budget
    landed = svc.store.read(frozen.kind, frozen.key, Budget(svc.store.clock()+1, svc.store.clock))
    assert landed == frozen and landed.value()["closed"]
