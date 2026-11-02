"""Deterministic observations through the real route, handler and fake Spanner."""
# ruff: noqa: F811 - shared env fixture
from __future__ import annotations

import copy
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException
from google.api_core.exceptions import DeadlineExceeded
from starlette.requests import Request

from tests.fakes.spanner import _FakeTransaction
from tests.test_async_settle_drain import healthy
from tests.test_async_settle_handler import NOW, call, env, prepare  # noqa: F401 - fixture
from tests.test_async_settle_ticket import authorization, endpoint
from trusted_router import async_settle_timing as timing
from trusted_router import storage_gcp_async_admission as admission_sql
from trusted_router.billing_snapshot import Eligibility
from trusted_router.routes import settlements
from trusted_router.services import async_settle_handler as handler
from trusted_router.services.async_settle import Admission, load_runtime, projection

# Independent schema: adding an accidental field to production must fail here.
ADMISSION = {'admission_ms', 'lock_wait_ms', 'health', 'health_ms', 'workspace', 'workspace_ms'}
HANDOFF = ADMISSION | {'auth_ms', 'body_ms', 'queue_ms', 'validate_ms', 'enqueue_ms', 'callback_ms',
                       'commit_ms', 'attempts', 'unclassified_ms', 'total_ms'}
LABELS = {'accepted', 'duplicate', 'sync_required', 'rejected', 'unknown'}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms / 1000


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(timing, 'monotonic', clock)
    monkeypatch.setattr(time, 'monotonic', clock)
    return clock


def lines(caplog, metric):
    result = []
    for record in caplog.records:
        text = record.getMessage()
        if record.name != timing.logger.name:
            continue
        assert record.levelno == logging.INFO
        pairs = [part.split('=') for part in text.split()]
        assert all(len(pair) == 2 for pair in pairs)
        values = dict(pairs)
        assert len(values) == len(pairs)
        name = values.pop('metric')
        assert name in {'async_settle_handoff_phases', 'async_settle_projection_admission'}
        fields = HANDOFF if name == 'async_settle_handoff_phases' else ADMISSION
        if fields == HANDOFF:
            assert values.pop('outcome') in LABELS
        assert values.keys() == fields
        assert all(math.isfinite(float(v)) and float(v) >= 0
                   for k, v in values.items() if k not in {'health', 'workspace'})
        for key in {'health', 'workspace'}:
            assert values[key] in {'none', 'read', 'error'}
        if 'attempts' in fields:
            assert values['attempts'].isdigit()
        if name == metric:
            result.append(dict(pairs))
    return result


def one(caplog, metric='async_settle_handoff_phases'):
    found = lines(caplog, metric)
    assert len(found) == 1
    return {k: v if k in {'metric', 'outcome', 'health', 'workspace'} else float(v) for k, v in found[0].items()}


def measured_runtime(env, monkeypatch, clock):
    reads = []

    def health(db):
        reads.append('health')
        clock.advance(11)
        return healthy(observed_at=NOW, worker_heartbeat=NOW)

    def workspace(db, ws):
        reads.append('workspace')
        clock.advance(13)
        return Admission(0, 2)

    monkeypatch.setattr(admission_sql, 'read_health', health)
    monkeypatch.setattr(admission_sql, 'read_admission', workspace)
    rt = load_runtime(env[3], env[0])
    rt.signer = env[2].signer
    rt.epoch = env[2].epoch
    rt.admission.clock = clock
    rt.admission.wall_clock = lambda: NOW
    underlying = rt.admission.lock.lock

    class Lock:
        def acquire(self, *args, **kwargs):
            assert args == () and kwargs == {'timeout': 0.2}
            clock.advance(17)
            return underlying.acquire(*args, **kwargs)

        def release(self):
            underlying.release()

    rt.admission.lock.lock = Lock()
    return rt, reads


def delay(monkeypatch, obj, name, clock, ms):
    original = getattr(obj, name)

    def wrapped(*args, **kwargs):
        clock.advance(ms)
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapped)


