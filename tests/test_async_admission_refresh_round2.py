"""Deterministic read-ahead proofs; no wall-clock sleeps or duration assertions."""
from __future__ import annotations

import logging
import threading
from contextvars import copy_context
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from fastapi import FastAPI

from tests.test_async_settle_drain import healthy
from trusted_router import async_settle_timing as timing
from trusted_router.config import Settings
from trusted_router.services import async_settle_admission_refresh as refresh
from trusted_router.services.async_settle import (
    Admission,
    AdmissionCache,
    DrainHealth,
    decode_health,
)


@pytest.fixture
def rig():
    now = [0.0]
    health = Mock(side_effect=lambda: healthy(observed_at=100 + now[0], worker_heartbeat=100 + now[0]))
    read = Mock(return_value=Admission(0, 2))
    cache = AdmissionCache(read, health_read=health, clock=lambda: now[0],
                           wall_clock=lambda: 100 + now[0])
    return SimpleNamespace(now=now, health=health, read=read, cache=cache,
                           worker=refresh.AdmissionRefresher(cache))


def warm(rig):
    assert rig.cache.eligible('ws', 0)
    assert rig.read.call_args_list == [call('ws')]
    assert rig.health.call_args_list == [call()]


def test_read_ahead_serves_fresh_request_without_reads(rig):
    warm(rig)
    rig.now[0] = 3
    rig.worker.refresh_once(3)
    assert rig.cache.health_read_at == 3
    assert rig.cache.entries['ws'][0] == 3
    rig.now[0] = 3.1
    assert rig.cache.eligible('ws', 0)
    assert rig.read.call_args_list == [call('ws'), call('ws')]
    assert rig.health.call_args_list == [call(), call()]


def test_stalled_refresher_keeps_one_second_discovery(rig):
    warm(rig)
    rig.health.side_effect = lambda: None
    rig.now[0] = .999
    assert rig.cache.eligible('ws', 0)
    rig.now[0] = 1
    assert not rig.cache.eligible('ws', 0)
    assert rig.health.call_args_list == [call(), call()]
    assert rig.read.call_args_list == [call('ws')]


@pytest.mark.parametrize('failure', ['raise', 'invalid'])
@pytest.mark.parametrize('field', ['health', 'workspace'])
def test_failed_refresh_preserves_evidence_and_expiry(rig, failure, field):
    warm(rig)
    previous_health = rig.cache.health
    previous_entry = rig.cache.entries['ws']
    reader = rig.health if field == 'health' else rig.read
    reader.side_effect = RuntimeError('unavailable') if failure == 'raise' else None
    reader.return_value = None
    rig.now[0] = .5 if field == 'health' else 3
    rig.worker.refresh_once()
    if field == 'health':
        assert rig.cache.health is previous_health
        assert rig.cache.health_read_at == 0
        rig.now[0] = .999
        assert rig.cache.eligible('ws', 0)
        assert reader.call_count == 2
        rig.now[0] = 1
    else:
        assert rig.cache.entries['ws'] is previous_entry
        rig.now[0] = 4.999
        assert rig.cache.eligible('ws', 0)
        assert reader.call_count == 2
        rig.now[0] = 5
    assert not rig.cache.eligible('ws', 0)
    assert reader.call_count == 3


@pytest.mark.parametrize('field', ['health', 'workspace'])
def test_older_refresh_cannot_overwrite_request_read(rig, field):
    warm(rig)
    rig.now[0] = .5 if field == 'health' else 3
    reader = rig.health if field == 'health' else rig.read
    newer = []

    def racing_read(*args):
        # The refresher must release the cache lock before calling the reader.
        assert rig.cache.lock.acquire(blocking=False)
        rig.cache.lock.release()
        rig.now[0] = 1 if field == 'health' else 5
        reader.side_effect = None
        reader.return_value = (healthy(observed_at=101, worker_heartbeat=101)
                               if field == 'health' else Admission(9, 3))
        assert rig.cache.eligible('ws', 0)
        newer.append((rig.cache.health_read_at, rig.cache.health) if field == 'health'
                     else rig.cache.entries['ws'])
        return healthy() if field == 'health' else Admission(0, 2)

    reader.side_effect = racing_read
    rig.worker.refresh_once()
    assert newer
    assert ((rig.cache.health_read_at, rig.cache.health) if field == 'health'
            else rig.cache.entries['ws']) == newer[0]


def test_activity_gate_idle_then_request_resumes_reads(rig):
    rig.worker.refresh_once(0)
    assert rig.health.call_count == rig.read.call_count == 0
    warm(rig)
    rig.now[0] = 1800
    rig.worker.refresh_once()
    assert rig.health.call_count == rig.read.call_count == 1
    assert rig.cache.eligible('ws', 0)
    assert rig.health.call_count == rig.read.call_count == 2
    rig.now[0] = 1803
    rig.worker.refresh_once()
    assert rig.health.call_count == rig.read.call_count == 3


