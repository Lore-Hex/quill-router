"""Leased booking uses configuration, never the scope's zero key_shard."""
from __future__ import annotations

import contextvars
import copy
import hashlib
import threading
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.exceptions import DeadlineExceeded

from tests.test_fastpath_pilot_route import (
    _body,
    _pilot,
    _request,
    _seed,
    _settle_body,
)
from tests.test_fastpath_pilot_store import _authorize, _funded_workspace, _store
from tests.test_fastpath_streams_terminals import (
    REAPED,
    _expire,
    _Fleet,
    _leased_stream,
    fleet,  # noqa: F401 - pytest fixture
)
from trusted_router import storage_gcp
from trusted_router.routes.internal import gateway
from trusted_router.services import settle_outbox_drain
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE, leased_key_usage_shard
from trusted_router.storage_gcp_key_shard_admin import reshard_key_usage
from trusted_router.storage_gcp_leased_key_usage import (
    CACHE_MAX_ENTRIES,
    CACHE_TTL_SECONDS,
    READ_DEADLINE_SECONDS,
    RETRY_SECONDS,
    WAIT_SECONDS,
    LeasedKeyUsageShardCache,
)


@pytest.fixture(autouse=True)
def _allow_cold_thread_scheduling(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ordinary tests wait for their tiny read rather than racing the production
    # 100 ms cold fallback. Wait-specific tests supply controlled Events.
    from trusted_router import storage_gcp_leased_key_usage as cache_module
    monkeypatch.setattr(cache_module, 'WAIT_SECONDS', 5.0)


def _expected(aid: str, n: int) -> int:
    return int(hashlib.sha256(aid.encode('utf-8')).hexdigest()[:16], 16) % n


@pytest.mark.parametrize('aid', ['', 'auth-leased-1', 'gwa-fixed-200', 'clé-東京'])
@pytest.mark.parametrize('n', [1, 2, 16, 64])
def test_shard_vectors(aid: str, n: int) -> None:
    result = leased_key_usage_shard(aid, n)
    assert result == _expected(aid, n)
    assert 0 <= result < n
    if n == 1:
        assert result == 0


@pytest.mark.parametrize('n', [0, -1, 65, True, None, 'bad'])
def test_invalid_shard_count(n: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        leased_key_usage_shard('auth', n)


def _shards(store: Any, db: Any, key: Any, n: int) -> None:
    key.usage_shard_count = n
    store._write_entity('api_key', key.hash, key)
    original = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    for shard in range(1, n):
        db.typed[KEY_LIMIT_TABLE][(key.hash, shard)] = {**original, 'shard': shard}


def _setup(n: int = 16) -> tuple[Any, Any, Any, Any]:
    store, db = _store()
    workspace, key = _funded_workspace(store)
    _shards(store, db, key, n)
    return store, db, workspace, key


def _settle(store: Any, workspace: Any, key: Any, aid: str, *, success: bool = True, usage: str = 'Credits') -> None:
    _, auth = _authorize(store, workspace, key, envelope='sealed', authorization_id=aid)
    assert auth is not None
    result = store.typed_finalize_leased_result(
        aid, success=success, actual_microdollars=700, selected_usage_type=usage,
        authorization_snapshot=auth,
    )
    assert result is not None and result[0].finalized


def _assert_booking(db: Any, key: Any, aid: str, n: int, cost: int, *, byok: bool = False) -> None:
    target = _expected(aid, n)
    rows = [db.typed[KEY_LIMIT_TABLE][(key.hash, s)] for s in range(n)]
    for shard, row in enumerate(rows):
        amount = cost if shard == target else 0
        assert row['usage'] == (0 if byok else amount)
        assert row['byok_usage'] == (amount if byok else 0)
        assert row['reserved'] == 0
        assert (row['day_usage'], row['week_usage'], row['month_usage']) == (amount,) * 3
    assert sum(r['usage'] + r['byok_usage'] for r in rows) == cost


@pytest.mark.parametrize('usage', ['Credits', 'BYOK'])
def test_success_books_exact_derived_shard(usage: str) -> None:
    store, db, workspace, key = _setup()
    aid = 'auth-leased-1'
    assert _expected(aid, 16) != 0
    before = copy.deepcopy(db.typed[KEY_LIMIT_TABLE][(key.hash, 0)])
    _settle(store, workspace, key, aid, usage=usage)
    _assert_booking(db, key, aid, 16, 700, byok=usage == 'BYOK')
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)] == before
    scope = next(iter(db.reservations.values()))
    assert scope['key_shard'] == scope['key_reserved_micro'] == 0


