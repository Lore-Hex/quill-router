"""Content-free claim preimages and unchanged claim/apply/resolution behavior."""
from __future__ import annotations

import copy
import datetime as dt
import logging
import threading
from types import SimpleNamespace

import pytest

from tests.test_settle_outbox_drain import _bare_authorization, _row
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services import settle_outbox_worker as worker
from trusted_router.services.settle_drain_observability import WORKER_LOGGER, WorkerLogFilter
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox

HASH = '0123456789abcdef' * 4
REF = HASH[:16]
OLD = 'soworker_' + 'a' * 32
NEW = 'soworker_' + 'b' * 32
PRIOR = '2026-01-01T00:00:00+00:00'
LEASE = '2099-01-01T00:00:00+00:00'


def setup_slot(monkeypatch, *, prior=True, reason=None, missing_hash=False):
    row = _row(_bare_authorization('SENTINEL_AUTHORIZATION'))
    row.workspace_id = 'SENTINEL_WORKSPACE'
    row.reservation_id = 'SENTINEL_RESERVATION'
    row.model_id = 'SENTINEL_MODEL'
    row.selected_endpoint_id = 'SENTINEL_PROVIDER'
    row.selected_usage_type = 'SENTINEL_USAGE'
    row.settle_body = 'SENTINEL_KEY SENTINEL_GENERATION SENTINEL_USAGE'
    row.actual_cost_micro = 987654321
    row.payload_hash = None if missing_hash else HASH
    row.lease_owner, row.leased_until = (OLD, PRIOR) if prior else (None, None)
    row.created_at = '2026-01-01T00:00:00+00:00'
    stop = threading.Event()
    clock = [0.]
    calls = []

    def claim(rows, *, lease_seconds):
        assert rows == [row] and lease_seconds == 30
        clock[0] += .25
        row.lease_owner, row.leased_until = NEW, LEASE
        if reason == 'stop':
            stop.set()
        elif reason == 'deadline':
            clock[0] = 10.
        elif reason == 'lease':
            row.leased_until = PRIOR
        elif reason == 'missing_lease':
            row.leased_until = None
        return rows

    def apply(value):
        calls.append(value)
        clock[0] += .5
        return ApplyOutcome.SETTLED_NOW

    def resolve(box, value, outcome, **kwargs):
        clock[0] += .125
        box.mark(value.authorization_id, value.intent_kind, done=True, lease_owner=value.lease_owner)

    class DateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 0, 0, 1, tzinfo=dt.UTC)

    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 123.))
    monkeypatch.setattr(worker, 'dt', SimpleNamespace(datetime=DateTime, UTC=dt.UTC))
    monkeypatch.setattr(drain, 'apply_frozen_settle', apply)
    monkeypatch.setattr(drain, '_resolve_row', resolve)
    monkeypatch.setattr(SpannerSettleOutbox, 'mark', lambda *args, **kwargs: 'done')
    box = SimpleNamespace(due_shard=lambda **kw: [row], claim_candidates=claim,
                          _database=None, _pt=None, _async_fence=True)
    return box, stop, calls


@pytest.mark.parametrize('prior', [False, True])
@pytest.mark.parametrize('missing_hash', [False, True])
def test_exact_claim_timing_completed_resolution_and_privacy(monkeypatch, caplog, prior, missing_hash):
    box, stop, calls = setup_slot(monkeypatch, prior=prior, missing_hash=missing_hash)
    ref = 'none' if missing_hash else REF
    with caplog.at_level(logging.INFO):
        assert worker._apply_slot(box, 0, 30, 10., stop) == (1, 'settled_now', 987654321)
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        f'async_drain.claim ref={ref} prior_owner={OLD if prior else "none"} '
        f'prior_leased_until={PRIOR if prior else "none"} owner={NEW} leased_until={LEASE}',
        f'async_drain.resolution status=done fence_miss=0 ref={ref}',
        f'async_drain.completed latency_seconds=1.000000 ref={ref}',
        'async_drain.timing outcome=settled_now service_seconds=0.625000 observed_at=123.000000 '
        f'ref={ref} claim_seconds=0.250000 resolve_seconds=0.125000',
    ]
    assert len(calls) == 1
    assert all(r.name == WORKER_LOGGER and WorkerLogFilter().filter(r) is r for r in caplog.records)
    assert 'SENTINEL_' not in caplog.text and '987654321' not in caplog.text
    assert HASH not in caplog.text
    for name in ('authorization_id', 'workspace', 'generation', 'model', 'provider', 'usage', 'cost', 'key='):
        assert name not in caplog.text
    other = logging.LogRecord('other.module', logging.INFO, '', 0, 'SENTINEL_KEY', (), None)
    scrubbed = WorkerLogFilter().filter(other)
    assert scrubbed.getMessage() == 'logger=other.module level=INFO exception_class=none'


@pytest.mark.parametrize('reason', ['stop', 'deadline', 'lease', 'missing_lease'])
def test_exact_deferred_all_reasons(monkeypatch, caplog, reason):
    box, stop, calls = setup_slot(monkeypatch, reason=reason)
    with caplog.at_level(logging.INFO):
        assert worker._apply_slot(box, 0, 30, 10., stop) == (1, 'deferred', 0)
    assert len(caplog.records) == 2 and not calls
    assert caplog.records[-1].getMessage() == (
        f'async_drain.deferred ref={REF} owner={NEW} reason={"lease" if reason == "missing_lease" else reason}')
    assert 'SENTINEL_' not in caplog.text


@pytest.mark.parametrize('operation,result', [('mark', 'done'), ('mark', 'pending'), ('mark', None),
                                             ('park', True), ('park', False)])