def test_only_recent_existing_workspaces_are_refreshed(rig):
    warm(rig)
    rig.cache.entries['never-evaluated'] = (0, Admission(0, 2))
    rig.now[0] = 1799
    assert rig.cache.eligible('recent', 0)
    assert rig.cache.eligible('removed', 0)
    del rig.cache.entries['removed']
    rig.now[0] = 1802
    rig.worker.refresh_once()
    assert rig.read.call_args_list == [call('ws'), call('recent'), call('removed'), call('recent')]
    assert set(rig.cache.entries) == {'ws', 'recent', 'never-evaluated'}
    assert rig.cache.entries['ws'][0] == rig.cache.entries['never-evaluated'][0] == 0


def test_removed_workspace_is_not_reinserted_after_read(rig):
    warm(rig)
    rig.now[0] = 3
    def remove(ws):
        del rig.cache.entries[ws]
        return Admission(0, 2)
    rig.read.side_effect = remove
    rig.worker.refresh_once()
    assert 'ws' not in rig.cache.entries


@pytest.mark.parametrize('failure', [False, True])
def test_many_passes_obey_attempt_rate_even_on_failure(rig, failure):
    warm(rig)
    if failure:
        rig.health.side_effect = RuntimeError('health')
        rig.read.side_effect = RuntimeError('workspace')
    for tick in range(1, 601):
        rig.now[0] = tick / 100
        rig.worker.refresh_once()
    assert rig.health.call_count == 13  # cold + 0.5, 1.0, ... 6.0
    assert rig.read.call_args_list == [call('ws')] * 3  # cold + 3, 6


@pytest.mark.parametrize('field', ['health', 'workspace'])
def test_blocked_refresh_does_not_hold_request_lock(rig, field):
    warm(rig)
    rig.now[0] = .5 if field == 'health' else 3
    if field == 'workspace':
        rig.cache.health_read_at = 2.9
        rig.cache.health = decode_health(healthy(observed_at=103, worker_heartbeat=103), now=3, wall=103)
    entered, release = threading.Event(), threading.Event()
    reader = rig.health if field == 'health' else rig.read
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return healthy(observed_at=103, worker_heartbeat=103) if field == 'health' else Admission(0, 2)
    reader.side_effect = blocked
    thread = threading.Thread(target=rig.worker.refresh_once)
    thread.start()
    try:
        assert entered.wait(5)
        assert rig.cache.eligible('ws', 0)
        assert not release.is_set()
        # An overlapping pass neither reads nor waits on the blocked pass.
        rig.worker.refresh_once()
        assert reader.call_count == 2
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize('projection', [False, True])
def test_background_read_is_absent_from_request_timing(rig, caplog, projection):
    warm(rig)
    rig.cache.read = timing.read(rig.read, 'workspace')
    rig.cache.health_read = timing.read(rig.health, 'health')
    rig.cache.lock = timing.AdmissionLock(rig.cache.lock)
    rig.now[0] = 3
    original = rig.cache.eligible
    def during_request(ws, cap):
        # Even a deliberately copied request context cannot attribute the read.
        thread = threading.Thread(target=copy_context().run, args=(rig.worker.refresh_once,))
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        return original(ws, cap)
    rig.cache.eligible = during_request
    caplog.set_level(logging.INFO, logger=timing.__name__)
    if projection:
        assert timing.eligible(rig.cache, 'ws', 0, projection=True)
    else:
        with timing.handoff_scope(timing.monotonic(), None, enabled=True):
            assert timing.eligible(rig.cache, 'ws', 0)
    lines = [record.message for record in caplog.records if 'metric=async_settle_' in record.message]
    assert len(lines) == 1
    assert 'health=none health_ms=0.000 workspace=none workspace_ms=0.000' in lines[0]
    assert rig.health.call_count == rig.read.call_count == 2


@pytest.mark.parametrize('enabled,cache_kind,expected', [
    (False, 'health', 0), (True, 'health', 1), (True, 'no-health', 0), (True, 'absent', 0),
])
async def test_application_lifecycle_starts_only_eligible_worker(monkeypatch, rig, enabled, cache_kind, expected):
    from trusted_router import main
    from trusted_router.services import async_settle

    created = []
    base = refresh.AdmissionRefresher
    class Observed(base):
        def __init__(self, cache):
            super().__init__(cache)
            self.thread.join = Mock(wraps=self.thread.join)
            created.append(self)
    monkeypatch.setattr(refresh, 'AdmissionRefresher', Observed)
    if cache_kind == 'no-health':
        rig.cache.health_read = None
    runtime = SimpleNamespace(admission=None if cache_kind == 'absent' else rig.cache)
    monkeypatch.setattr(async_settle, 'load_runtime', lambda *args: runtime)
    config = Settings(environment='test', async_settle_admission_refresh_enabled=enabled)
    app = main.create_app(config, configure_store_arg=False, init_observability=False)
    assert created == []  # construction never starts a worker
    if not enabled:
        assert not any(hook.__module__ == refresh.__name__
                       for hook in app.router.on_startup + app.router.on_shutdown)
    async with app.router.lifespan_context(app):
        for hook in app.router.on_startup:
            if hook.__module__ == refresh.__name__:
                await hook()  # idempotent startup hook
        assert len(created) == expected
        if expected:
            assert created[0].thread.daemon and created[0].thread.is_alive()
    for worker in created:
        assert worker.stop_event.is_set()
        worker.thread.join.assert_called_once_with(timeout=2.0)
        assert not worker.thread.is_alive()


