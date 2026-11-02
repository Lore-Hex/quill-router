"""Cold-start and cadence regressions through the production publisher owner."""
import asyncio
from dataclasses import replace

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_health_contention import outcome
from tests.test_async_settle_shadow_health_stagger import advance, timed_owner
from tests.test_async_settle_shadow_health_watchdog import accounting
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    service,
)
from tests.test_async_settle_shadow_observation_r7 import entries
from trusted_router import async_settle_shadow_lifecycle as lifecycle
from trusted_router.async_settle_shadow_v2_a1_health import PUBLISHER_SLOT_TRACE
from trusted_router.services import async_settle_shadow_health as shell


@pytest.mark.parametrize('reason', ['publisher_watchdog', 'publisher_worker_busy'])
def test_release_leaves_immediately_next_slot_publishable(monkeypatch, reason):
    svc, wall, mono, job, _ = timed_owner(monkeypatch)
    svc.accept(lifecycle.PublisherFailed(wall[0], job.slot, job.generation, reason))
    next_job = advance(svc, wall, mono, job.slot+1)
    svc.submit_publisher(next_job.slot, wall[0])
    outcome(svc, next_job, 'published')
    accounting(svc)


@pytest.mark.parametrize("early", [0., .1, .3])
def test_early_next_wake_waits_for_actual_start_cadence(monkeypatch, early):
    svc, wall, mono, job, waits = timed_owner(monkeypatch)
    # Receipt 896295396: published start 8,633 us into the slot, cycle 97,787 us.
    mono[0] += .008633
    wall[0] += 8633
    svc.incumbent_slot = job.slot-1
    svc.submit_publisher(job.slot, wall[0])
    outcome(svc, job, 'published')
    first_start = svc.state.last_actual_start
    wall[0], mono[0] = (job.slot+1)*lifecycle.SLOT_US, 102.-early
    svc.store.database.server_ns = wall[0]*1000
    svc.tick(wall[0])
    next_job = svc.state.job
    # Forward wall steps/early monotonic wakes wait for the reserved boundary;
    # the previous worker's 8.633ms jitter is not carried forward.
    assert next_job.earliest_start_mono == pytest.approx(102.)
    svc.submit_publisher(next_job.slot, wall[0])
    trace = outcome(svc, next_job, 'missed' if early > .25 else 'published')
    assert waits == pytest.approx([early] if 0 < early <= .25 else [])
    if early > .25:
        assert trace['rpc_count'] == 0
        assert svc.state.last_actual_start == first_start
    else:
        assert svc.state.last_actual_start-first_start >= 1.75
        assert trace['claim_start_us'] % lifecycle.SLOT_US == round(early*1e6)
    assert trace['cycle_us'] == (None if early > .25 else 0)  # Wait precedes actual admission.


@pytest.mark.parametrize('change', ['revoke', 'late'])
def test_cadence_wait_rechecks_revocation_and_window(monkeypatch, change):
    svc, wall, mono, job, _ = timed_owner(monkeypatch)
    job = replace(job, earliest_start_mono=mono[0]+.01)
    svc._state = replace(svc.state, job=job)
    svc.admission_job = job
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        mono[0] += .3 if change == 'late' else seconds
        wall[0] += 300_000 if change == 'late' else round(seconds*1e6)
        if change == 'revoke':
            svc.revocation_epoch = 1
    monkeypatch.setattr(shell, 'sleep', sleep)
    svc.submit_publisher(job.slot, wall[0])
    trace = outcome(svc, job, 'missed')
    assert waits and trace['rpc_count'] == 0
    assert not svc.store.database.commits


def test_stagger_retains_cold_spread_and_waits_for_warm_claim():
    cold = {shell.follower_stagger(f'boot-{b}', s, False) for b in range(12) for s in range(100)}
    warm = {shell.follower_stagger(f'boot-{b}', s, True) for b in range(12) for s in range(100)}
    assert cold == {0, .03, .06, .09, .12, .15, .18, .21}
    assert warm == {.06, .09, .12}
    assert .054 < min(warm) < max(warm) < .250  # Claim committed before observation at B+54ms.
    assert shell.follower_stagger('boot', 7, False) == .09


@pytest.mark.parametrize('gap', [1, 2, 3])
def test_installed_fleet_publication_selects_follower_stagger(monkeypatch, gap):
    svc, wall, _, job, waits = timed_owner(monkeypatch)
    svc.observer.health = {'body': {'slot': job.slot-gap}}
    svc.submit_publisher(job.slot, wall[0])
    # A cached health body selects timing, but cannot veto an election when
    # the authoritative claim row is absent.
    outcome(svc, job, 'published')
    assert svc.store.database.commits == 2
    # Once health was installed, keep the warm follower band.
    assert waits == [shell.follower_stagger(svc.journal.boot, job.slot, True)]


