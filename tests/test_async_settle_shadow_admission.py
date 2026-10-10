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
    # installation .5 and .25 clock skew. Inspect BETWEEN completions.
    cached = None
    for i in range(4000):
        now = i/100
        read_start = math.floor((now-.5-phase)/1.25)*1.25+phase
        if read_start < 0:
            continue
        publication = math.floor((read_start-.5)/2.25)*2.25
        if publication < 0:
            continue
        cached = publication
        assert now-cached+.25 <= 4.75+1e-9
    assert cached is not None
    # Witness for the rejected four-second polling cadence.
    assert 5.1-0 >= 5 and 4+0.25+0.5 < 5


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


@pytest.mark.parametrize('reader', ['workspace', 'health'])
@pytest.mark.parametrize('seconds,prediction', [(.45, 'yes'), (.5, 'yes'), (.55, 'unknown')])
def test_install_deadline_preserves_fail_closed(reader, seconds, prediction):
    observer = Observer(frozenset({'ws'}), lambda _: Admission(0, 2), lambda: health(0),
                        clock=lambda: seconds, wall=lambda: seconds)
    observer.install_workspace('ws', 0. if reader == 'workspace' else seconds, Admission(0, 2))
    observer.install_health(0. if reader == 'health' else seconds, health(0))
    assert observer.peek('ws')['prediction'] == prediction
    assert observer.progress_ok  # A single failure is below either streak threshold.
    assert observer.counts['late_installs'] == int(seconds > .5)
    assert observer.counts['max_consecutive_failures'] == int(seconds > .5)
    observer.install_workspace('ws', seconds, Admission(0, 2))
    observer.install_health(seconds, health(seconds))
    assert observer.peek('ws')['prediction'] == 'yes'
    assert observer.progress_ok


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
        # Binary-exact virtual times, 195 ms reads within the 500 ms RPC deadline.
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


@pytest.mark.parametrize('reader,threshold', [('health', 3), ('ws', 2)])
@pytest.mark.parametrize('cause', ['read_failures', 'late_installs', 'missed_ticks'])
def test_failure_streak_recovers_and_measures_degraded_union(reader, threshold, cause):
    now = [0.]
    def fail(*_):
        raise TimeoutError('deadline')
    observer = Observer(frozenset({'ws', 'other'}), fail, fail,
                        clock=lambda: now[0], wall=lambda: now[0])
    key = None if reader == 'health' else reader
    for i in range(threshold):
        now[0] = float(i)
        observer.install_workspace('other', now[0], Admission(0, 2))
        if reader != 'health':
            observer.install_health(now[0], health(now[0]))
        if cause == 'read_failures':
            observer._health(now[0]) if key is None else observer._workspace(key, now[0])
        elif cause == 'late_installs':
            observer.install_health(now[0] - .55, health(now[0])) if key is None else observer.install_workspace(key, now[0] - .55, Admission(0, 2))
        else:
            observer.missed_tick(key, now[0])
        assert observer.progress_ok is (i + 1 < threshold)
        assert observer.peek('ws')['prediction'] == 'unknown'
        assert observer.peek('other')['prediction'] == ('unknown' if key is None else 'yes')
    assert observer.counts[cause] == threshold
    assert observer.counts['max_consecutive_failures'] == threshold
    now[0] += .25
    assert observer.snapshot_counts()['degraded_seconds'] == .25
    now[0] += .25
    observer.install_workspace('ws', now[0], Admission(0, 2))
    observer.install_health(now[0], health(now[0]))
    assert observer.peek('ws')['prediction'] == 'yes' and observer.progress_ok
    assert observer.snapshot_counts()['degraded_seconds'] == .5
    now[0] += 1
    assert observer.snapshot_counts()['degraded_seconds'] == .5
    assert observer.counts['max_consecutive_failures'] == threshold


def test_late_start_and_install_are_one_failed_tick_and_old_result_cannot_recover():
    now = [1.]
    observer = Observer(frozenset({'ws'}), lambda _: Admission(0, 2), lambda: health(),
                        clock=lambda: now[0], wall=lambda: now[0])
    observer.missed_tick(None, 1.)
    now[0] = 1.55
    observer.install_health(1., health(1.))
    assert observer.failures[None] == 1
    assert observer.counts['late_installs'] == observer.counts['missed_ticks'] == 1
    observer.missed_tick(None, 1.6)
    observer.install_health(1.55, health(1.55))
    assert observer.failures[None] == 2 and None in observer.stale
    observer.install_health(1.7, health(1.7))
    assert observer.failures[None] == 0 and None not in observer.stale


def test_degraded_seconds_is_union_and_maximum_is_not_summed():
    now = [0.]
    observer = Observer(frozenset({'ws'}), lambda _: Admission(0, 2), lambda: health(),
                        clock=lambda: now[0], wall=lambda: now[0])
    for tick in (0., 1., 2.):
        now[0] = tick
        observer.missed_tick(None, tick)
        observer.missed_tick('ws', tick)
    now[0] = 3.
    assert observer.snapshot_counts()['degraded_seconds'] == 2.
    observer.install_workspace('ws', 3., Admission(0, 2))
    now[0] = 4.
    observer.install_health(4., health(4.))
    counts = observer.snapshot_counts()
    assert counts['degraded_seconds'] == 3.
    assert counts['max_consecutive_failures'] == 3
    assert observer.snapshot_counts()['max_consecutive_failures'] == 0


def test_timer_late_start_stales_until_next_clean_tick_without_catch_up(monkeypatch):
    from types import SimpleNamespace

    from trusted_router.services import async_settle_shadow_admission as module
    now = [0.]
    starts = []
    samples = []
    observer = Observer(frozenset({'ws'}), lambda _: Admission(0, 2), lambda: health(now[0]),
                        clock=lambda: now[0], wall=lambda: now[0])
    def submit(executor, function, *args):
        starts.append((function.__name__, now[0]))
        function(*args)
        return SimpleNamespace(done=lambda: True)
    times = iter([1.375, 2.375, 4.375, 5.375])
    async def advance(_):
        samples.append(observer.peek('ws')['prediction'])
        try:
            now[0] = next(times)
        except StopIteration:
            observer.stopped = True
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(run_in_executor=submit), sleep=advance))
    asyncio.run(observer.run())
    # Late health at 1.375, recovery at 2.375; workspace is independently late
    # at 4.375 and remains stale until its next four-second tick.
    assert samples[:3] == ['yes', 'unknown', 'yes']
    assert observer.counts['missed_ticks'] == 3
    assert observer.counts['health_reads'] == 5 and observer.counts['workspace_reads'] == 2
    assert len(starts) == 7  # No catch-up for either missed health interval.
    assert observer.counts['max_consecutive_failures'] == 1


def test_read_failure_preserves_ages_but_invalidates_only_that_workspace():
    now = [0.]
    def fail(_):
        raise TimeoutError('deadline')
    observer = Observer(frozenset({'ws', 'other'}), fail, lambda: health(now[0]),
                        clock=lambda: now[0], wall=lambda: now[0])
    for key in observer.workspaces:
        observer.install_workspace(key, 0., Admission(0, 2))
    observer.install_health(0., health(0.))
    now[0] = .5
    observer._workspace('ws', 0.)
    prediction = observer.peek('ws')
    assert (prediction['prediction'], prediction['reason'], prediction['workspace_age_us']) == ('unknown', 'cache_stale', 500000)
    assert observer.peek('other')['prediction'] == 'yes'
    observer.install_workspace('ws', .5, Admission(0, 2))
    assert observer.peek('ws')['prediction'] == 'yes'