async def test_start_failure_cannot_escape_startup(monkeypatch, rig):
    app = FastAPI()
    app.state.async_settle = SimpleNamespace(admission=rig.cache)
    refresh.install(app, Settings(async_settle_admission_refresh_enabled=True))
    monkeypatch.setattr(threading.Thread, 'start', Mock(side_effect=RuntimeError('cannot start')))
    async with app.router.lifespan_context(app):
        pass


def test_raising_read_does_not_stop_worker_or_reach_request(rig):
    warm(rig)
    permits, finished = Queue(), Queue()
    class SteppedEvent:
        stopped = False
        def wait(self, timeout):
            finished.put(True)
            permits.get(timeout=5)
            return self.stopped
        def is_set(self):
            return self.stopped
        def set(self):
            self.stopped = True
            permits.put(True)
    rig.worker.stop_event = SteppedEvent()
    rig.worker.thread.start()
    try:
        finished.get(timeout=5)
        rig.now[0] = .5
        rig.health.side_effect = RuntimeError('read failure')
        permits.put(True)
        finished.get(timeout=5)
        assert rig.health.call_count == 2
        assert rig.worker.thread.is_alive()
        assert rig.cache.eligible('ws', 0)
        rig.now[0] = 1
        rig.health.side_effect = lambda: healthy(observed_at=101, worker_heartbeat=101)
        permits.put(True)
        finished.get(timeout=5)
        assert rig.health.call_count == 3
        assert rig.cache.health_read_at == 1
    finally:
        rig.worker.close()
    assert not rig.worker.thread.is_alive()


def test_setting_defaults_off_and_env_enables(monkeypatch):
    monkeypatch.delenv('TR_ASYNC_SETTLE_ADMISSION_REFRESH_ENABLED', raising=False)
    assert Settings().async_settle_admission_refresh_enabled is False
    monkeypatch.setenv('TR_ASYNC_SETTLE_ADMISSION_REFRESH_ENABLED', 'true')
    assert Settings().async_settle_admission_refresh_enabled is True


@pytest.mark.parametrize('value', [None, Admission(-1, 2), Admission(True, 2), Admission(0, True)])
def test_invalid_workspace_refresh_installs_nothing(rig, value):
    warm(rig)
    previous = rig.cache.entries['ws']
    rig.read.return_value = value
    rig.now[0] = 3
    rig.worker.refresh_once()
    assert rig.cache.entries['ws'] is previous


@pytest.mark.parametrize('overrides', [
    {'complete': False}, {'observed_at': 99, 'worker_heartbeat': 99}, {'sample_count': True},
])
def test_invalid_health_refresh_uses_existing_decoder(rig, overrides):
    warm(rig)
    previous = rig.cache.health
    rig.health.side_effect = None
    rig.health.return_value = healthy(**overrides)
    rig.now[0] = 4
    rig.worker.refresh_once()
    assert rig.cache.health is previous
    assert rig.cache.health_read_at == 0


def test_unhealthy_health_refresh_replaces_evidence(rig):
    # A valid, complete, fresh record whose drain p95 exceeds 5 s is an
    # observation, not an invalid read: read-ahead installs it, and admission
    # rejects it, exactly as the full path rejects the same record.
    warm(rig)
    rig.health.side_effect = None
    unhealthy = healthy(p95_age_seconds=6, oldest_unresolved_age_seconds=6)
    rig.health.return_value = unhealthy
    rig.now[0] = 4
    rig.worker.refresh_once()
    assert rig.cache.health == DrainHealth(0, 6)
    assert rig.cache.health_read_at == 4
    assert decode_health(unhealthy, now=4, wall=104) is None
    assert not rig.cache.eligible('ws', 0, cached_only=True)
    assert not rig.cache.eligible('ws', 0)


def test_slow_reads_use_start_timestamp_and_exact_health_decoder(rig):
    warm(rig)
    rig.now[0] = 3
    def slow_health():
        rig.now[0] = 3.2
        return healthy(observed_at=102.9, worker_heartbeat=103)
    def slow_workspace(ws):
        rig.now[0] = 3.4
        return Admission(0, 2)
    rig.health.side_effect = slow_health
    rig.read.side_effect = slow_workspace
    rig.worker.refresh_once()
    assert rig.cache.health_read_at == 3
    assert rig.cache.health == decode_health(healthy(observed_at=102.9, worker_heartbeat=103),
                                            now=3.2, wall=103.2)
    assert rig.cache.entries['ws'][0] == 3.2
    # No rejuvenation of the health read cadence by its completion time.
    rig.now[0] = 4
    rig.health.side_effect = None
    rig.health.return_value = None
    assert not rig.cache.eligible('ws', 0)
    assert rig.health.call_count == 3
