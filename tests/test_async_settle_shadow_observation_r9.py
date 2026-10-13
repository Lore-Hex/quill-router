"""Actual claim admission and advancing-clock final closure regressions."""
import asyncio
import threading
from contextlib import contextmanager

import pytest

from tests.shadow_observation_diagnostics import client_history, status
from tests.shadow_observation_fixtures import observation_service
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    fake_store,
    service,
)
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.services import async_settle_shadow_health as shell


@pytest.mark.parametrize("revoke", [True, False])
@pytest.mark.asyncio
async def test_admission_after_scope_sample(monkeypatch, revoke):
    svc = service()
    svc.loop = asyncio.get_running_loop()
    wall, mono, claims = [NOW_US], [100.], []
    svc.store.wall, svc.store.clock = lambda: wall[0]/1e6, lambda: mono[0]
    svc.tick(wall[0], publisher=True)
    entered, release = asyncio.Event(), threading.Event()
    scope = shell.rpc_scope
    @contextmanager
    def held(*args):
        with scope(*args):
            svc.loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "pre-admission scope was never released"
            yield
    monkeypatch.setattr(shell, "rpc_scope", held)
    claim = svc.store.claim
    def record(*args):
        claims.append(mono[0])
        return claim(*args)
    monkeypatch.setattr(svc.store, "claim", record)
    svc.submit_publisher(NOW_US//m.SLOT_US, NOW_US)
    try:
        await entered.wait()
        if revoke:
            svc.settings.async_settle_shadow_policy = ""
            assert not svc.enabled()
        else:
            wall[0], mono[0] = NOW_US+200_000, 100.2
    finally:
        release.set()
    await svc.publisher_job
    assert claims == ([] if revoke else [100.2]), status(svc)
    if not revoke:
        assert svc.state.last_actual_start == 100.2, status(svc)
        monkeypatch.setattr(shell, "rpc_scope", scope)
        wall[0], mono[0] = NOW_US+m.SLOT_US, 102.
        svc.tick(wall[0], publisher=True)
        svc.submit_publisher(wall[0]//m.SLOT_US, wall[0])
        await svc.publisher_job
        assert claims == [100.2], status(svc)


@pytest.mark.asyncio
async def test_revocation_after_admission_and_during_io(monkeypatch):
    svc = service()
    svc.loop = asyncio.get_running_loop()
    svc.store.wall, svc.store.clock = lambda: NOW_US/1e6, lambda: 100.
    svc.tick(NOW_US, publisher=True)
    released, continue_claim, inside, finish = asyncio.Event(), threading.Event(), asyncio.Event(), threading.Event()
    lock = svc.admission_lock
    class Gate:
        def __enter__(self):
            return lock.__enter__()
        def __exit__(self, *args):
            lock.__exit__(*args)
            if threading.get_ident() != svc.owner_thread:
                svc.loop.call_soon_threadsafe(released.set)
                assert continue_claim.wait(5), "post-admission gate not released"
    svc.admission_lock = Gate()
    claim = svc.store.claim
    def held(*args):
        svc.loop.call_soon_threadsafe(inside.set)
        assert finish.wait(5), "held store I/O not released"
        return claim(*args)
    monkeypatch.setattr(svc.store, "claim", held)
    svc.submit_publisher(NOW_US//m.SLOT_US, NOW_US)
    try:
        await released.wait()
        svc.settings.async_settle_shadow_policy = ""
        assert not svc.enabled()  # Linearized after admission: this claim is allowed.
        continue_claim.set()
        await inside.wait()
        svc.settings.async_settle_shadow_policy = "shadow-v2"
        assert svc.enabled()  # Returns while I/O is still held, without a timing threshold.
        assert not finish.is_set()
    finally:
        continue_claim.set()
        finish.set()
    await svc.publisher_job
    assert svc.state.days[0].attempted == 1, status(svc)


def test_new_job_uses_current_generation():
    state, _ = m.step(m.State(), m.PolicyChanged(0, 7))
    state, _ = m.step(state, m.Tick(NOW_US, 100.))
    assert state.job.generation == 7
    state, _ = m.step(state, m.SlotOutcome(NOW_US, NOW_US//m.SLOT_US, "missed", generation=7))
    assert state.job is None


def test_advancing_wall_final_close():
    store, _ = fake_store()
    wall = [NOW_US]
    def advancing():
        wall[0] += 1_000
        return wall[0]/1e6
    store.wall = advancing
    svc = observation_service(store)
    svc.closing_reason, svc.termination_deadline = "shutdown", store.clock()+9
    with client_history(store) as history:
        svc.flush(store.clock()+4)
        assert not svc.journal.days, status(svc, history)
        assert svc.last_closure_ack is not None, status(svc, history)


@pytest.mark.asyncio
async def test_admission_samples_inside_lock(monkeypatch):
    svc = service()
    svc.loop = asyncio.get_running_loop()
    mono, wall = [100.], [NOW_US]
    svc.store.clock, svc.store.wall = lambda: mono[0], lambda: wall[0]/1e6
    svc.tick(NOW_US, publisher=True)
    lock = svc.admission_lock
    entered, release = asyncio.Event(), threading.Event()
    class Gate:
        def __enter__(self):
            if threading.get_ident() != svc.owner_thread:
                svc.loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
            return lock.__enter__()
        def __exit__(self, *args):
            return lock.__exit__(*args)
    svc.admission_lock = Gate()
    svc.submit_publisher(NOW_US//m.SLOT_US, NOW_US)
    try:
        await entered.wait()
        mono[0], wall[0] = 100.2, NOW_US+200_000
    finally:
        release.set()
    await svc.publisher_job
    assert svc.state.last_actual_start == 100.2, status(svc)


def test_owner_generation_writes_hold_admission_lock(monkeypatch):
    original = shell.Service.__setattr__
    writes = []
    def checked(svc, name, value):
        if name == "generation" and hasattr(svc, "generation"):
            writes.append(value)
            assert svc.admission_lock._is_owned(), "generation changed outside admission lock"
        original(svc, name, value)
    svc = service()
    monkeypatch.setattr(shell.Service, "__setattr__", checked)
    svc.settings.async_settle_shadow_policy = ""
    assert not svc.enabled() and writes
    svc.tick(NOW_US+100_000_000, publisher=False)
    svc.accept(m.ShutdownRequested(NOW_US+100_000_000))


def test_native_midnight_clock_mismatch_is_diagnostic():
    store, _ = fake_store()
    now = [100.]
    store.clock, store.wall = lambda: now[0], lambda: NOW_US/1e6
    svc = observation_service(store)
    now[0] += .01  # Exactly the native observer's advance against frozen midnight.
    svc.closing_reason = "shutdown"
    with client_history(store) as history:
        svc.flush(store.clock()+4)
        assert svc.journal.days and not svc.final_plans, status(svc, history)
        assert "2026-10-11" in svc.observer.daily_counts, status(svc, history)
        assert svc.journal.days["2026-10-12"].losses, status(svc, history)


def test_reachable_full_queue_fixture():
    from tests.shadow_observation_fixtures import saturate
    store, _ = fake_store()
    now = [520.]
    store.clock, store.wall = lambda: now[0], lambda: NOW_US/1e6+now[0]
    svc = observation_service(store)
    now[0] = 1000.
    stamp = NOW_US+1000_000_000
    rows = saturate(svc, stamp)
    assert len(rows) == 32 and svc.journal.work_bytes == 256*1024, status(svc)
    assert len(svc.bases.queue) == len(svc.traces.queue) == len(svc.journal.pages) == 4, status(svc)
    assert len(svc.state.days[0].observers) == 32 and svc.state.job is not None, status(svc)
    from trusted_router.storage_gcp_shadow_observation import Budget
    day = "2026-10-12"
    pages = svc.journal.freeze(day)
    svc.journal.checkpoint(day)
    counter = svc.counter_row(day, stamp, closed=False)
    budget = Budget(svc.store.clock()+4, svc.store.clock)
    svc.store.flush(pages, counter, None, budget)
    svc.store.retain((rows[0],), budget)
    assert all(svc.store.read(row.kind, row.key, budget) == row for row in [*pages, counter, rows[0]])
    assert all(svc.store.read(row.kind, row.key, budget) is None for row in rows[1:])
    assert counter.value()["durable_event_hwm"] == 0 and counter.value()["loss_count"] > 0



def test_request_policy_clear_never_takes_admission_lock():
    svc = service()
    svc.journal.grant("2026-10-12", 100)
    class Forbidden:
        def __enter__(self):
            raise AssertionError("request waited for admission")
        def __exit__(self, *args):
            pass
    svc.admission_lock = Forbidden()
    svc.settings.async_settle_shadow_policy = ""
    assert svc.port.reserve(NOW_US) is None
    assert svc.port.lost.is_set()


@pytest.mark.asyncio
async def test_production_close_with_advancing_wall():
    store, _ = fake_store()
    wall = [NOW_US+100_000_000]
    def advancing():
        wall[0] += 1000
        return wall[0]/1e6
    store.wall = advancing
    svc = observation_service(store)
    with client_history(store) as history:
        await svc.close()
        assert svc.close_finished and not svc.unclosed and not svc.journal.days, status(svc, history)
        assert svc.interval_end_us == svc.state.shutdown_stamp, status(svc, history)