def test_spread_of_200_leased_settles() -> None:
    store, db, workspace, key = _setup()
    ids = [f'leased-spread-{i}' for i in range(200)]
    expected = Counter(_expected(aid, 16) for aid in ids)
    assert len(expected) >= 12
    for aid in ids:
        _settle(store, workspace, key, aid)
    for shard in range(16):
        row = db.typed[KEY_LIMIT_TABLE][(key.hash, shard)]
        assert row['usage'] == row['day_usage'] == row['week_usage'] == row['month_usage'] == expected[shard] * 700
    assert sum(r['usage'] for r in db.typed[KEY_LIMIT_TABLE].values()) == 200 * 700


def test_stale_larger_count_after_real_shrink(caplog: pytest.LogCaptureFixture) -> None:
    store, db, workspace, key = _setup()
    assert store._leased_key_usage_shard_count(key.hash) == 16
    store.update_workspace(workspace.id, billing_paused=True)
    changed = reshard_key_usage(store, key.hash, 4, apply=True)
    assert changed.applied
    assert {s for kh, s in db.typed[KEY_LIMIT_TABLE] if kh == key.hash} == set(range(4))
    aid = next(f'shrink-{i}' for i in range(100) if _expected(f'shrink-{i}', 16) >= 4)
    _settle(store, workspace, key, aid)
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 700
    for column in ('usage', 'day_usage', 'week_usage', 'month_usage'):
        assert sum(r[column] for r in db.typed[KEY_LIMIT_TABLE].values()) == 700
    assert sum('metric=key_usage_shard_fallback_total' in r.message for r in caplog.records) == 1


def test_stale_smaller_count_after_real_split() -> None:
    store, db, workspace, key = _setup(4)
    assert store._leased_key_usage_shard_count(key.hash) == 4
    store.update_workspace(workspace.id, billing_paused=True)
    assert reshard_key_usage(store, key.hash, 16, apply=True).applied
    aid = 'growth-0'
    assert _expected(aid, 4) == 2 and _expected(aid, 16) == 6
    _settle(store, workspace, key, aid)
    _assert_booking(db, key, aid, 4, 700)
    assert all(db.typed[KEY_LIMIT_TABLE][(key.hash, s)]['usage'] == 0 for s in range(4, 16))


@pytest.mark.parametrize('strict', [False, True])
def test_single_and_strict_key(strict: bool) -> None:
    store, db, workspace, key = _setup(1)
    key.budget_strict = strict
    store._write_entity('api_key', key.hash, key)
    _settle(store, workspace, key, 'strict')
    _assert_booking(db, key, 'strict', 1, 700)


def test_refund_neither_reads_configuration_nor_writes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, workspace, key = _setup()
    before = copy.deepcopy(db.typed[KEY_LIMIT_TABLE])
    def forbidden(*args: Any) -> int:
        pytest.fail('refund looked up shard count')
    monkeypatch.setattr(storage_gcp.SpannerStore, '_leased_key_usage_shard_count', forbidden)
    _settle(store, workspace, key, 'refund', success=False)
    assert db.typed[KEY_LIMIT_TABLE] == before


