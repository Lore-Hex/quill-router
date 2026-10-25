"""Real worker/storage execution with serialized claim-row transactions."""
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from tests.shadow_health_cold_start_simulation import Clock, Database, modules, simulate
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    logical_time,
    service,
)
from trusted_router.async_settle_shadow_journal import CONTROL_KIND
from trusted_router.config import Settings
from trusted_router.storage_gcp_shadow_observation import ContentionLoss, UnknownCommit


@pytest.mark.parametrize("warm", [False, True])
def test_serialized_fleet_after_warmup(warm):
    started = time.monotonic()
    result = simulate(warm=warm)
    assert result["measured_slots"] >= 120
    assert result["commits_per_slot"] <= 1.1
    assert result["max_60_slot_commits"] <= 1.1
    assert result["rate"] >= .95
    assert result["unknown_ratio"] <= .01
    assert result["max_published_observation_ms"] <= 250
    assert time.monotonic()-started < 20


def test_serialized_fleet_survives_aborted_writer():
    result = simulate(warm=True, abort=True)
    assert result["aborts"] == 1
    assert result["rate"] >= .95
    assert result["unknown_ratio"] == 0
    assert result["max_60_slot_commits"] <= 1.1


def cap_allocation_fleet(monkeypatch, count, *, baseline=False, demand=False, spacing=.1):
    storage, shell = modules("3e5485a650011699bb19f5d500f6221c7eed85c4" if baseline else None)
    clock = Clock()
    start = NOW_US/1e6
    clock.now = start
    db = Database(clock, storage)
    services, writes, errors, futures = [], [], [], []
    # This witness isolates allocation; publisher/flush regressions run in the
    # existing full owner simulator. No production allocation method is replaced.
    monkeypatch.setattr(shell.Service, "_flush", lambda *args: iter(()))
    uuids = iter(UUID(int=i+1) for i in range(count))
    monkeypatch.setattr(shell.Counters.__init__.__globals__["uuid"], "uuid4", lambda: next(uuids))
    settings = Settings(environment="test", async_settle_shadow_policy="shadow-v2",
                        async_settle_shadow_workspaces="ws", source_revision="a"*40)
    original = db.run_in_transaction

    def transaction(callback, **options):
        result = original(callback, **options)
        writes.append(clock.now-start)
        return result

    db.run_in_transaction = transaction
    with ThreadPoolExecutor(max_workers=count) as pool:
        def resume(work, index, value=None, error=None):
            try:
                operation = work.throw(error) if error else work.send(value)
            except StopIteration:
                return

            def execute():
                value, error = None, None
                try:
                    value = operation()
                except Exception as caught:
                    error = caught
                    errors.append(type(caught).__name__)
                clock.schedule(clock.now, lambda: resume(work, index, value, error))

            with clock.condition:
                expected = clock.ready+1
                futures.append(pool.submit(clock.worker, execute, index))
                clock.condition.wait_for(lambda: clock.ready == expected
                    and any(i == index for _, i, _ in clock.events))

        def boot(index):
            store = storage.ObservationStore(db, clock=lambda: clock.now, wall=lambda: clock.now)
            svc = shell.Service(settings, store)
            services.append(svc)
            if demand:
                assert svc.port.reserve(round(clock.now*1e6), terminal=True) is None
                assert svc.port.lost.is_set()  # Existing refusal accounting.
            # run() polls every 10 ms; preserve that quantization.
            first_tick = start+math.ceil((max(clock.now, svc.next_flush)-start)*100)/100
            for tick in range(25):
                clock.schedule(first_tick+tick*5, lambda: resume(svc.maintenance(), index))

        for index in range(count):
            clock.schedule(start+index*spacing, lambda index=index: boot(index))
        clock.run_queue()
        for future in futures:
            future.result()
    for svc in services:
        for executor in (svc.evidence_executor, svc.publisher_executor, svc.session_executor):
            executor.shutdown(wait=True)
    cap = db.rows.get((CONTROL_KIND, "2026-10-12/cap-v2"))
    if cap:
        value = json.loads(cap)
        assert value["terminal_reserved"] == value["event_reserved"] == 100*len(writes)
    result = dict(processes=count, demand=demand, spacing=spacing, writes=len(writes),
                  first_5s=sum(t < 5 for t in writes),
                  peak_5s=max((sum(t <= w < t+5 for w in writes) for t in writes), default=0),
                  max_lock_wait_ms=1000*max((tx.lock_wait for tx in db.transactions), default=0),
                  budget_exhaustions=errors.count("TimeoutError")+errors.count("UnknownCommit"),
                  contention_losses=errors.count("ContentionLoss"),
                  last_write_s=max(writes, default=0))
    print(json.dumps(dict(baseline=baseline, **result), sort_keys=True))
    return result