async def route_call(env, body, rt, monkeypatch, clock):
    async def endpoint():
        raise AssertionError('legacy route')

    async def receive():
        clock.advance(2)
        return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}

    async def worker(fn, *args, **kwargs):
        clock.advance(3)
        return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))

    monkeypatch.setattr(settlements, 'require_internal_gateway', lambda *_: clock.advance(1))
    monkeypatch.setattr(settlements, 'run_in_threadpool', worker)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/settle',
                       'headers': [(b'x-tr-settlement-mode', b'async-v1')],
                       'app': SimpleNamespace(state=SimpleNamespace(settings=env[3], async_settle=rt))}, receive)
    dispatch = settlements.AsyncSettlementRoute('/settle', endpoint).get_route_handler()
    return await dispatch(request)


@pytest.mark.asyncio
async def test_accepted_route_phases_add_up(env, monkeypatch, clock, caplog):
    body, _, _ = prepare(env)
    rt, reads = measured_runtime(env, monkeypatch, clock)
    for name, ms in [('parse', 4), ('verify', 5), ('price', 6), ('intent', 7)]:
        delay(monkeypatch, handler, name, clock, ms)
    delay(monkeypatch, _FakeTransaction, 'batch_update', clock, 19)
    delay(monkeypatch, env[1], '_try_commit', clock, 23)
    delay(monkeypatch, env[1], 'run_in_transaction', clock, 29)
    original_metric = handler.metric

    def metric(event, **kwargs):
        # The unassigned remainder is ordinary post-enqueue bookkeeping/logging.
        if event == 'accepted':
            clock.advance(.2)
        original_metric(event, **kwargs)

    monkeypatch.setattr(handler, 'metric', metric)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result = await route_call(env, body, rt, monkeypatch, clock)
    assert result.status_code == 202
    row = one(caplog)
    expected = dict(auth_ms=1, body_ms=2, queue_ms=3, validate_ms=22, admission_ms=41, lock_wait_ms=17,
                    health='read', health_ms=11, workspace='read', workspace_ms=13,
                    enqueue_ms=71, callback_ms=19, commit_ms=23, attempts=1, unclassified_ms=.2, total_ms=140.2)
    assert row['outcome'] == 'accepted'
    for key, value in expected.items():
        assert row[key] == (value if isinstance(value, str) else pytest.approx(value, abs=.001))
    remainder = row['total_ms'] - sum(row[k] for k in ('auth_ms', 'body_ms', 'queue_ms', 'validate_ms', 'admission_ms', 'enqueue_ms'))
    assert remainder == pytest.approx(row['unclassified_ms'], abs=.004)  # eight rounded fields, <= 0.004 ms
    assert row['unclassified_ms'] == pytest.approx(.2, abs=.001)
    old_total = [r.value for r in caplog.records if getattr(r, 'metric', None) == 'async_settle_handoff_ms']
    assert old_total == [pytest.approx(row['total_ms'], abs=.001)]
    assert reads == ['health', 'workspace']
    assert timing.current() is None


@pytest.mark.parametrize('warm', [False, True])
def test_handoff_cold_and_warm(env, monkeypatch, clock, caplog, warm):
    body, _, _ = prepare(env)
    rt, reads = measured_runtime(env, monkeypatch, clock)
    if warm:
        assert rt.admission.eligible('ws-v1', 0)
        reads.clear()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert call((env[0], env[1], rt, env[3]), body).status_code == 202
    row = one(caplog)
    assert row['outcome'] == 'accepted'
    assert row['health'] == row['workspace'] == ('none' if warm else 'read')
    assert row['health_ms'] == (0 if warm else 11)
    assert row['workspace_ms'] == (0 if warm else 13)
    assert row['lock_wait_ms'] == 17
    assert reads == ([] if warm else ['health', 'workspace'])