@pytest.mark.parametrize('failure', [RuntimeError('secret'), DeadlineExceeded('secret')])
def test_lookup_failure_settles_outside_transaction(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Exception,
) -> None:
    store, db, workspace, key = _setup()
    now = [0.0]
    store._leased_key_usage_counts = LeasedKeyUsageShardCache(clock=lambda: now[0])
    active = False
    reads = []
    real = storage_gcp.run_in_transaction_with_retry
    def run(database: Any, callback: Any, **kwargs: Any) -> Any:
        def checked(tx: Any) -> Any:
            nonlocal active
            active = True
            try:
                return callback(tx)
            finally:
                active = False
        return real(database, checked, **kwargs)
    def lookup(self: Any, key_hash: str) -> int:
        reads.append(active)
        assert not active, 'configuration read inside settle transaction'
        raise failure
    monkeypatch.setattr(storage_gcp, 'run_in_transaction_with_retry', run)
    monkeypatch.setattr(storage_gcp.SpannerStore, '_read_leased_key_usage_shard_count', lookup)
    for i in range(3):
        _settle(store, workspace, key, f'failure-{i}')
    assert reads == [False]
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 2100
    assert sum(r['usage'] for r in db.typed[KEY_LIMIT_TABLE].values()) == 2100
    assert caplog.text.count('metric=leased_key_usage_shard_lookup_fallback_total') == 1
    assert 'secret' not in caplog.text and key.hash not in caplog.text
    now[0] = RETRY_SECONDS - 0.01
    _settle(store, workspace, key, 'before-retry')
    assert reads == [False]
    now[0] = RETRY_SECONDS
    _settle(store, workspace, key, 'after-retry')
    assert reads == [False, False]
    assert caplog.text.count('metric=leased_key_usage_shard_lookup_fallback_total') == 2
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 3500


def _observe_reads(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    finished = threading.Event()
    real_load = LeasedKeyUsageShardCache._load
    def load(self: Any, *args: Any) -> None:
        try:
            real_load(self, *args)
        finally:
            finished.set()
    monkeypatch.setattr(LeasedKeyUsageShardCache, '_load', load)
    return finished


def test_stalled_lookup_late_result_fills_cache(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from trusted_router import storage_gcp_leased_key_usage as cache_module
    monkeypatch.setattr(cache_module, 'WAIT_SECONDS', WAIT_SECONDS)
    store, db, workspace, key = _setup()
    now = [0.0]
    store._leased_key_usage_counts = LeasedKeyUsageShardCache(clock=lambda: now[0])
    started, release = threading.Event(), threading.Event()
    finished = _observe_reads(monkeypatch)
    reads = []
    # Model the cold wait expiring without a sleep or a real 100 ms wait.
    class ExpiredWait(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            assert timeout is not None and 0 <= timeout <= WAIT_SECONDS
            assert started.wait(1)
            return False
    real_start = LeasedKeyUsageShardCache._start
    def start(self: Any, kh: str, entry: Any, loader: Any) -> None:
        real_start(self, kh, entry, loader)
        entry.done = ExpiredWait()  # _start's caller holds the cache lock.
    def stalled(self: Any, key_hash: str) -> int:
        reads.append(key_hash)
        started.set()
        assert release.wait(1)
        return 16
    monkeypatch.setattr(LeasedKeyUsageShardCache, '_start', start)
    monkeypatch.setattr(storage_gcp.SpannerStore, '_read_leased_key_usage_shard_count', stalled)
    try:
        _settle(store, workspace, key, 'deadline')
        assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 700
        assert not finished.is_set()
        now[0] = 7.0
    finally:
        release.set()
        assert finished.wait(2)
    _settle(store, workspace, key, 'auth-leased-1')
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 12)]['usage'] == 700
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 700
    assert reads == [key.hash]
    assert caplog.text.count('metric=leased_key_usage_shard_lookup_fallback_total') == 1
    assert store._leased_key_usage_counts._entries[key.hash].next_attempt == 7.0 + CACHE_TTL_SECONDS


def test_worker_scheduled_after_600_ms_still_fills_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import storage_gcp_leased_key_usage as cache_module
    store, db, workspace, key = _setup()
    ticks = [0.0]
    # Same cache-only clock model as the reviewer's scheduler probe. The
    # production point read keeps its own real RPC clock and original options.
    monkeypatch.setattr(cache_module, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))
    cache = LeasedKeyUsageShardCache(clock=lambda: ticks[0])
    store._leased_key_usage_counts = cache
    started, release = threading.Event(), threading.Event()
    calls = []
    real_load = LeasedKeyUsageShardCache._load
    def delayed_load(self: Any, *args: Any) -> None:
        calls.append(1)
        ticks[0] = 0.6
        started.set()
        assert release.wait(2)
        real_load(self, *args)
    class ExpiredWait(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            assert started.wait(1)
            return False
    real_start = LeasedKeyUsageShardCache._start
    def start(self: Any, kh: str, entry: Any, loader: Any) -> None:
        real_start(self, kh, entry, loader)
        entry.done = ExpiredWait()
    monkeypatch.setattr(LeasedKeyUsageShardCache, '_load', delayed_load)
    monkeypatch.setattr(LeasedKeyUsageShardCache, '_start', start)
    try:
        _settle(store, workspace, key, 'scheduler-first')
        assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 700
    finally:
        release.set()
        assert cache_module._wait_for_idle_reads(2)
    _settle(store, workspace, key, 'auth-leased-1')
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 12)]['usage'] == 700
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 700
    assert cache._entries[key.hash].next_attempt == 0.6 + CACHE_TTL_SECONDS
    assert calls == [1]


