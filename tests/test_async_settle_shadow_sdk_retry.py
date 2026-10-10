"""Offline witnesses through real Session/Transaction and SDK retry paths."""
from __future__ import annotations

import datetime as dt
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Aborted, DeadlineExceeded, InternalServerError
from google.cloud.spanner_v1 import KeySet
from google.cloud.spanner_v1 import _helpers as sdk_helpers
from google.cloud.spanner_v1.session import Session
from google.cloud.spanner_v1.types import CommitResponse, PartialResultSet, Transaction
from google.rpc.error_details_pb2 import RetryInfo

from trusted_router import storage_gcp_io as io
from trusted_router.storage_gcp_async_settle_shadow import COUNTER, EvidenceStore, point_statement

RESET_MESSAGES = (
    'RST_STREAM',
    'Received unexpected EOS on DATA frame from server',
)


@pytest.fixture
def sdk_database(monkeypatch, shadow_deadline_clock):
    clock = shadow_deadline_clock
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    # Replace only the SDK helper's module-local sleep, retaining its real clock.
    # Session, Transaction, streaming result decoding and all retry loops are real.
    monkeypatch.setattr(sdk_helpers, 'time', SimpleNamespace(sleep=sleep, time=time.time))

    class Database:
        name = 'projects/offline/instances/offline/databases/offline'
        database_id = 'offline'
        _route_to_leader_enabled = False
        _next_nth_request = 1
        log_commit_stats = False
        default_transaction_options = SimpleNamespace(default_read_write_transaction_options=None)
        _instance = SimpleNamespace(instance_id='offline', _client=SimpleNamespace(
            project='offline', _client_context=None, _query_options=None))

        def __init__(self):
            self.calls = []
            self.errors = []
            self.begin_errors = []
            self.rollback_errors = []
            self.read_errors = []
            self.rows = []
            self.spanner_api = SimpleNamespace(
                execute_streaming_sql=self.query, commit=self.commit,
                begin_transaction=self.begin, rollback=self.rollback)
            self.session = Session(self)
            self.session._session_id = 'offline'

        def rpc(self, name, kwargs, duration, errors):
            self.calls.append((name, kwargs['timeout']))
            clock.now += min(duration, kwargs['timeout'])
            if kwargs['timeout'] < duration:
                raise DeadlineExceeded('fake transport deadline')
            if errors:
                raise errors.pop(0)

        def query(self, **kwargs):
            self.rpc('read', kwargs, .01, self.read_errors)
            response = PartialResultSet(metadata=dict(
                transaction=dict(id=b'offline'), row_type=dict(
                    fields=[dict(name='body', type=dict(code='STRING'))])))
            for row in self.rows:
                response._pb.values.add(string_value=row)
            return iter([response])

        def begin(self, **kwargs):
            self.rpc('begin', kwargs, .45, self.begin_errors)
            return Transaction(id=b'offline')

        def rollback(self, **kwargs):
            self.rpc('rollback', kwargs, .2, self.rollback_errors)

        def commit(self, **kwargs):
            self.rpc('commit', kwargs, .45, self.errors)
            return CommitResponse(commit_timestamp=dt.datetime(2026, 10, 10, tzinfo=dt.UTC))

        def metadata_and_request_id(self, nth_request, attempt, metadata, span):
            return metadata, None

        def with_error_augmentation(self, *args):
            return [], nullcontext()

        def run_in_transaction(self, callback, **kwargs):
            return self.session.run_in_transaction(callback, **kwargs)

    db = Database()
    io.configure_spanner_rpc_deadlines(db)
    return db, clock, sleeps


def read_once(tx):
    sql, params, param_types = point_statement(COUNTER, 'offline')
    return list(tx.execute_sql(sql, params=params, param_types=param_types, timeout=.2, retry=None))