@pytest.mark.parametrize('warm', [False, True])
def test_projection_cold_and_warm(env, monkeypatch, clock, caplog, warm):
    rt, reads = measured_runtime(env, monkeypatch, clock)
    if warm:
        assert rt.admission.eligible('ws-v1', 0)
        reads.clear()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result = projection(authorization=authorization(), endpoints=[endpoint()], requested=Eligibility(),
                            runtime=rt, settings=env[3], now=NOW)
    assert result['async_eligible']
    row = one(caplog, 'async_settle_projection_admission')
    assert row['health'] == row['workspace'] == ('none' if warm else 'read')
    assert row['health_ms'] == (0 if warm else 11)
    assert row['workspace_ms'] == (0 if warm else 13)
    assert row['lock_wait_ms'] == 17
    assert row['admission_ms'] == (17 if warm else 41)
    assert not lines(caplog, 'async_settle_handoff_phases')
    assert reads == ([] if warm else ['health', 'workspace'])


@pytest.mark.parametrize('outcome', ['duplicate', 'sync_required', 'rejected', 'unknown'])
def test_nonaccepted_outcomes(env, monkeypatch, clock, caplog, outcome):
    body, _, _ = prepare(env)
    if outcome == 'duplicate':
        assert call(env, body).status_code == 202
        env[3].async_settle_enabled = False  # Lookup before admission/enqueue.
    elif outcome == 'sync_required':
        env[2].signer = None
    elif outcome == 'rejected':
        body['secret-request-field'] = 'must-not-appear'
    else:
        def lost(fn, **kwargs):
            clock.advance(501)
            raise DeadlineExceeded('must-not-appear')
        monkeypatch.setattr(env[1], 'run_in_transaction', lost)
    if outcome == 'duplicate':
        delay(monkeypatch, handler.SpannerSettleOutbox, 'get', clock, 9)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        if outcome in {'rejected', 'unknown'}:
            with pytest.raises(HTTPException) as exc:
                call(env, body)
            assert exc.value.status_code == (400 if outcome == 'rejected' else 503)
        else:
            result = call(env, body)
            assert json.loads(result.body)['data']['acceptance']['status'] == outcome
    row = one(caplog)
    assert row['outcome'] == outcome
    for key in ['auth_ms', 'body_ms', 'queue_ms', 'callback_ms', 'commit_ms', 'attempts',
                'health_ms', 'workspace_ms']:
        assert row[key] == 0
    assert row['health'] == row['workspace'] == 'none'
    if outcome != 'unknown':
        assert row['admission_ms'] == row['enqueue_ms'] == 0
    else:
        assert row['enqueue_ms'] == row['total_ms'] == 501
    remainder = row['total_ms'] - sum(row[k] for k in
        ('auth_ms', 'body_ms', 'queue_ms', 'validate_ms', 'admission_ms', 'enqueue_ms'))
    assert row['unclassified_ms'] == pytest.approx(remainder, abs=.004)
    if outcome == 'duplicate':
        assert row['unclassified_ms'] > 0
    assert 'must-not-appear' not in caplog.text


@pytest.mark.parametrize('failure', ['clock', 'logger', 'construction'])
def test_observation_failure_preserves_acceptance(env, monkeypatch, clock, caplog, failure):
    body, _, _ = prepare(env)

    def broken(*args, **kwargs):
        raise RuntimeError('instrumentation failed')

    if failure == 'clock':
        monkeypatch.setattr(timing, 'monotonic', broken)
    elif failure == 'logger':
        monkeypatch.setattr(timing.logger, 'info', broken)
    else:
        monkeypatch.setattr(timing, 'Timing', broken)
    assert call(env, body).status_code == 202
    assert timing.current() is None


def test_other_threads_and_outside_request_do_not_record(env, monkeypatch, clock, caplog):
    rt, reads = measured_runtime(env, monkeypatch, clock)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert rt.admission.eligible('outside', 0)
        with timing.handoff_scope(clock(), clock(), enabled=True):
            observation = timing.current()
            observation.admitting = True
            ctx = copy_context()
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(ctx.run, rt.admission.eligible, 'other-thread', 0).result()
            assert all(v == 0 for v in observation.values.values())
            observation.admitting = False
    assert reads == ['health', 'workspace', 'workspace']
    assert len(lines(caplog, 'async_settle_handoff_phases')) == 1
    assert not lines(caplog, 'async_settle_projection_admission')