@pytest.mark.parametrize('contenders', [8, 12])
def test_cold_fleet_establishes_incumbent(contenders):
    from tests.shadow_health_cold_start_simulation import simulate
    result = simulate(contenders)
    assert result['rate'] >= .99
    assert result['first_published_slot_offset'] <= 1
    assert result['first_incumbent_ms'] <= 2500


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['worker', 'submit'])
@pytest.mark.parametrize('previous_publication', [False, True])
async def test_unknown_fallback_preserves_start_and_next_slot(monkeypatch, failure, previous_publication):
    svc, wall, mono, job, _ = timed_owner(monkeypatch)
    if previous_publication:
        svc.submit_publisher(job.slot, wall[0])
        outcome(svc, job, 'published')
        job = advance(svc, wall, mono, job.slot+1)
    svc.loop = asyncio.get_running_loop()
    before = svc.state.last_actual_start
    commits = svc.store.database.commits
    def escaped(*args, **kwargs):
        raise RuntimeError('no worker start evidence')
    try:
        with monkeypatch.context() as patch:
            if failure == 'worker':
                patch.setattr(shell, 'publish_cycle', escaped)
                svc.submit_publisher(job.slot, wall[0])
                with pytest.raises(RuntimeError, match='no worker start evidence'):
                    await asyncio.wrap_future(svc.publisher_worker)
                await svc.publisher_job
            else:
                patch.setattr(svc.publisher_executor, 'submit', escaped)
                with pytest.raises(RuntimeError, match='no worker start evidence'):
                    svc.submit_publisher(job.slot, wall[0])
        trace = outcome(svc, job, 'failed', unknown=True)
        assert trace['release_reason'] == (
            'publisher_worker_exception' if failure == 'worker' else 'publisher_submit_failed')
        PUBLISHER_SLOT_TRACE(trace)
        assert trace['rpc_count'] == 0 and trace['claim_at_ns'] is None
        assert svc.store.database.commits == commits
        assert svc.state.last_actual_start == before
        counts = dict(svc.counters[trace['day']]['publisher'])
        assert counts['unknown_outcomes'] == counts['failures'] == 1
        assert counts['claim_attempts'] == 1+previous_publication
        assert svc.publisher_job.done() and not svc.publisher_worker_active.is_set()
        fallback = shell.failed_publisher_reply(svc.journal.boot, job)
        assert fallback.actual_start_mono is None
        next_job = advance(svc, wall, mono, job.slot+1)
        assert next_job.earliest_start_mono < next_job.latest_start_mono
        state, traces = svc.state, entries(svc)
        assert svc.publisher_reply(fallback, job=job) == (lifecycle.Fault('invalid_publisher_reply'),)
        assert svc.state == state and entries(svc) == traces
        svc.submit_publisher(next_job.slot, wall[0])
        await svc.publisher_job
        outcome(svc, next_job, 'published')
        accounting(svc)
    finally:
        svc.publisher_executor.shutdown(wait=True)


def test_scope_setup_does_not_accumulate_after_cadence_wait(monkeypatch):
    from contextlib import contextmanager

    svc, wall, mono, job, _ = timed_owner(monkeypatch)
    svc.incumbent_slot = job.slot-1
    original = shell.rpc_scope
    @contextmanager
    def setup(*args):
        mono[0] += .005
        wall[0] += 5000
        svc.store.database.server_ns = wall[0]*1000
        with original(*args):
            yield
    monkeypatch.setattr(shell, "rpc_scope", setup)
    starts = []
    for offset in range(5):
        current = job if not offset else advance(svc, wall, mono, job.slot+offset)
        svc.submit_publisher(current.slot, wall[0])
        trace = outcome(svc, current, "published")
        starts.append(trace["claim_start_us"]-current.slot*lifecycle.SLOT_US)
    # Previously 5, 10, 15, 20, 25ms: each setup followed the cadence wait.
    assert starts == [5000]*5


@pytest.mark.parametrize("forward", [True, False])
def test_wall_steps_cannot_compress_cadence_or_replay_slots(forward):
    state = lifecycle.State()
    stamp = 1_792_590_792_000_000
    starts = []
    for index in range(20):
        # Wall advances two seconds while monotonic advances only 100ms.
        wall = stamp+index*lifecycle.SLOT_US if forward else stamp-index*lifecycle.SLOT_US
        state, effects = lifecycle.step(state, lifecycle.Tick(wall, 100.+index*.1))
        jobs = [e.job for e in effects if isinstance(e, lifecycle.StartPublisherCycle)]
        if not jobs:
            continue
        job = jobs[0]
        if job.earliest_start_mono <= job.latest_start_mono:
            starts.append(job.earliest_start_mono)
            state, _ = lifecycle.step(state, lifecycle.SlotOutcome(wall, job.slot, "failed",
                generation=job.generation, actual_start_mono=job.earliest_start_mono))
        else:
            state, _ = lifecycle.step(state, lifecycle.SlotOutcome(wall, job.slot, "missed",
                generation=job.generation))
    assert starts == ([100., 102.] if forward else [100.])
    assert state.last_start_boundary == starts[-1]
    # After impossible early windows, the next feasible window can run.
    # Reserving those impossible jobs would incorrectly push Q to 140s.
    state, effects = lifecycle.step(state, lifecycle.Tick(stamp+20*lifecycle.SLOT_US, 104.))
    job = next(e.job for e in effects if isinstance(e, lifecycle.StartPublisherCycle))
    assert job.earliest_start_mono == 104. <= job.latest_start_mono
    assert lifecycle.accounted_once(state)
    assert lifecycle.conservation(state)


