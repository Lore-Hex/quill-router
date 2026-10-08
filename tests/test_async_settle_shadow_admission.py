from __future__ import annotations

import asyncio
import math

import pytest

from trusted_router.services.async_settle import Admission
from trusted_router.services.async_settle_shadow_admission import Observer


def health(observed=10., **changes):
    return dict(v=1,authority='local',observed_at=observed,worker_heartbeat=observed,complete=True,
                sample_count=0,backlog_count=0,frozen_micro=0,p50_age_seconds=0,p95_age_seconds=0,
                oldest_unresolved_age_seconds=0,dead_count=0,**changes)


def test_cache_does_not_rejuvenate_and_nonmember_has_no_reads():
    now = [10.]
    calls = []
    observer = Observer(frozenset({'ws'}),lambda ws:calls.append(ws) or Admission(0,2),lambda:health(),
                        clock=lambda:now[0],wall=lambda:now[0])
    assert observer.peek('ws')['prediction'] == 'unknown'
    observer.install_workspace('ws',10.,Admission(0,2))
    observer.install_health(10.,health())
    assert observer.peek('ws')['prediction'] == 'yes'
    now[0] = 15.
    assert observer.peek('ws')['reason'] == 'cache_stale'
    observer.install_workspace('other',15.,Admission(0,2))
    assert observer.peek('other')['prediction'] == 'unknown' and calls == []
    assert 'other' not in observer.entries


@pytest.mark.parametrize('phase', [i/20 for i in range(40)])
def test_continuous_health_phase_sweep(phase):
    # Worst publication delay .5, period+excess 2.25; read-start gap 1.25,
    # installation .2 and .25 clock skew. Inspect BETWEEN completions.
    cached = None
    for i in range(4000):
        now = i/100
        read_start = math.floor((now-.2-phase)/1.25)*1.25+phase
        if read_start < 0:
            continue
        publication = math.floor((read_start-.5)/2.25)*2.25
        if publication < 0:
            continue
        cached = publication
        assert now-cached+.25 <= 4.45+1e-9
    assert cached is not None
    # Witness for the rejected four-second polling cadence.
    assert 5.1-0 >= 5 and 4+0.25+0.2 < 5


@pytest.mark.asyncio
async def test_timer_populates_without_eligible(monkeypatch):
    from trusted_router.services.async_settle import AdmissionCache
    eligible_calls = []
    monkeypatch.setattr(AdmissionCache, 'eligible', lambda *args: eligible_calls.append(args))
    calls = []
    from types import SimpleNamespace

    from trusted_router.services import async_settle_shadow_admission as module

    now = 16.0
    observer = Observer(frozenset({'ws'}), lambda ws: calls.append(ws) or Admission(0, 2),
                        lambda: health(now), clock=lambda: now, wall=lambda: now)
    loop = asyncio.get_running_loop()
    jobs = []
    def submit(executor, function, *args):
        job = loop.run_in_executor(executor, function, *args)
        jobs.append(job)
        return job
    async def tick(_):
        # Wait for the exact jobs scheduled by this tick, without a wall budget.
        await asyncio.gather(*jobs)
        observer.stopped = True
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(
        create_task=asyncio.create_task,
        get_running_loop=lambda: SimpleNamespace(run_in_executor=submit), sleep=tick))
    observer.start()
    try:
        await observer.task
        observer.stopped = False
        assert observer.peek('ws')['prediction'] == 'yes'
        assert calls == ['ws'] and observer.counts['health_reads'] == 1
    finally:
        await observer.close()
    assert eligible_calls == [], 'money admission called'
    assert observer.peek('ws')['prediction'] == 'unknown'


def test_empty_creates_no_timer():
    calls = []
    observer = Observer(frozenset(), lambda _: calls.append('workspace'), lambda: calls.append('health'))
    observer.start()
    assert observer.task is observer.executor is None
    assert calls == []


def test_maximum_workspaces_share_two_readers_with_health_priority(monkeypatch):
    from types import SimpleNamespace

    from trusted_router.services import async_settle_shadow_admission as module

    now = [0.]
    jobs = []
    starts = {}
    alive = []
    predictions = []
    keys = frozenset(f"ws-{i:02}" for i in range(32))
    observer = Observer(keys, lambda _: Admission(0, 2), lambda: health(now[0]),
                        clock=lambda: now[0], wall=lambda: now[0])

    class Future:
        finished = False

        def done(self):
            return self.finished

    def submit(executor, function, *args):
        key = args[0] if function == observer._workspace else "health"
        starts.setdefault(key, []).append(now[0])
        future = Future()
        # Binary-exact virtual times, just below the 200 ms RPC deadline.
        jobs.append((now[0] + 25 / 128, future, function, args))
        alive.append(sum(not job[1].done() for job in jobs))
        return future

    async def advance(_):
        now[0] += 1 / 128
        for due, future, function, args in jobs:
            if not future.done() and due <= now[0]:
                function(*args)
                future.finished = True
        if now[0] >= 9:
            predictions.extend(observer.peek(key)["prediction"] for key in keys)
            observer.stopped = True

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(run_in_executor=submit), sleep=advance))
    asyncio.run(observer.run())
    assert set(starts) == keys | {"health"}
    assert max(alive) == 2 and observer.counts["missed_ticks"] == 0
    assert all(len(starts[key]) >= 2 for key in keys)
    assert all(b - a <= (1.25 if key == "health" else 4.25)
               for key, values in starts.items() for a, b in zip(values, values[1:], strict=False))
    assert predictions == ["yes"] * 32 and observer.progress_ok