def test_attempts_and_commit_retry_intervals(env, monkeypatch, clock, caplog):
    body, _, _ = prepare(env)
    original = env[1]._try_commit
    commits = []
    intervals = []
    original_add = timing.Timing.add

    def observe_add(self, field, start, end):
        if field == 'callback_ms':
            intervals.append((end - start) * 1000)
        return original_add(self, field, start, end)

    monkeypatch.setattr(timing.Timing, 'add', observe_add)

    def commit(tx):
        clock.advance(23)
        commits.append(tx)
        return False if len(commits) == 1 else original(tx)

    monkeypatch.setattr(env[1], '_try_commit', commit)
    delay(monkeypatch, _FakeTransaction, 'batch_update', clock, 19)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert call(env, body).status_code == 202
    row = one(caplog)
    assert row['attempts'] == 2
    assert row['callback_ms'] == 38
    assert intervals == pytest.approx([19, 19])
    assert row['commit_ms'] == 46
    assert row['enqueue_ms'] == 84


def test_sync_recovery_does_not_emit_handoff(env, clock, caplog):
    body, _, _ = prepare(env)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert call(env, body, synchronous=True).status_code == 200
    assert not lines(caplog, 'async_settle_handoff_phases')


@pytest.mark.parametrize('site', ['lock', 'health', 'workspace'])
def test_admission_failure_times_only_reached_phases(env, monkeypatch, clock, caplog, site):
    body, _, _ = prepare(env)
    rt, _ = measured_runtime(env, monkeypatch, clock)
    if site == 'lock':
        def acquire(*args, **kwargs):
            # eligible's blocking acquire times out; diagnostic try-lock is separate.
            if kwargs == {'timeout': .2}:
                clock.advance(200)
            else:
                assert kwargs == {'blocking': False}
            return False
        monkeypatch.setattr(rt.admission.lock.lock, 'acquire', acquire)
    else:
        def failed(*args, **kwargs):
            clock.advance(11 if site == 'health' else 13)
            raise RuntimeError('private-read-error')

        # Keep timing around the actual failing callable, as load_runtime does.
        if site == 'health':
            rt.admission.health_read = timing.read(failed, 'health')
        else:
            rt.admission.read = timing.read(failed, 'workspace')
        # Diagnostics use the same underlying lock, but must not add lock_wait_ms.
        original_acquire = rt.admission.lock.lock.acquire
        # Let the diagnostic try-lock fail without affecting eligibility or reads.
        def diagnostic_acquire(*args, **kwargs):
            return False if kwargs == {'blocking': False} else original_acquire(*args, **kwargs)
        monkeypatch.setattr(rt.admission.lock.lock, 'acquire', diagnostic_acquire)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result = call((env[0], env[1], rt, env[3]), body)
    assert json.loads(result.body)['data']['acceptance']['status'] == 'sync_required'
    row = one(caplog)
    assert row['outcome'] == 'sync_required'
    assert row['enqueue_ms'] == row['callback_ms'] == row['commit_ms'] == row['attempts'] == 0
    assert row['lock_wait_ms'] == (200 if site == 'lock' else 17)
    assert row['health'] == ('none' if site == 'lock' else 'error' if site == 'health' else 'read')
    assert row['workspace'] == ('none' if site == 'lock' else 'error' if site == 'workspace' else 'read')
    assert row['health_ms'] == (0 if site == 'lock' else 11)
    assert row['workspace_ms'] == (0 if site == 'lock' else 13)
    assert 'private-read-error' not in caplog.text