def test_base_exception_retires_attempt_and_propagates() -> None:
    from trusted_router.storage_gcp_leased_key_usage import _Entry
    class CancelRead(BaseException):
        pass
    failure = CancelRead()
    now = [0.0]
    cache = LeasedKeyUsageShardCache(clock=lambda: now[0])
    entry = _Entry()
    with cache._lock:
        cache._entries['key'] = entry
        cache._inflight['key'] = entry
    def cancelled() -> int:
        raise failure
    # Invoke the worker body directly to assert propagation without turning
    # an intentional BaseException into an unhandled-thread pytest warning.
    with pytest.raises(CancelRead) as caught:
        cache._load('key', entry, cancelled)
    assert caught.value is failure
    assert entry.done.is_set()
    assert not cache._inflight
    assert entry.count is None
    assert entry.next_attempt == RETRY_SECONDS
    now[0] = RETRY_SECONDS
    assert cache.get('key', lambda: 16) == 16


@pytest.mark.parametrize('wrapped_load', [False, True])
def test_idle_read_wait_covers_late_worker_and_patched_methods(
    monkeypatch: pytest.MonkeyPatch, wrapped_load: bool,
) -> None:
    from trusted_router import storage_gcp_leased_key_usage as cache_module
    store, db, workspace, key = _setup()
    assert cache_module._wait_for_idle_reads(0)
    slots, replacement = threading.BoundedSemaphore(1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(cache_module, '_READ_SLOTS', slots)
    started, release = threading.Event(), threading.Event()
    wrapper_finished, release_wrapper = threading.Event(), threading.Event()
    class ExpiredWait(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            assert started.wait(1)
            return False
    real_start = LeasedKeyUsageShardCache._start
    def start(self: Any, kh: str, entry: Any, loader: Any) -> None:
        real_start(self, kh, entry, loader)
        entry.done = ExpiredWait()
    def blocked(self: Any, kh: str) -> int:
        started.set()
        assert release.wait(2)
        return 16
    real_load = LeasedKeyUsageShardCache._load
    def load(self: Any, *args: Any) -> None:
        real_load(self, *args)
        wrapper_finished.set()
        assert release_wrapper.wait(2)
    monkeypatch.setattr(LeasedKeyUsageShardCache, '_start', start)
    monkeypatch.setattr(storage_gcp.SpannerStore, '_read_leased_key_usage_shard_count', blocked)
    if wrapped_load:
        monkeypatch.setattr(LeasedKeyUsageShardCache, '_load', load)
    try:
        _settle(store, workspace, key, 'idle-wait')
        assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 700
        assert not cache_module._wait_for_idle_reads(0.001)
        # Teardown may restore this global while the original worker exists.
        monkeypatch.setattr(cache_module, '_READ_SLOTS', replacement)
        release.set()
        if wrapped_load:
            assert wrapper_finished.wait(1)
            assert not cache_module._wait_for_idle_reads(0.001)
    finally:
        release.set()
        release_wrapper.set()
        assert cache_module._wait_for_idle_reads(2)
    # Public semaphore operations prove the correct slot was released exactly
    # once, without inspecting semaphore internals or assuming its identity.
    for semaphore in (slots, replacement):
        assert semaphore.acquire(blocking=False)
        assert not semaphore.acquire(blocking=False)
        semaphore.release()


def test_idle_read_wait_after_thread_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import storage_gcp_leased_key_usage as cache_module
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(cache_module, '_READ_SLOTS', slots)
    def cannot_start(self: Any) -> None:
        raise RuntimeError('thread unavailable')
    monkeypatch.setattr(threading.Thread, 'start', cannot_start)
    cache = LeasedKeyUsageShardCache()
    assert cache.get('start-failure', lambda: 16) == 1
    assert cache_module._wait_for_idle_reads(0)
    assert slots.acquire(blocking=False)
    slots.release()


def test_expired_entry_settles_without_waiting_for_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, workspace, key = _setup()
    now = [0.0]
    cache = LeasedKeyUsageShardCache(clock=lambda: now[0])
    store._leased_key_usage_counts = cache
    assert cache.get(key.hash, lambda: 16) == 16
    now[0] = CACHE_TTL_SECONDS
    started, release = threading.Event(), threading.Event()
    finished = _observe_reads(monkeypatch)
    reads = []
    class NoRefreshWait(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            assert release.is_set(), 'expired entry waited for its blocked refresh'
            return super().wait(timeout)
    real_start = LeasedKeyUsageShardCache._start
    def start(self: Any, kh: str, entry: Any, loader: Any) -> None:
        real_start(self, kh, entry, loader)
        entry.done = NoRefreshWait()
    def blocked(self: Any, kh: str) -> int:
        reads.append(kh)
        started.set()
        assert release.wait(1)
        return 4
    monkeypatch.setattr(LeasedKeyUsageShardCache, '_start', start)
    monkeypatch.setattr(storage_gcp.SpannerStore, '_read_leased_key_usage_shard_count', blocked)
    try:
        _settle(store, workspace, key, 'growth-0')
        assert started.wait(1)
        assert not finished.is_set()
        assert db.typed[KEY_LIMIT_TABLE][(key.hash, 6)]['usage'] == 700
    finally:
        release.set()
        assert finished.wait(2)
    _settle(store, workspace, key, 'auth-leased-1')
    assert _expected('auth-leased-1', 4) == 0
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 700
    assert reads == [key.hash]


@pytest.mark.parametrize('failure', [RuntimeError('secret'), DeadlineExceeded('secret'), None, 65])
def test_refresh_failure_keeps_last_good_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Any,
) -> None:
    store, db, workspace, key = _setup()
    now = [0.0]
    cache = LeasedKeyUsageShardCache(clock=lambda: now[0])
    store._leased_key_usage_counts = cache
    assert cache.get(key.hash, lambda: 16) == 16
    finished = _observe_reads(monkeypatch)
    reads = []
    def lookup(self: Any, kh: str) -> Any:
        reads.append(kh)
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(storage_gcp.SpannerStore, '_read_leased_key_usage_shard_count', lookup)
    now[0] = CACHE_TTL_SECONDS
    _settle(store, workspace, key, 'auth-leased-1')
    assert finished.wait(1)
    _settle(store, workspace, key, 'growth-0')
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 12)]['usage'] == 700
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 6)]['usage'] == 700
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 0
    assert reads == [key.hash]
    assert caplog.text.count('metric=leased_key_usage_shard_refresh_failed_total') == 1
    assert 'lookup_fallback_total' not in caplog.text
    assert 'secret' not in caplog.text and key.hash not in caplog.text
    now[0] += RETRY_SECONDS - 0.01
    assert cache.get(key.hash, lambda: 4) == 16
    assert reads == [key.hash]
    finished.clear()
    now[0] = CACHE_TTL_SECONDS + RETRY_SECONDS
    _settle(store, workspace, key, 'refresh-retry')
    assert finished.wait(1)
    assert reads == [key.hash, key.hash]
    assert caplog.text.count('metric=leased_key_usage_shard_refresh_failed_total') == 2
    assert cache.get(key.hash, lambda: 4) == 16


