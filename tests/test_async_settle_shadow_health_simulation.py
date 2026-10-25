"""Real worker/storage execution with serialized claim-row transactions."""
import hashlib
import json
import math
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from tests import shadow_health_cold_start_simulation as simulation
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


ROUND_TWO = "c2e8139b19d510b5af4db5489da2aef9f55a0c7d"


@pytest.mark.parametrize("base", ["main", None])
def test_controls_resolve_baseline_in_shallow_checkout(monkeypatch, base):
    if base:
        monkeypatch.setenv("GITHUB_BASE_REF", base)
    else:
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    loaded = []

    def check(command, **kwargs):
        if not base or not command[-1].startswith(f"{base}:"):
            raise subprocess.CalledProcessError(128, command)

    def show(command, **kwargs):
        loaded.append(command[-1])
        assert base and command[-1].startswith(f"{base}:")
        # Model a checkout with only the CI base ref, using local source blobs.
        return (simulation.ROOT/command[-1].split(":", 1)[1]).read_bytes()

    monkeypatch.setattr(simulation.subprocess, "run", check)
    monkeypatch.setattr(simulation.subprocess, "check_output", show)
    if base:
        storage, publisher = modules("baseline")
        assert publisher.ObservationStore is storage.ObservationStore
        assert len(loaded) == 2
    else:
        with pytest.raises(pytest.skip.Exception, match="baseline unavailable"):
            modules("baseline")
        assert not loaded
    # Missing baseline objects never stop independent worktree cases.
    storage, publisher = modules()
    assert publisher.ObservationStore is storage.ObservationStore


@pytest.mark.parametrize("ref", ["587c15eb407d6bc075964034da94596ec1238b89", ROUND_TWO])
def test_controls_skip_missing_historical_sources(monkeypatch, ref):
    def missing(command, **kwargs):
        raise subprocess.CalledProcessError(128, command)

    monkeypatch.setattr(simulation.subprocess, "check_output", missing)
    with pytest.raises(pytest.skip.Exception, match="simulation revision.*shallow checkout"):
        modules(ref)
    storage, publisher = modules()
    assert publisher.ObservationStore is storage.ObservationStore


def cap_allocation_fleet(monkeypatch, count, *, baseline=False, demand=False, spacing=.1, ref=None):
    storage, shell = modules("baseline" if baseline else ref)
    clock = Clock()
    start = NOW_US/1e6
    clock.now = start
    db = Database(clock, storage)
    services, writes, errors, futures = {}, [], [], []
    demands, grants, attempts, first_attempts, first_ticks = {}, {}, [], {}, {}
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
                record = dict(process=index, started_s=started-start,
                              duration_s=clock.now-started,
                              error=type(error).__name__ if error else None)
                attempts.append(record)
                if error is None:
                    grants.setdefault(index, clock.now)
                def complete():
                    resume(work, index, value, error)
                    if record["error"] == "ContentionLoss":
                        record["retry_at_s"] = services[index].next_allocate-start
                clock.schedule(clock.now, complete)

            with clock.condition:
                expected = clock.ready+1
                futures.append(pool.submit(clock.worker, execute, index))
                clock.condition.wait_for(lambda: clock.ready == expected
                    and any(i == index for _, i, _ in clock.events))

        def boot(index):
            store = storage.ObservationStore(db, clock=lambda: clock.now, wall=lambda: clock.now)
            svc = shell.Service(settings, store)
            services[index] = svc
            def request():
                demands[index] = clock.now
                reservation = svc.port.reserve(round(clock.now*1e6), terminal=True)
                if reservation is None:
                    assert svc.port.lost.is_set()  # Existing refusal accounting.
            if demand:
                clock.schedule(clock.now+.2, request)
            # run() polls every 10 ms; preserve that quantization.
            first_tick = start+math.ceil((max(clock.now, svc.next_flush)-start)*100)/100
            first_ticks[index] = first_tick-start
            for tick in range(100):
                clock.schedule(first_tick+tick*5, lambda: resume(svc.maintenance(), index))

        for index in range(count):
            clock.schedule(start+index*spacing, lambda index=index: boot(index))
        clock.run_queue()
        for future in futures:
            future.result()
    for svc in services.values():
        for executor in (svc.evidence_executor, svc.publisher_executor, svc.session_executor):
            executor.shutdown(wait=True)
    cap = db.rows.get((CONTROL_KIND, "2026-10-12/cap-v2"))
    if cap:
        value = json.loads(cap)
        assert value["terminal_reserved"] == value["event_reserved"] == 100*len(writes)
    result = dict(processes=count, demand=demand, spacing=spacing, writes=len(writes),
                  granted_processes=len(grants),
                  first_5s=sum(t < 5 for t in writes),
                  peak_5s=max((sum(t <= w < t+5 for w in writes) for t in writes), default=0),
                  max_lock_wait_ms=1000*max((tx.lock_wait for tx in db.transactions), default=0),
                  budget_exhaustions=errors.count("TimeoutError")+errors.count("UnknownCommit"),
                  contention_losses=errors.count("ContentionLoss"),
                  allocation_attempts=len(attempts),
                  attempts_first_minute=sum(a["started_s"] < 60 for a in attempts),
                  contention_first_minute=sum(a["started_s"] < 60 and a["error"] == "ContentionLoss"
                                              for a in attempts),
                  max_attempt_ms=1000*max((a["duration_s"] for a in attempts), default=0),
                  last_first_grant_s=max((stamp-start for stamp in grants.values()), default=0),
                  max_demand_to_attempt_s=max((first_attempts[i]-stamp for i, stamp in demands.items()), default=0),
                  max_demand_to_grant_s=max((max(0, grants[i]-stamp) for i, stamp in demands.items()), default=0),
                  last_write_s=max(writes, default=0))
    print(json.dumps(dict(baseline=baseline, ref=ref, **result), sort_keys=True))
    assert len(attempts) == len(writes)+len(errors)
    return dict(result, attempt_log=attempts, first_ticks=first_ticks,
                boots={i: str(svc.journal.boot) for i, svc in services.items()},
                demand_times={i: stamp-start for i, stamp in demands.items()})