@pytest.mark.parametrize('message', RESET_MESSAGES)
@pytest.mark.parametrize('operation', ['transaction', 'flush'])
def test_shadow_reset_fails_inside_horizon_without_sdk_sleep(sdk_database, caplog, message, operation):
    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError(message+' private SQL body and customer token')]
    store = EvidenceStore(db)
    started = clock.monotonic()
    failure = None
    try:
        if operation == 'transaction':
            store.transaction(read_once, started+1)
        else:
            identity = dt.datetime.now(dt.UTC).date().isoformat()+'/offline'
            store.flush(identity, {'sequence': 1}, started+1)
    except Exception as error:
        failure = error
    elapsed = clock.monotonic()-started
    # Keep the horizon assertion first: removing the conversion must expose
    # the 2 s SDK sleep, not merely a different exception type.
    assert elapsed < 1, f'elapsed={elapsed:.2f}s sleeps={sleeps}'
    assert sleeps == []
    assert elapsed == pytest.approx(.46 if operation == 'transaction' else .47)
    assert type(failure) is io.SpannerTransportReset
    assert failure.message == 'transport_reset'
    assert failure.__cause__ is None and failure.__suppress_context__
    assert db.calls == ([('read', .2), ('read', .2)] if operation == 'flush' else [('read', .2)]) + [('commit', .5)]
    assert 'stage=commit reason=transport_reset' in caplog.text
    assert 'private' not in caplog.text and message not in caplog.text
    assert io._SPANNER_RPC_MAX_SECONDS.get() is None
    assert io._SPANNER_RPC_DEADLINE.get() is None


@pytest.mark.parametrize('message', RESET_MESSAGES)
@pytest.mark.parametrize('strict', [False, True])
def test_billing_commit_still_uses_real_sdk_reset_retry(sdk_database, message, strict):
    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError(message)]
    started = clock.monotonic()
    # Billing can use strict deadlines, but does not set the shadow per-RPC cap.
    with io.spanner_rpc_deadline(started+10) if strict else nullcontext():
        assert db.run_in_transaction(lambda tx: (read_once(tx), 'billed')[1], timeout_secs=10) == 'billed'
    assert sleeps == [2]
    assert clock.monotonic()-started == pytest.approx(2.91)
    assert [timeout for name, timeout in db.calls if name == 'commit'] == pytest.approx([9.99, 7.54])


@pytest.mark.parametrize('error_type', [InternalServerError, DeadlineExceeded])
@pytest.mark.parametrize('shadow', [False, True])
def test_other_commit_errors_propagate_unchanged(sdk_database, error_type, shadow):
    db, clock, sleeps = sdk_database
    error = error_type('ordinary server failure')
    db.errors = [error]
    started = clock.monotonic()
    with pytest.raises(error_type) as caught:
        if shadow:
            EvidenceStore(db).transaction(read_once, started+1)
        else:
            db.run_in_transaction(read_once, timeout_secs=10)
    assert caught.value is error
    assert sleeps == [] and len(db.calls) == 2
    assert clock.monotonic()-started == pytest.approx(.46)


def test_capped_context_with_room_for_first_backoff_keeps_sdk_retry(sdk_database):
    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError('RST_STREAM')]
    with io.spanner_rpc_deadline(clock.monotonic()+3, max_rpc_seconds=.5):
        db.run_in_transaction(read_once)
    assert sleeps == [2]
    assert db.calls == [('read', .2), ('commit', .5), ('commit', .5)]


def test_shutdown_flush_records_reset_as_store_failure(sdk_database, caplog):
    from tests.test_async_settle_ticket import runtime, settings
    from trusted_router.services.async_settle_shadow import Runtime

    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError('RST_STREAM private evidence')]
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
    started = clock.monotonic()
    try:
        rt.flush(started+1, True)
        assert clock.monotonic()-started == pytest.approx(.47)
        assert sleeps == []
        body = rt.counters.snapshot()[0][1]
        assert body['drops'] == [dict(phase='worker', adapter='unknown', route_type='unknown',
                                     streamed=None, reason='store_unavailable', count=1)]
        assert body['first_gap_at_us'] is not None
        assert 'stage=commit reason=transport_reset' in caplog.text
        assert 'private' not in caplog.text
    finally:
        rt.executor.shutdown()


