"""Cold-start and cadence regressions through the production publisher owner."""
import asyncio
from dataclasses import replace

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_health_contention import outcome
from tests.test_async_settle_shadow_health_stagger import advance, timed_owner
from tests.test_async_settle_shadow_health_watchdog import accounting
from tests.test_async_settle_shadow_observation import deterministic_scheduler  # noqa: F401
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


def test_early_next_wake_waits_for_actual_start_cadence(monkeypatch):
    svc, wall, mono, job, waits = timed_owner(monkeypatch)
    # Receipt 896295396: published start 8,633 us into the slot, cycle 97,787 us.
    mono[0] += .008633
    wall[0] += 8633
    svc.incumbent_slot = job.slot-1
    svc.submit_publisher(job.slot, wall[0])
    outcome(svc, job, 'published')
    first_start = svc.state.last_actual_start
    next_job = advance(svc, wall, mono, job.slot+1)
    assert next_job.earliest_start_mono > mono[0]
    svc.submit_publisher(next_job.slot, wall[0])
    trace = outcome(svc, next_job, 'published')
    assert waits == pytest.approx([.008633])
    assert svc.state.last_actual_start-first_start >= 2
    assert trace['claim_start_us'] % lifecycle.SLOT_US == 8633
    assert trace['cycle_us'] == 0  # Cadence wait precedes the actual admitted start.


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
    outcome(svc, job, 'lost_claim' if gap <= 2 else 'published')
    assert svc.store.database.commits == (0 if gap <= 2 else 2)
    assert waits == [shell.follower_stagger(svc.journal.boot, job.slot, gap <= 2)]


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