@pytest.mark.parametrize("ref", [ROUND_TWO, None])
def test_idle_startup_block_invariant(monkeypatch, ref):
    before = cap_allocation_fleet(monkeypatch, 26, baseline=True)
    after = cap_allocation_fleet(monkeypatch, 26, ref=ref)
    assert before["first_5s"] >= 20
    assert after["writes"] == (0 if ref == ROUND_TWO else 26)  # No idle refills.
    assert after["allocation_attempts"] == after["writes"]
    assert after["budget_exhaustions"] == 0


@pytest.mark.parametrize("ref", [ROUND_TWO, None])
@pytest.mark.parametrize("count,spacing", [(26, .1), (40, .1), (26, 0.), (40, 0.)])
def test_restart_with_cohort_demand(monkeypatch, count, spacing, ref):
    result = cap_allocation_fleet(monkeypatch, count, demand=True, spacing=spacing, ref=ref)
    assert result["granted_processes"] == count
    if ref == ROUND_TWO:
        assert result["writes"] == count
        assert result["first_5s"] > 5
        assert (result["writes"]+result["contention_losses"])/60 <= 1
    else:
        assert count <= result["writes"] <= 2*count
        assert result["first_5s"] >= 1  # Simultaneous eager starts contend immediately.
    assert result["max_lock_wait_ms"] < 200
    assert result["max_attempt_ms"] < 2000
    assert result["max_demand_to_attempt_s"] <= 5.01
    assert result["budget_exhaustions"] == 0


