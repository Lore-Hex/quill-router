"""Real worker/storage execution with serialized claim-row transactions."""
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from tests.shadow_health_cold_start_simulation import Clock, Database, modules, simulate
from tests.shadow_observation_fixtures import event_for
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    fake_store,
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
    demands, grants, attempts, first_attempts = {}, {}, [], {}
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
                started = clock.now
                first_attempts.setdefault(index, started)
                try:
                    value = operation()
                except Exception as caught:
                    error = caught
                    errors.append(type(caught).__name__)
                attempts.append(clock.now-started)
                if error is None:
                    grants[index] = clock.now
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
            def request():
                demands[index] = clock.now
                assert svc.port.reserve(round(clock.now*1e6), terminal=True) is None
                assert svc.port.lost.is_set()  # Existing refusal accounting.
            if demand:
                clock.schedule(clock.now+.2, request)
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
                  max_attempt_ms=1000*max(attempts, default=0),
                  max_demand_to_attempt_s=max((first_attempts[i]-stamp for i, stamp in demands.items()), default=0),
                  max_demand_to_grant_s=max((grants[i]-stamp for i, stamp in demands.items()), default=0),
                  last_write_s=max(writes, default=0))
    print(json.dumps(dict(baseline=baseline, **result), sort_keys=True))
    return result


def test_restart_positive_control(monkeypatch):
    before = cap_allocation_fleet(monkeypatch, 26, baseline=True)
    after = cap_allocation_fleet(monkeypatch, 26)
    assert before["first_5s"] >= 20
    assert after["writes"] == after["budget_exhaustions"] == 0


@pytest.mark.parametrize("count,spacing", [(26, .1), (40, .1), (26, 0.), (40, 0.)])
def test_restart_with_cohort_demand(monkeypatch, count, spacing):
    result = cap_allocation_fleet(monkeypatch, count, demand=True, spacing=spacing)
    assert result["writes"] == count
    assert result["first_5s"] > 5
    assert (result["writes"]+result["contention_losses"])/60 <= 1
    assert result["max_lock_wait_ms"] < 200
    assert result["max_attempt_ms"] < 2000
    assert result["max_demand_to_attempt_s"] <= 5.01
    assert result["budget_exhaustions"] == 0


@pytest.mark.parametrize("round_one", [True, False])
def test_first_demand_next_tick_loss_control(monkeypatch, round_one):
    # Execute the committed round-1 implementation as the positive control.
    _, shell = modules("587c15eb407d6bc075964034da94596ec1238b89" if round_one else None)
    monkeypatch.setattr(shell.Counters.__init__.__globals__["uuid"], "uuid4", lambda: UUID(int=1))
    store, _ = fake_store()
    svc = shell.Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), store)
    day = "2026-10-12"
    started = logical_time()
    first_tick = started+math.ceil((svc.next_flush-started)*100)/100
    assert started+.2 < first_tick < started+5
    losses, requests, allocations = [], [], []
    real_loss, real_allocate = svc.journal.loss, svc.store.allocate

    def loss(day):
        losses.append((logical_time(), day))
        real_loss(day)

    def allocate(*args, **kwargs):
        allocations.append(logical_time())
        return real_allocate(*args, **kwargs)

    monkeypatch.setattr(svc.journal, "loss", loss)
    monkeypatch.setattr(svc.store, "allocate", allocate)
    try:
        # First request at +200 ms, then every 100 ms for the following 5 s.
        for index in range(51):
            stamp = started+.2+index/10
            if svc.next_flush <= stamp:
                logical_time.now = first_tick
                svc.next_flush = first_tick+5
                svc.maintain()  # Real allocation AND flush, including sticky-loss folding.
            logical_time.now = stamp
            reservation = svc.port.reserve(NOW_US+round((stamp-started)*1e6), terminal=True)
            requests.append((stamp, reservation is not None))
            if reservation is not None:
                assert svc.port.finalize(reservation, event_for(reservation)) is not None
                assert svc.port.complete(reservation)
        after_tick = [admitted for stamp, admitted in requests if stamp > first_tick]
        assert after_tick
        assert not any(admitted for stamp, admitted in requests if stamp < first_tick)
        if round_one:
            assert svc.next_allocate > started+5.2
            assert not allocations and not any(after_tick)
        else:
            assert allocations == [first_tick]
            assert all(after_tick)
        # The first refusal must remain evidence loss. Removing the delay cannot
        # make this boot-day loss-free without changing the refusal contract.
        assert losses == [(first_tick, day)]
        logical_time.now = first_tick+5
        svc.maintain()
        assert losses == [(first_tick, day)]  # No additional loss after the tick.
        assert svc.journal.days[day].losses == 1
        print(json.dumps(dict(round_one=round_one, first_tick_s=first_tick-started,
            refused=sum(not admitted for _, admitted in requests),
            admitted=sum(admitted for _, admitted in requests), journal_losses=len(losses)), sort_keys=True))
    finally:
        for executor in (svc.evidence_executor, svc.publisher_executor, svc.session_executor):
            executor.shutdown(wait=True)


@pytest.mark.parametrize("terminal", [False, True])
def test_demand_refusal_then_grant_and_refill(terminal):
    svc = service()
    assert 100 <= svc.next_flush < 105
    day = "2026-10-12"
    svc.maintain()
    assert not svc.journal.permits and not svc.journal.terminal_permits
    assert svc.port.reserve(NOW_US, terminal=terminal) is None
    assert svc.port.lost.is_set() if terminal else svc.journal.days[day].losses == 1
    assert svc.next_allocate == logical_time() == 100
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