def test_restart_positive_control(monkeypatch):
    before = cap_allocation_fleet(monkeypatch, 26, baseline=True)
    after = cap_allocation_fleet(monkeypatch, 26)
    assert before["first_5s"] >= 20
    assert after["writes"] == after["budget_exhaustions"] == 0


@pytest.mark.parametrize("count,spacing", [(26, .1), (40, .1), (40, 0.)])
def test_restart_with_cohort_demand(monkeypatch, count, spacing):
    result = cap_allocation_fleet(monkeypatch, count, demand=True, spacing=spacing)
    assert result["writes"] == count
    assert result["first_5s"] <= 5
    assert (result["writes"]+result["contention_losses"])/60 <= 1
    assert result["max_lock_wait_ms"] < 200
    assert result["budget_exhaustions"] == 0


@pytest.mark.parametrize("terminal", [False, True])
def test_demand_refusal_then_grant_and_refill(terminal):
    svc = service()
    assert 100 <= svc.next_flush < 105
    day = "2026-10-12"
    svc.maintain()
    assert not svc.journal.permits and not svc.journal.terminal_permits
    assert svc.port.reserve(NOW_US, terminal=terminal) is None
    assert svc.port.lost.is_set() if terminal else svc.journal.days[day].losses == 1
    assert 100 <= svc.next_allocate < 160
    logical_time.now = svc.next_allocate
    svc.maintain()
    assert svc.journal.permits[day] == svc.journal.terminal_permits[day] == 100
    losses = svc.journal.days[day].losses
    assert svc.port.reserve(NOW_US, terminal=terminal) is not None
    logical_time.advance(5)
    svc.maintain()
    assert svc.journal.permits[day] == 199
    assert svc.journal.terminal_permits[day] == (199 if terminal else 100)
    assert svc.journal.days[day].losses >= losses > 0


@pytest.mark.parametrize("error", [ContentionLoss, UnknownCommit])
def test_failed_allocation_retains_demand_without_grant(monkeypatch, error):
    svc = service()
    day = "2026-10-12"
    assert svc.port.reserve(NOW_US, terminal=True) is None
    real_allocate = svc.store.allocate
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(logical_time())
        raise error("injected")

    monkeypatch.setattr(svc.store, "allocate", fail)
    for attempt in range(1, 7):
        logical_time.now = svc.next_allocate
        svc.maintain()
        assert svc.counters[day]["diagnostics"]["cap_unavailable"] == attempt
        assert not svc.journal.permits and not svc.journal.terminal_permits
        assert svc.allocation_requested
        if error is ContentionLoss:
            ceiling = min(60, 5*2**attempt)
            assert 5 <= svc.next_allocate-logical_time() < 5+ceiling
        else:
            assert svc.next_allocate == logical_time()+5
        svc.maintain()
        assert len(attempts) == attempt
    monkeypatch.setattr(svc.store, "allocate", real_allocate)
    logical_time.now = svc.next_allocate
    svc.maintain()
    assert svc.journal.permits[day] == svc.journal.terminal_permits[day] == 100
    assert svc.allocation_failures == 0 and not svc.allocation_requested