@pytest.fixture
def offline_driver(monkeypatch):
    svc = service()
    wall, mono, claims = [NOW_US+lifecycle.SLOT_US], [102.], []
    svc.store.wall = lambda: wall[0]/1e6
    svc.store.clock = lambda: mono[0]
    svc.store.database.server_ns = wall[0]*1000
    original = svc.store.claim

    def claim(boot, slot, *args, **kwargs):
        claims.append(slot)
        return original(boot, slot, *args, **kwargs)

    def sleep(seconds):
        mono[0] += seconds
        wall[0] += round(seconds*1e6)
        svc.store.database.server_ns = wall[0]*1000

    monkeypatch.setattr(svc.store, "claim", claim)
    monkeypatch.setattr(shell, "sleep", sleep)
    return svc, wall, mono, claims


def test_offline_stale_publish_does_not_block_next_slot(offline_driver):
    svc, wall, mono, claims = offline_driver
    slot = NOW_US//lifecycle.SLOT_US
    # Fresh service, caller still names s while the paired sample is in s+1.
    svc.publish_cycle(slot, NOW_US)
    pending = svc.state.job
    assert not claims and not svc.store.database.snapshot_calls
    assert svc.store.database.commits == 0
    wall[0], mono[0] = NOW_US+2*lifecycle.SLOT_US, 104.
    svc.store.database.server_ns = wall[0]*1000
    svc.publish_cycle(slot+2, wall[0])
    print("OFFLINE_STALE", "pending_after_stale", pending.slot-slot if pending else None,
          "claim_offsets", [s-slot for s in claims],
          "outcomes", [(t["slot"]-slot, t["outcome"]) for t in entries(svc)])
    assert claims == [slot+2]
    assert pending is None and svc.state.job is None and svc.admission_job is None
    assert not svc.publishing and svc.publisher_faults == 0
    traces = entries(svc)
    assert [(t["slot"], t["outcome"]) for t in traces] == [(slot+1, "missed"), (slot+2, "published")]
    assert traces[0]["rpc_count"] == 0 and not traces[0]["unknown_outcome"]
    day, = svc.state.days
    assert (day.scheduled, day.missed, day.attempted, day.published, day.failed) == (2, 1, 1, 1, 0)
    accounting(svc)


@pytest.mark.parametrize("slot_offset,stamp_offset", [
    (-1, -lifecycle.SLOT_US), (1, lifecycle.SLOT_US), (-1, 0),
    (0, -lifecycle.DAY_US), (0, lifecycle.DAY_US),
], ids=["stale-slot", "future-slot", "inconsistent-slot", "old-stamp-day", "future-stamp-day"])
def test_offline_schedule_resolves_slot_and_stamp_mismatch(offline_driver, slot_offset, stamp_offset):
    svc, wall, mono, claims = offline_driver
    stamp = wall[0]
    slot = stamp//lifecycle.SLOT_US
    svc.schedule_cycle(slot+slot_offset, stamp+stamp_offset)
    assert not claims and not svc.store.database.snapshot_calls
    assert svc.store.database.commits == 0
    if slot_offset:
        assert svc.state.job is None and svc.admission_job is None
        assert not svc.publishing
        trace, = entries(svc)
        assert (trace["slot"], trace["outcome"], trace["rpc_count"]) == (slot, "missed", 0)
        assert not trace["unknown_outcome"]
    else:
        assert svc.state.job.slot == slot
        # Pending ownership is the actual slot's day, never the caller's stamp.
        assert svc.publishing == {lifecycle.day_name(stamp)}
    svc.publish_cycle(slot, stamp)
    assert claims == ([] if slot_offset else [slot])  # No retry of a missed slot.
    assert svc.state.job is None and not svc.publishing
    accounting(svc)
    wall[0], mono[0] = stamp+lifecycle.SLOT_US, 104.
    svc.store.database.server_ns = wall[0]*1000
    svc.publish_cycle(slot+1, wall[0])
    assert claims == ([slot+1] if slot_offset else [slot, slot+1])
    # Replaying either call cannot create another attempt or another trace.
    svc.publish_cycle(slot+slot_offset, stamp+stamp_offset)
    svc.publish_cycle(slot+1, wall[0])
    assert claims == ([slot+1] if slot_offset else [slot, slot+1])
    day, = svc.state.days
    assert (day.scheduled, day.missed, day.attempted, day.published) == (
        (2, 1, 1, 1) if slot_offset else (2, 0, 2, 2))
    assert len(entries(svc)) == 2 and not day.publishers
    assert svc.state.job is None and svc.admission_job is None and not svc.publishing
    assert svc.publisher_faults == 0
    accounting(svc)