@pytest.mark.parametrize("ref", ["587c15eb407d6bc075964034da94596ec1238b89", ROUND_TWO, "baseline", None])
def test_first_demand_next_tick_loss_control(monkeypatch, ref):
    # Retain round 1 and compare committed round 2, main and the worktree.
    _, shell = modules(ref)
    round_one = ref == "587c15eb407d6bc075964034da94596ec1238b89"
    delayed = round_one or ref == ROUND_TWO
    monkeypatch.setattr(shell.Counters.__init__.__globals__["uuid"], "uuid4", lambda: UUID(int=1))
    store, _ = fake_store()
    svc = shell.Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), store)
    day = "2026-10-12"
    started = logical_time()
    first_tick = started+math.ceil((max(started, svc.next_flush)-started)*100)/100
    if delayed:
        assert started+.2 < first_tick < started+5
    else:
        assert first_tick == started
    svc.next_flush = first_tick
    losses, requests, allocations = [], [], []
    real_loss, real_allocate = svc.journal.loss, svc.store.allocate

    def loss(day):
        losses.append((logical_time(), day))
        real_loss(day)

    def allocate(*args, **kwargs):
        result = real_allocate(*args, **kwargs)
        logical_time.advance(.073)  # Same fixed 20+20+33 ms costs as the lock witness.
        allocations.append(logical_time())
        return result

    monkeypatch.setattr(svc.journal, "loss", loss)
    monkeypatch.setattr(svc.store, "allocate", allocate)
    try:
        # First request at +200 ms, then every 100 ms for the following 5 s.
        for index in range(51):
            stamp = started+.2+index/10
            if svc.next_flush+.073 <= stamp:
                logical_time.now = max(started, svc.next_flush)
                svc.next_flush = logical_time()+5
                svc.maintain()  # Real allocation AND flush, including sticky-loss folding.
            logical_time.now = stamp
            reservation = svc.port.reserve(NOW_US+round((stamp-started)*1e6), terminal=True)
            requests.append((stamp, reservation is not None))
            if reservation is not None:
                assert svc.port.finalize(reservation, event_for(reservation)) is not None
                assert svc.port.complete(reservation)
        after_tick = [admitted for stamp, admitted in requests if stamp > first_tick+.073]
        assert after_tick
        assert not any(admitted for stamp, admitted in requests if stamp < first_tick)
        if round_one:
            assert svc.next_allocate > started+5.2
            assert not allocations and not any(after_tick)
        else:
            assert allocations[0] == pytest.approx(first_tick+.073)
            assert all(after_tick)
        # Refusal remains sticky in the delayed controls; eager startup adds none.
        assert len(losses) == int(delayed)
        assert svc.port.lost.is_set() == delayed
        logical_time.now = max(logical_time(), first_tick+5)
        svc.maintain()
        assert len(losses) == int(delayed)
        assert svc.journal.days[day].losses == int(delayed)
        print(json.dumps(dict(ref=ref, first_tick_s=first_tick-started,
            first_grant_s=allocations[0]-started if allocations else None,
            refused=sum(not admitted for _, admitted in requests),
            admitted=sum(admitted for _, admitted in requests), journal_losses=len(losses)), sort_keys=True))
    finally:
        for executor in (svc.evidence_executor, svc.publisher_executor, svc.session_executor):
            executor.shutdown(wait=True)


@pytest.mark.parametrize("terminal", [False, True])
def test_demand_refusal_then_grant_and_refill(terminal):
    svc = service()
    assert svc.next_flush == float("-inf")
    day = "2026-10-12"
    # A request before the owner has run still follows the existing refusal path.
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
            ceiling = min(40, 5*2**attempt)
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


@pytest.mark.parametrize("count", [26, 40])
@pytest.mark.parametrize("ref", ["baseline", ROUND_TWO, None])
def test_simultaneous_restart_comparison(monkeypatch, count, ref):
    result = cap_allocation_fleet(monkeypatch, count, demand=True, spacing=0., ref=ref)
    assert result["granted_processes"] == count
    assert count <= result["writes"] <= 2*count
    assert result["budget_exhaustions"] == 0
    assert result["max_attempt_ms"] < 2000