def test_enqueue_retry_accumulates_and_final_duplicate_wins(env, monkeypatch, clock, caplog):
    body, _, _ = prepare(env)
    original = env[1].run_in_transaction
    calls = []

    def lost_once(fn, **kwargs):
        calls.append(1)
        result = original(fn, **kwargs)
        if len(calls) == 1:
            raise DeadlineExceeded('private-commit-reply')
        return result

    monkeypatch.setattr(env[1], 'run_in_transaction', lost_once)
    delay(monkeypatch, _FakeTransaction, 'batch_update', clock, 19)
    delay(monkeypatch, env[1], '_try_commit', clock, 23)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result = call(env, body)
    assert json.loads(result.body)['data']['acceptance']['status'] == 'duplicate'
    row = one(caplog)
    assert row['outcome'] == 'duplicate'
    assert row['attempts'] == 2
    assert row['callback_ms'] == 38
    assert row['commit_ms'] == 23  # The second callback failed before commit.
    assert row['enqueue_ms'] == 61
    assert 'private-commit-reply' not in caplog.text


@pytest.mark.parametrize('mutation', ['extra_key', 'text_value', 'label', 'nonfinite', 'duplicate_key', 'extra_line', 'level', 'read_label'])
def test_content_free_parser_rejects_leaks(caplog, mutation):
    observation = timing.Timing(0)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        observation.emit()
    record = caplog.records[-1]
    if mutation == 'extra_line':
        with caplog.at_level(logging.INFO):
            timing.logger.info('metric=private workspace_id=private')
    elif mutation == 'level':
        record.levelno = logging.WARNING
    else:
        record.msg = {
            'read_label': record.msg.replace('health=none', 'health=private'),
            'extra_key': record.msg + ' workspace_id=123',
            'text_value': record.msg.replace('auth_ms=0.000', 'auth_ms=private'),
            'label': record.msg.replace('outcome=rejected', 'outcome=private'),
            'nonfinite': record.msg.replace('auth_ms=0.000', 'auth_ms=nan'),
            'duplicate_key': record.msg + ' auth_ms=1',
        }[mutation]
    with pytest.raises((AssertionError, ValueError)):
        one(caplog)


@pytest.mark.parametrize('form', ['acquire', 'context'])
def test_runtime_lock_forwards_full_interface(env, monkeypatch, clock, caplog, form):
    rt = load_runtime(env[3], env[0])
    native = threading.Lock()
    calls = []

    class Lock:
        def acquire(self, *args, **kwargs):
            calls.append((args, kwargs))
            clock.advance(2)
            return native.acquire(*args, **kwargs)

        def release(self):
            return native.release()

        def locked(self):
            return native.locked()

    rt.admission.lock.lock = Lock()
    lock = rt.admission.lock
    caplog.clear()
    with caplog.at_level(logging.INFO), timing.handoff_scope(clock(), clock(), enabled=True):
        timing.current().admitting = True
        assert not lock.locked()
        if form == 'acquire':
            assert lock.acquire(blocking=False)
            assert lock.locked()
            lock.release()
            assert calls == [((), {'blocking': False})]
        else:
            with pytest.raises(ValueError), lock as acquired:
                assert acquired is True  # threading.Lock.__enter__ returns acquire's result.
                assert lock.locked()
                raise ValueError('body failed')
            assert calls == [((), {})]
        assert not lock.locked()
        timing.current().admitting = False
    assert one(caplog)['lock_wait_ms'] == 2


@pytest.mark.parametrize('site', ['health', 'workspace'])
def test_projection_read_error_labels(env, monkeypatch, clock, caplog, site):
    rt, _ = measured_runtime(env, monkeypatch, clock)

    def failed(*args, **kwargs):
        clock.advance(11 if site == 'health' else 13)
        raise RuntimeError('private-reader-error')

    if site == 'health':
        rt.admission.health_read = timing.read(failed, site)
    else:
        rt.admission.read = timing.read(failed, site)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result = projection(authorization=authorization(), endpoints=[endpoint()], requested=Eligibility(),
                            runtime=rt, settings=env[3], now=NOW)
    assert not result['async_eligible']
    row = one(caplog, 'async_settle_projection_admission')
    assert row[site] == 'error'
    assert row['workspace' if site == 'health' else 'health'] == 'read'
    assert row['health_ms'] == 11
    assert row['workspace_ms'] == 13
    assert 'private-reader-error' not in caplog.text