@pytest.mark.parametrize('malformed', [None, 0, 65, True, 'bad'])
def test_missing_or_malformed_configuration_falls_back(malformed: Any) -> None:
    store, db, workspace, key = _setup()
    if malformed is None:
        store.api_keys.delete(key.hash)
    else:
        key.usage_shard_count = malformed
        store._write_entity('api_key', key.hash, key)
    _settle(store, workspace, key, 'malformed')
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]['usage'] == 700


def test_cache_ttl_and_one_lookup_across_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, workspace, key = _setup()
    now = [0.0]
    store._leased_key_usage_counts = LeasedKeyUsageShardCache(clock=lambda: now[0])
    finished = _observe_reads(monkeypatch)
    reads = []
    real = storage_gcp.SpannerStore._read_leased_key_usage_shard_count
    def lookup(self: Any, key_hash: str) -> int:
        reads.append(key_hash)
        return real(self, key_hash)
    monkeypatch.setattr(storage_gcp.SpannerStore, '_read_leased_key_usage_shard_count', lookup)
    for i in range(10):
        _settle(store, workspace, key, f'cached-{i}')
    assert finished.wait(1)
    assert reads == [key.hash]
    finished.clear()
    now[0] = CACHE_TTL_SECONDS
    _settle(store, workspace, key, 'after-ttl')
    assert finished.wait(1)
    assert reads == [key.hash, key.hash]