@pytest.mark.parametrize('message', RESET_MESSAGES)
def test_shadow_mutations_only_begin_reset_fails_inside_horizon(sdk_database, caplog, message):
    db, clock, sleeps = sdk_database
    db.begin_errors = [InternalServerError(message+' private cleanup keys')]
    started = clock.monotonic()
    failure = None
    try:
        EvidenceStore(db).transaction(
            lambda tx: tx.delete('tr_entities', KeySet(keys=[('kind', 'offline')])), started+1)
    except Exception as error:
        failure = error
    elapsed = clock.monotonic()-started
    assert elapsed < 1, f'elapsed={elapsed:.2f}s sleeps={sleeps}'
    assert sleeps == []
    assert elapsed == pytest.approx(.45)
    assert type(failure) is io.SpannerTransportReset
    assert failure.message == 'transport_reset'
    assert failure.__cause__ is None and failure.__suppress_context__
    assert db.calls == [('begin', .5)]
    assert 'stage=commit reason=transport_reset' in caplog.text
    assert 'private' not in caplog.text and message not in caplog.text
    assert io._SPANNER_RPC_MAX_SECONDS.get() is None
    assert io._SPANNER_RPC_DEADLINE.get() is None


@pytest.mark.parametrize('message', RESET_MESSAGES)
def test_shadow_callback_failure_rollback_reset_fails_inside_horizon(sdk_database, caplog, message):
    db, clock, sleeps = sdk_database
    # Real streaming decode succeeds and establishes the transaction id; the
    # store then fails while decoding the counter, so Session must roll it back.
    db.rows = ['private malformed counter JSON']
    db.rollback_errors = [InternalServerError(message+' private rollback body')]
    started = clock.monotonic()
    failure = None
    try:
        EvidenceStore(db).flush('offline', {'sequence': 1}, started+1)
    except Exception as error:
        failure = error
    elapsed = clock.monotonic()-started
    assert elapsed < 1, f'elapsed={elapsed:.2f}s sleeps={sleeps}'
    assert sleeps == []
    assert elapsed == pytest.approx(.21)
    assert type(failure) is io.SpannerTransportReset
    assert failure.message == 'transport_reset'
    assert failure.__cause__ is None and failure.__suppress_context__
    assert db.calls == [('read', .2), ('rollback', .5)]
    assert 'stage=callback reason=transport_reset' in caplog.text
    assert 'private' not in caplog.text and message not in caplog.text
    assert io._SPANNER_RPC_MAX_SECONDS.get() is None
    assert io._SPANNER_RPC_DEADLINE.get() is None


def test_shadow_read_deadline_exceeded_does_not_rollback(sdk_database, caplog):
    db, clock, sleeps = sdk_database
    error = DeadlineExceeded('private read failure')
    def callback(tx):
        read_once(tx)
        # Establish an id first: an incorrect rollback would now issue an RPC.
        db.read_errors = [error]
        read_once(tx)

    with pytest.raises(DeadlineExceeded) as caught:
        EvidenceStore(db).transaction(callback, clock.monotonic()+1)
    assert caught.value is error
    assert db.calls == [('read', .2), ('read', .2)]
    assert sleeps == []
    assert 'stage=callback reason=deadline_exceeded' in caplog.text
    assert 'private' not in caplog.text


def test_shadow_aborted_reentry_never_repeats_callback_or_commit(sdk_database, caplog):
    db, clock, sleeps = sdk_database
    # A server-supplied zero retry delay reaches the second SDK attempt without
    # exceeding its deadline; all Session retry and rollback code remains real.
    cause = SimpleNamespace(trailing_metadata=lambda: [
        ('google.rpc.retryinfo-bin', RetryInfo().SerializeToString())])
    db.errors = [Aborted('private conflict', errors=[cause])]
    callbacks = []

    def callback(tx):
        callbacks.append(tx)
        read_once(tx)

    with pytest.raises(RuntimeError, match='^shadow_transaction_retry$'):
        EvidenceStore(db).transaction(callback, clock.monotonic()+1)
    assert len(callbacks) == 1
    assert db.calls == [('read', .2), ('commit', .5)]
    # Session rolls back the fresh second transaction, which has no id and
    # therefore sends no rollback RPC.
    assert sleeps == [0]
    assert 'stage=commit reason=other' in caplog.text
    assert io._SPANNER_RPC_MAX_SECONDS.get() is None
    assert io._SPANNER_RPC_DEADLINE.get() is None