def test_callback_wrapper_preserves_arguments_result_and_metadata(clock, caplog):
    transaction = object()
    result = object()

    def original(tx, *, argument):
        """Original callback documentation."""
        assert tx is transaction and argument is result
        clock.advance(2)  # Local checks before SQL.
        clock.advance(19)  # SQL work.
        clock.advance(3)  # Local checks after SQL.
        return result

    wrapped = timing.callback(original)
    assert wrapped.__wrapped__ is original
    assert wrapped.__name__ == original.__name__
    assert wrapped.__doc__ == original.__doc__
    with caplog.at_level(logging.INFO), timing.handoff_scope(clock(), clock(), enabled=True):
        timing.stop_validation()
        with timing.measure('enqueue_ms'), timing.transaction_run():
            assert wrapped(transaction, argument=result) is result
            clock.advance(23)
    row = one(caplog)
    assert row['callback_ms'] == 24
    assert row['commit_ms'] == 23
    assert row['attempts'] == 1
    assert row['enqueue_ms'] == 47


@pytest.mark.parametrize('accepted', [False, True])
def test_zero_remainder_uses_unrounded_clock_readings(monkeypatch, caplog, accepted):
    # Adjacent intervals whose separately scaled float durations produce a
    # negative residual (~-2.3e-13 ms). Exact timestamp differences produce zero.
    points = [0., .07944374052099434, .2951456624856886, .3335027049958045,
              .671283989481595, 1.1294102994636352, 1.310233581366859]
    observation = timing.Timing(points[0])
    fields = ['auth_ms', 'body_ms', 'queue_ms', 'validate_ms', 'admission_ms', 'enqueue_ms']
    for key, start, end in zip(fields, points, points[1:], strict=False):
        observation.add(key, start, end)
    token = timing._CURRENT.set(observation)
    try:
        if accepted:
            timing.save_total(points[-1])
        else:
            observation.add('total_ms', points[0], points[-1])
        with caplog.at_level(logging.INFO):
            observation.emit()
    finally:
        timing._CURRENT.reset(token)
    assert observation.values['unclassified_ms'] == 0
    row = one(caplog)
    assert row['unclassified_ms'] == 0
    assert row['total_ms'] == pytest.approx(sum(row[key] for key in fields), abs=.004)


@pytest.mark.parametrize('elapsed_ms', [10, 501])
def test_definitive_conflict_after_uncertain_enqueue_is_rejected(env, monkeypatch, clock, caplog, elapsed_ms):
    # From the exact-head review of 12574688a: after an uncertain enqueue, reconciliation that
    # proves a different payload answers 409, and the line must say rejected, not unknown.
    # 10 ms leaves budget for the handler's retry; 501 ms exhausts the 500 ms handoff budget.
    body, _, _ = prepare(env)
    assert call(env, body).status_code == 202
    original = copy.deepcopy(env[1].settle_outbox)
    changed = copy.deepcopy(body)
    changed['raw_usage']['input_tokens'] = 3
    changed['terminal']['usage'].update(uncached_input_tokens=3, total_prompt_tokens=3)
    changed['terminal']['charge_micro'] = 3
    run = env[1].run_in_transaction
    calls = []

    def timeout_once(fn, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            clock.advance(elapsed_ms)
            raise DeadlineExceeded('uncertain outcome before the callback')
        return run(fn, **kwargs)

    monkeypatch.setattr(env[1], 'run_in_transaction', timeout_once)
    caplog.clear()
    with caplog.at_level(logging.INFO), pytest.raises(HTTPException) as raised:
        call(env, changed)
    assert raised.value.status_code == 409
    assert raised.value.detail['error']['message'] == 'Settlement intent already exists with a different payload'
    assert env[1].settle_outbox == original
    assert len(calls) == (2 if elapsed_ms == 10 else 1)
    assert one(caplog)['outcome'] == 'rejected'