def test_exact_observed_resolution(monkeypatch, caplog, operation, result):
    monkeypatch.setattr(SpannerSettleOutbox, operation, lambda *a, **kw: result)
    box = worker._ObservedOutbox(None, None)
    box.ref = REF
    with caplog.at_level(logging.INFO):
        assert getattr(box, operation)('SENTINEL_AUTHORIZATION', 'settle') == result
    expected = (f'status=park parked={int(result)} fence_miss={int(not result)}' if operation == 'park'
                else f'status={result} fence_miss={int(result is None)}')
    assert caplog.messages == [f'async_drain.resolution {expected} ref={REF}']


@pytest.mark.parametrize('reason', [None, 'stop', 'deadline', 'lease'])
def test_logging_failure_cannot_change_claim_apply_or_resolution(monkeypatch, reason):
    box, stop, calls = setup_slot(monkeypatch, reason=reason)
    def broken(*args, **kwargs):
        raise RuntimeError('handler failed')
    monkeypatch.setattr(worker.logger, 'log', broken)
    assert worker._apply_slot(box, 0, 30, 10., stop) == (
        (1, 'deferred', 0) if reason else (1, 'settled_now', 987654321))
    assert len(calls) == (0 if reason else 1)


@pytest.mark.parametrize('value', [None, '', 'SENTINEL_AUTHORIZATION', 'A' * 64, '0' * 63, '0' * 64 + '\n'])
def test_malformed_hash_never_emits_arbitrary_content(value):
    assert worker._row_ref(value) == 'none'


@pytest.mark.parametrize('winner', [0, 1, None, 'exception'])
def test_split_claim_equivalent_to_claim_shard(monkeypatch, caplog, winner):
    """Run actual claim_candidates in both legs, including loss and exceptions."""
    from trusted_router import storage_gcp_settle_outbox as adapter
    monkeypatch.setattr(adapter.uuid, 'uuid4', lambda: SimpleNamespace(hex='b' * 32))
    monkeypatch.setattr(adapter, '_iso_after_seconds', lambda seconds: LEASE)
    original = [_row(_bare_authorization(f'SENTINEL_{i}')) for i in range(2)]
    for i, row in enumerate(original):
        row.lease_owner = OLD if i else None
        row.leased_until = PRIOR if i else None
        row.payload_hash = HASH if i else 'f' * 64
    traces, states, results = [], [], []
    for split in (False, True):
        candidates = copy.deepcopy(original)
        trace = []
        stop = threading.Event()
        class Box(SpannerSettleOutbox):
            def __init__(self, candidates, trace, stop):
                super().__init__(None, None)
                self.candidates, self.trace, self.stop = candidates, trace, stop

            def due_shard(self, *, shard, async_only=False):
                self.trace.append(('due', shard, async_only))
                return self.candidates

            def _claim_one(self, row, *, owner, lease_until):
                self.trace.append(('claim', row.authorization_id, owner, lease_until))
                if winner == 'exception':
                    raise TimeoutError('same claim failure')
                return winner is not None and row is self.candidates[winner]

            def claim_candidates(self, rows, *, lease_seconds):
                result = super().claim_candidates(rows, lease_seconds=lease_seconds)
                self.stop.set()  # Defer application so only claim behavior is compared.
                return result
        box = Box(candidates, trace, stop)
        with caplog.at_level(logging.INFO):
            if winner == 'exception':
                with pytest.raises(TimeoutError, match='same claim failure'):
                    if split:
                        worker._apply_slot(box, 3, 30, float('inf'), stop)
                    else:
                        box.claim_shard(shard=3, lease_seconds=30)
            elif split:
                result = worker._apply_slot(box, 3, 30, float('inf'), stop)
                results.append(result[0])
            else:
                results.append(len(box.claim_shard(shard=3, lease_seconds=30)))
        traces.append(trace)
        states.append(candidates)
    assert traces[0] == traces[1] and states[0] == states[1]
    assert len(results) == 0 or results[0] == results[1]
    if winner in (None, 'exception'):
        assert not any(message.startswith(('async_drain.claim ', 'async_recovery.claim '))
                       for message in caplog.messages)
    if winner == 1:
        assert caplog.messages[0] == (f'async_drain.claim ref={REF} prior_owner={OLD} '
                                     f'prior_leased_until={PRIOR} owner={NEW} leased_until={LEASE}')


@pytest.mark.parametrize('failure', ['apply', 'resolve'])
@pytest.mark.parametrize('broken_logger', [False, True])
def test_existing_exception_outcomes_survive_audit(monkeypatch, caplog, failure, broken_logger):
    box, stop, _ = setup_slot(monkeypatch)
    errors = []
    def fail(*args, **kwargs):
        raise RuntimeError('SENTINEL_AUTHORIZATION SENTINEL_KEY SENTINEL_USAGE')
    def resolve(observed, row, outcome, **kwargs):
        errors.append((outcome, kwargs))
        if failure == 'resolve':
            fail()
    monkeypatch.setattr(drain, '_resolve_row', resolve)
    if failure == 'apply':
        monkeypatch.setattr(drain, 'apply_frozen_settle', fail)
    if broken_logger:
        monkeypatch.setattr(worker.logger, 'log', fail)
    with caplog.at_level(logging.INFO):
        assert worker._apply_slot(box, 0, 30, 10., stop) == (
            1, ApplyOutcome.ERROR if failure == 'apply' else 'resolve_error', 0)
    assert len(errors) == 1
    if failure == 'apply':
        assert errors[0][0] == ApplyOutcome.ERROR
        assert isinstance(errors[0][1]['apply_error'], RuntimeError)
        assert errors[0][1]['error_note'].startswith('RuntimeError: SENTINEL_')
    assert 'SENTINEL_' not in caplog.text