@pytest.mark.parametrize('message', RESET_MESSAGES)
def test_billing_short_deadline_keeps_reset_sleep_then_rejects_retry(sdk_database, message):
    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError(message)]
    started = clock.monotonic()
    failure = None
    try:
        with io.spanner_rpc_deadline(started+1.5):
            assert io._SPANNER_RPC_MAX_SECONDS.get() is None
            db.run_in_transaction(read_once, timeout_secs=1.5)
    except Exception as error:
        failure = error
    # This witness must fail if the cap guard is removed: short billing
    # deadlines deliberately retain the SDK's private two-second sleep.
    assert sleeps == [2], f'elapsed={clock.monotonic()-started:.2f}s sleeps={sleeps}'
    assert clock.monotonic()-started == pytest.approx(2.46)
    assert type(failure) is DeadlineExceeded
    assert 'transaction deadline exceeded' in failure.message
    assert [name for name, _ in db.calls] == ['read', 'commit']
    assert db.calls[-1][1] == pytest.approx(1.49)
    assert io._SPANNER_RPC_MAX_SECONDS.get() is None
    assert io._SPANNER_RPC_DEADLINE.get() is None


@pytest.mark.parametrize('message', RESET_MESSAGES)
def test_billing_without_contextvar_deadline_keeps_sdk_retry(sdk_database, message):
    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError(message)]
    started = clock.monotonic()
    assert io._SPANNER_RPC_DEADLINE.get() is None
    # Call Session directly: the API wrappers are installed, but the database
    # runner does not establish a ContextVar deadline for this witness.
    db.session.run_in_transaction(read_once)
    assert sleeps == [2]
    assert clock.monotonic()-started == pytest.approx(2.91)
    assert db.calls == [('read', .2), ('commit', 20.), ('commit', 20.)]
    assert io._SPANNER_RPC_DEADLINE.get() is None


@pytest.mark.parametrize('method', ['commit', 'begin_transaction', 'rollback'])
@pytest.mark.parametrize('message', [None, 'missing', 'rst_stream',
                                    'received unexpected EOS on DATA frame from server'])
def test_wrapper_leaves_nonmatching_or_missing_reset_message_unchanged(sdk_database, method, message):
    db, clock, _ = sdk_database
    error = InternalServerError(message)
    if message == 'missing':
        del error.message
    {'commit': db.errors, 'begin_transaction': db.begin_errors,
     'rollback': db.rollback_errors}[method].append(error)
    # Exercise the wrapper directly: the upstream SDK itself assumes a string
    # message, so its checker is intentionally outside this wrapper witness.
    with io.spanner_rpc_deadline(clock.monotonic()+1, max_rpc_seconds=.5):
        with pytest.raises(InternalServerError) as caught:
            getattr(db.spanner_api, method)()
    assert caught.value is error


@pytest.mark.parametrize('method', ['commit', 'begin_transaction', 'rollback'])
def test_wrapper_requires_exact_error_class_and_shared_deadline(sdk_database, method):
    db, clock, _ = sdk_database
    errors = {'commit': db.errors, 'begin_transaction': db.begin_errors,
              'rollback': db.rollback_errors}[method]

    class DerivedInternalServerError(InternalServerError):
        pass

    error = DerivedInternalServerError('RST_STREAM')
    errors.append(error)
    with io.spanner_rpc_deadline(clock.monotonic()+1, max_rpc_seconds=.5):
        with pytest.raises(DerivedInternalServerError) as caught:
            getattr(db.spanner_api, method)()
    assert caught.value is error
    error = InternalServerError('RST_STREAM')
    errors.append(error)
    token = io._SPANNER_RPC_MAX_SECONDS.set(.5)
    try:
        assert io._SPANNER_RPC_DEADLINE.get() is None
        with pytest.raises(InternalServerError) as caught:
            getattr(db.spanner_api, method)()
        assert caught.value is error
    finally:
        io._SPANNER_RPC_MAX_SECONDS.reset(token)