@pytest.mark.parametrize("count", [26, 40])
def test_simultaneous_restart_attempt_control(monkeypatch, count):
    before = cap_allocation_fleet(monkeypatch, count, demand=True, spacing=0., ref="baseline")
    after = cap_allocation_fleet(monkeypatch, count, demand=True, spacing=0.)
    # Count every invocation, including aborted transactions, not just commits.
    # These are bounds for this fixed 500 s witness, not a fleet rate limiter.
    limits = dict(attempts_first_minute=5*count, contention_first_minute=4*count,
                  allocation_attempts=7*count, contention_losses=6*count)
    print(json.dumps(dict(processes=count, attempt_control={metric: dict(
        baseline=before[metric], worktree=after[metric], bound=limit)
        for metric, limit in limits.items()}), sort_keys=True))
    for metric, limit in limits.items():
        assert after[metric] <= limit, f"{metric}: worktree {after[metric]} > bound {limit}"
        assert before[metric] > limit, f"{metric}: baseline must violate bound {limit}"
    for result in (before, after):
        assert result["granted_processes"] == count
        assert result["budget_exhaustions"] == 0
        assert result["max_attempt_ms"] < 2000
        assert sum(a["started_s"] == 0 for a in result["attempt_log"]) == count
        for process in range(count):
            attempts = sorted((a for a in result["attempt_log"] if a["process"] == process),
                              key=lambda a: a["started_s"])
            assert attempts[0]["started_s"] == result["first_ticks"][process] == 0
            assert attempts[-1]["error"] is None
    retry_deadlines, retry_ticks = [], []
    for process in range(count):
        attempts = sorted((a for a in after["attempt_log"] if a["process"] == process),
                          key=lambda a: a["started_s"])
        failures = 0
        for attempt, following in zip(attempts, attempts[1:], strict=False):
            if attempt["error"] is None:
                # The only post-grant refill follows the one +200 ms request.
                assert (attempt["started_s"]+attempt["duration_s"]
                        < after["demand_times"][process] < following["started_s"])
                failures = 0
                continue
            assert attempt["error"] == "ContentionLoss"
            failures += 1
            seed = f"{after['boots'][process]}/2026-10-12/{failures}"
            fraction = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big")/2**64
            delay = 5+min(40, 5*2**failures)*fraction
            # Absolute epoch subtraction has sub-microsecond rounding error.
            deadline = attempt["started_s"]+attempt["duration_s"]+delay
            assert attempt["retry_at_s"] == pytest.approx(deadline, abs=1e-6)
            assert following["started_s"] == 5*math.ceil(deadline/5)
            if attempt is attempts[0]:
                retry_deadlines.append(attempt["retry_at_s"])
                retry_ticks.append(following["started_s"])
    assert len(set(retry_deadlines)) == count-1
    # Five-second maintenance quantization can collide individual retries;
    # jitter breaks the fleet-wide lock-step, not every possible collision.
    assert len(set(retry_ticks)) > 1


@pytest.mark.parametrize("ref", ["baseline", ROUND_TWO, None])
def test_rollover_refusal_window_control(monkeypatch, ref):
    _, shell = modules(ref)
    store, _ = fake_store()
    boundary = NOW_US+86_400_000_000
    started = logical_time()
    monkeypatch.setattr(store, "wall", lambda: boundary/1e6+logical_time()-started-.2)
    svc = shell.Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), store)
    real_allocate = store.allocate
    grants = []

    def allocate(*args, **kwargs):
        result = real_allocate(*args, **kwargs)
        logical_time.advance(.073)
        grants.append(logical_time()-started-.2)
        return result

    monkeypatch.setattr(store, "allocate", allocate)
    try:
        # An established busy process has just run maintenance 200 ms before
        # midnight. The owner opens the new day at midnight, before traffic.
        svc.tick(boundary-200_000, publisher=False)
        svc.port.reserve(boundary-200_000, terminal=True)
        svc.maintain()
        logical_time.now = started+.2
        svc.tick(boundary, publisher=False)
        requests = []
        next_tick = started+5
        for index in range(52):
            stamp = started+.2+index/10
            if next_tick+.073 <= stamp:
                logical_time.now = next_tick
                svc.maintain()
                next_tick += 5
            logical_time.now = stamp
            reservation = svc.port.reserve(boundary+index*100_000, terminal=True)
            requests.append(reservation is not None)
            if reservation is not None:
                assert svc.port.finalize(reservation, event_for(reservation)) is not None
                assert svc.port.complete(reservation)
        assert not any(requests[:49]) and all(requests[49:])
        assert grants[-1] == pytest.approx(4.873)
        assert svc.port.lost.is_set()
        print(json.dumps(dict(rollover_ref=ref, first_grant_s=grants[-1],
            refused=requests.count(False), admitted=requests.count(True)), sort_keys=True))
    finally:
        for executor in (svc.evidence_executor, svc.publisher_executor, svc.session_executor):
            executor.shutdown(wait=True)