def test_cache_lru_evicts_and_new_key_gets_lookup() -> None:
    assert CACHE_MAX_ENTRIES == 10_000
    cache = LeasedKeyUsageShardCache(max_entries=3)
    reads = []
    def load() -> int:
        reads.append(1)
        return 16
    for kh in ('a', 'b', 'c', 'a', 'd'):
        assert cache.get(kh, load) == 16
        assert len(cache._entries) <= 3
    assert list(cache._entries) == ['c', 'a', 'd']
    assert len(reads) == 4
    assert cache.get('b', load) == 16
    assert list(cache._entries) == ['a', 'd', 'b']
    assert len(reads) == 5


def test_evicted_inflight_read_is_shared_and_never_reinserts(monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    cache = LeasedKeyUsageShardCache(max_entries=1)
    started, release = threading.Event(), threading.Event()
    finished = _observe_reads(monkeypatch)
    reads = []
    def blocked() -> int:
        reads.append(1)
        started.set()
        assert release.wait(1)
        return 16
    # Expire only a's cold wait explicitly; b/c still get the generous
    # ordinary-test wait. Eviction behavior does not need a real timeout.
    class ExpiredWait(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            assert started.wait(1)
            return False
    real_start = LeasedKeyUsageShardCache._start
    def start(self: Any, kh: str, entry: Any, loader: Any) -> None:
        real_start(self, kh, entry, loader)
        if kh == 'a':
            entry.done = ExpiredWait()  # Caller holds the cache lock.
    monkeypatch.setattr(LeasedKeyUsageShardCache, '_start', start)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(cache.get, 'a', blocked)
        try:
            assert started.wait(1)
            assert cache.get('b', lambda: 4) == 4
            assert list(cache._entries) == ['b']
            # This concurrent caller shares the evicted, still-running read.
            assert cache.get('a', blocked) == 1
            assert cache.get('c', lambda: 2) == 2
            assert list(cache._entries) == ['c']
            finished.clear()
        finally:
            release.set()
        assert finished.wait(1)
        assert first.result(timeout=1) in (1, 16)
    assert reads == [1]
    assert list(cache._entries) == ['c']
    assert not cache._inflight


def test_no_read_slot_preserves_good_count_and_retries_cold(caplog: pytest.LogCaptureFixture) -> None:
    from trusted_router.storage_gcp_leased_key_usage import _READ_SLOTS
    now = [0.0]
    cache = LeasedKeyUsageShardCache(clock=lambda: now[0])
    assert cache.get('warm', lambda: 16) == 16
    now[0] = CACHE_TTL_SECONDS
    def forbidden() -> int:
        pytest.fail('lookup without a read slot')
    acquired = 0
    try:
        for _ in range(8):
            assert _READ_SLOTS.acquire(timeout=1)
            acquired += 1
        for _ in range(3):
            assert cache.get('warm', forbidden) == 16
            assert cache.get('cold', forbidden) == 1
        assert caplog.text.count('metric=leased_key_usage_shard_refresh_failed_total') == 1
        assert caplog.text.count('metric=leased_key_usage_shard_lookup_fallback_total') == 1
    finally:
        for _ in range(acquired):
            _READ_SLOTS.release()
    now[0] += RETRY_SECONDS
    assert cache.get('cold', lambda: 2) == 2
    assert cache.get('warm', lambda: 4) == 16
    assert cache._entries['warm'].done.wait(1)
    assert cache.get('warm', forbidden) == 4


def test_stale_point_read_has_short_deadline_and_no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, workspace, key = _setup()
    snapshots, options, deadlines, contexts = [], [], [], []
    from trusted_router import storage_gcp_io
    real_deadline = storage_gcp_io.spanner_rpc_deadline
    def record_deadline(deadline: float, **kwargs: Any) -> Any:
        deadlines.append((deadline, storage_gcp_io._SPANNER_RPC_DEADLINE.get()))
        return real_deadline(deadline, **kwargs)
    monkeypatch.setattr(storage_gcp_io, 'spanner_rpc_deadline', record_deadline)
    marker = contextvars.ContextVar("leased_test_caller", default=False)
    real_snapshot = db.snapshot
    def snapshot(**kwargs: Any) -> Any:
        snapshots.append(kwargs)
        contexts.append(marker.get())
        snap = real_snapshot(**kwargs)
        execute = snap.execute_sql
        def sql(query: str, **kwargs: Any) -> Any:
            options.append(kwargs)
            return execute(query, **kwargs)
        snap.execute_sql = sql
        return snap
    monkeypatch.setattr(db, 'snapshot', snapshot)
    token = marker.set(True)
    caller_deadline = time.monotonic() + 0.05
    deadline_token = storage_gcp_io._SPANNER_RPC_DEADLINE.set(caller_deadline)
    try:
        assert store._leased_key_usage_shard_count(key.hash) == 16
    finally:
        storage_gcp_io._SPANNER_RPC_DEADLINE.reset(deadline_token)
        marker.reset(token)
    assert contexts == [False]
    assert len(deadlines) == 1
    assert deadlines[0][1] is None
    assert deadlines[0][0] > caller_deadline
    assert READ_DEADLINE_SECONDS == 0.5
    assert len(snapshots) == len(options) == 1
    assert snapshots[0]['exact_staleness'].total_seconds() == 5
    assert options[0]['params'] == {'kind': 'api_key', 'id': key.hash}
    assert options[0]['timeout'] == READ_DEADLINE_SECONDS
    assert options[0]['retry'] is None
    assert options[0]['request_options'] == {'priority': 'PRIORITY_LOW'}


def test_metadata_edit_cannot_restore_pre_split_count(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, workspace, key = _setup(4)
    store.update_workspace(workspace.id, billing_paused=True)
    stale = store.api_keys.get_by_hash(key.hash)
    assert reshard_key_usage(store, key.hash, 16, apply=True).applied
    # The metadata edit's initial snapshot preceded the split commit.
    monkeypatch.setattr(store.api_keys, 'get_by_hash', lambda _kh: stale)
    updated = store.api_keys.update(key.hash, {'name': 'renamed'})
    assert updated.usage_shard_count == 16
    assert updated.name == 'renamed'
    assert {s for kh, s in db.typed[KEY_LIMIT_TABLE] if kh == key.hash} == set(range(updated.usage_shard_count))


@pytest.mark.parametrize('caller', ['sync', 'outbox', 'reaper'])
def test_real_callers_book_derived_shard(fleet: _Fleet, caller: str) -> None:  # noqa: F811
    store, db, key = _seed()
    _shards(store, db, key, 16)
    if caller == 'reaper':
        authorized = _leased_stream(store, db, key)
        authorization = store.get_gateway_authorization(authorized['authorization_id'])
        _expire(db, authorization)
        fleet.answers['/v1/disposition'] = (200, REAPED)
        settle_outbox_drain.drain_settle_outbox(10)
        cost = 55
    else:
        authorized = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())['data']
        if caller == 'outbox':
            fleet.answers['/v1/settle'] = (200, {'Status': 'recorded'})
        gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=_pilot())
        if caller == 'outbox':
            row = next(iter(db.settle_outbox.values()))
            row['next_attempt_at'] = '2020-01-01T00:00:00Z'
            fleet.answers['/v1/settle'] = (200, {'Status': 'won', 'Kind': 'settle', 'Charge': 306})
            settle_outbox_drain.drain_settle_outbox(10)
        cost = 306
    assert store.get_gateway_authorization(authorized['authorization_id']).settled
    _assert_booking(db, key, authorized['authorization_id'], 16, cost)


def test_cache_coalesces_concurrent_misses() -> None:
    from concurrent.futures import ThreadPoolExecutor
    cache = LeasedKeyUsageShardCache()
    started, release = threading.Event(), threading.Event()
    calls = []
    def load() -> int:
        calls.append(1)
        started.set()
        assert release.wait(1)
        return 16
    with ThreadPoolExecutor(max_workers=8) as executor:
        first = executor.submit(cache.get, 'key', load)
        assert started.wait(1)
        others = [executor.submit(cache.get, 'key', load) for _ in range(7)]
        release.set()
        assert [future.result() for future in [first, *others]] == [16] * 8
    assert calls == [1]


def test_retry_reuses_shard_without_another_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fakes.spanner import FakeAborted
    store, db, workspace, key = _setup()
    _, auth = _authorize(store, workspace, key, envelope='sealed', authorization_id='retry')
    assert auth is not None
    real = storage_gcp.run_in_transaction_with_retry
    calls, reads = [], []
    def run(database: Any, callback: Any, **kwargs: Any) -> Any:
        def checked(tx: Any) -> Any:
            result = callback(tx)
            calls.append(1)
            if len(calls) == 1:
                raise FakeAborted()
            return result
        return real(database, checked, **kwargs)
    def lookup(self: Any, key_hash: str) -> int:
        assert not calls
        reads.append(key_hash)
        return 16
    monkeypatch.setattr(storage_gcp, 'run_in_transaction_with_retry', run)
    monkeypatch.setattr(storage_gcp.SpannerStore, '_read_leased_key_usage_shard_count', lookup)
    result = store.typed_finalize_leased_result(
        auth.id, success=True, actual_microdollars=700, selected_usage_type='Credits', authorization_snapshot=auth,
    )
    assert result is not None
    assert len(calls) == 2 and reads == [key.hash]
    _assert_booking(db, key, auth.id, 16, 700)
