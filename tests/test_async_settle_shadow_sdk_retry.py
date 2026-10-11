"""Offline witnesses through real Session/Transaction and SDK retry paths."""
from __future__ import annotations

import datetime as dt
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import (
    Aborted,
    DeadlineExceeded,
    InternalServerError,
    ServiceUnavailable,
)
from google.cloud.spanner_v1 import KeySet
from google.cloud.spanner_v1 import _helpers as sdk_helpers
from google.cloud.spanner_v1.session import Session
from google.cloud.spanner_v1.types import CommitResponse, PartialResultSet, Transaction
from google.rpc.error_details_pb2 import RetryInfo

from trusted_router import storage_gcp_io as io
from trusted_router.storage_gcp_async_settle_shadow import (
    COUNTER,
    RPC_BUDGET_SECONDS,
    TRANSACTION_RPC_BUDGET_SECONDS,
    WRITE_BUDGET_SECONDS,
    EvidenceStore,
    point_statement,
)

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
        _directed_read_options = None
        _next_nth_request = 1
        log_commit_stats = False
        default_transaction_options = SimpleNamespace(default_read_write_transaction_options=None)
        _instance = SimpleNamespace(instance_id='offline', _client=SimpleNamespace(
            project='offline', _client_context=None, _query_options=None))

        def __init__(self):
            self.calls = []
            self.read_duration = .01
            self.read_durations = []
            self.commit_duration = .45
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
            duration = self.read_durations.pop(0) if self.read_durations else self.read_duration
            self.rpc('read', kwargs, duration, self.read_errors)
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
            self.rpc('commit', kwargs, self.commit_duration, self.errors)
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
    return list(tx.execute_sql(sql, params=params, param_types=param_types, timeout=RPC_BUDGET_SECONDS, retry=None))


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
            store.transaction(read_once, started+WRITE_BUDGET_SECONDS)
        else:
            identity = dt.datetime.now(dt.UTC).date().isoformat()+'/offline'
            store.flush(identity, {'sequence': 1}, started+WRITE_BUDGET_SECONDS)
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
    read_count = 2 if operation == 'flush' else 1
    assert db.calls == [
        ('read', pytest.approx(min(RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS-i*.01)))
        for i in range(read_count)
    ] + [('commit', pytest.approx(min(TRANSACTION_RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS-read_count*.01)))]
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
            EvidenceStore(db).transaction(read_once, started+WRITE_BUDGET_SECONDS)
        else:
            db.run_in_transaction(read_once, timeout_secs=10)
    assert caught.value is error
    assert sleeps == [] and len(db.calls) == 2
    assert clock.monotonic()-started == pytest.approx(.46)


def test_capped_context_with_room_for_first_backoff_keeps_sdk_retry(sdk_database):
    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError('RST_STREAM')]
    with io.spanner_rpc_deadline(clock.monotonic()+3, max_rpc_seconds=TRANSACTION_RPC_BUDGET_SECONDS):
        db.run_in_transaction(read_once)
    assert sleeps == [2]
    assert db.calls == [
        ('read', min(RPC_BUDGET_SECONDS, 3)),
        ('commit', pytest.approx(min(TRANSACTION_RPC_BUDGET_SECONDS, 3-.01))),
        ('commit', pytest.approx(min(TRANSACTION_RPC_BUDGET_SECONDS, 3-.01-.45-2))),
    ]


def test_shutdown_flush_records_reset_as_store_failure(sdk_database, caplog):
    from tests.test_async_settle_ticket import runtime, settings
    from trusted_router.services.async_settle_shadow import Runtime

    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError('RST_STREAM private evidence')]
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
    started = clock.monotonic()
    try:
        rt.flush(started+WRITE_BUDGET_SECONDS, True)
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
            lambda tx: tx.delete('tr_entities', KeySet(keys=[('kind', 'offline')])), started+WRITE_BUDGET_SECONDS)
    except Exception as error:
        failure = error
    elapsed = clock.monotonic()-started
    assert elapsed < 1, f'elapsed={elapsed:.2f}s sleeps={sleeps}'
    assert sleeps == []
    assert elapsed == pytest.approx(.45)
    assert type(failure) is io.SpannerTransportReset
    assert failure.message == 'transport_reset'
    assert failure.__cause__ is None and failure.__suppress_context__
    assert db.calls == [('begin', min(TRANSACTION_RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS))]
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
        EvidenceStore(db).flush('offline', {'sequence': 1}, started+WRITE_BUDGET_SECONDS)
    except Exception as error:
        failure = error
    elapsed = clock.monotonic()-started
    assert elapsed < 1, f'elapsed={elapsed:.2f}s sleeps={sleeps}'
    assert sleeps == []
    assert elapsed == pytest.approx(.21)
    assert type(failure) is io.SpannerTransportReset
    assert failure.message == 'transport_reset'
    assert failure.__cause__ is None and failure.__suppress_context__
    assert db.calls == [
        ('read', min(RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS)),
        ('rollback', pytest.approx(min(TRANSACTION_RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS-.01))),
    ]
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
        EvidenceStore(db).transaction(callback, clock.monotonic()+WRITE_BUDGET_SECONDS)
    assert caught.value is error
    assert db.calls == [
        ('read', min(RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS)),
        ('read', pytest.approx(min(RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS-.01))),
    ]
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
        EvidenceStore(db).transaction(callback, clock.monotonic()+WRITE_BUDGET_SECONDS)
    assert len(callbacks) == 1
    assert db.calls == [
        ('read', min(RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS)),
        ('commit', pytest.approx(min(TRANSACTION_RPC_BUDGET_SECONDS, WRITE_BUDGET_SECONDS-.01))),
    ]
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
    assert db.calls == [('read', RPC_BUDGET_SECONDS), ('commit', 20.), ('commit', 20.)]
    assert io._SPANNER_RPC_DEADLINE.get() is None


@pytest.mark.parametrize('method', ['commit', 'begin_transaction', 'rollback'])
@pytest.mark.parametrize('message', RESET_MESSAGES)
def test_wrapper_transport_reset_retains_no_private_error(sdk_database, method, message):
    db, clock, _ = sdk_database
    private_text = 'private SQL body and customer token'
    error = InternalServerError(message+' '+private_text)
    {'commit': db.errors, 'begin_transaction': db.begin_errors,
     'rollback': db.rollback_errors}[method].append(error)
    with io.spanner_rpc_deadline(clock.monotonic()+WRITE_BUDGET_SECONDS,
                                max_rpc_seconds=TRANSACTION_RPC_BUDGET_SECONDS):
        with pytest.raises(io.SpannerTransportReset) as caught:
            getattr(db.spanner_api, method)()
    err = caught.value
    assert err.__cause__ is None
    assert err.__context__ is None
    assert err.__suppress_context__ is True
    assert err.args == ('transport_reset',)
    for rendered in (repr(err), str(err), repr(err.args), repr(err.__dict__)):
        assert message not in rendered
        assert private_text not in rendered
        assert 'private' not in rendered


@pytest.mark.parametrize('read_seconds', [.25, .32])
@pytest.mark.parametrize('commit_seconds', [.3, .6])
@pytest.mark.parametrize('reset', [False, True])
def test_regional_flush_and_reset_share_one_sdk_deadline(sdk_database, read_seconds, commit_seconds, reset):
    db, clock, sleeps = sdk_database
    db.read_duration = read_seconds
    db.commit_duration = commit_seconds
    if reset:
        db.errors = [InternalServerError('RST_STREAM private evidence')]
    started = clock.monotonic()
    identity = dt.datetime.now(dt.UTC).date().isoformat()+'/regional'
    failure = None
    try:
        EvidenceStore(db).flush(identity, {'sequence': 1}, started+1)
    except Exception as error:
        failure = error
    remaining = 1 - 2*read_seconds
    # Each read is given the read budget, clipped by what the caller's second
    # has left; the commit the remainder.
    assert db.calls == [('read', pytest.approx(min(RPC_BUDGET_SECONDS, 1))),
                        ('read', pytest.approx(min(RPC_BUDGET_SECONDS, 1-read_seconds))),
                        ('commit', pytest.approx(remaining))]
    assert sleeps == []
    assert clock.monotonic()-started == pytest.approx(min(1, 2*read_seconds+commit_seconds))
    if commit_seconds > remaining:
        assert type(failure) is DeadlineExceeded
    elif reset:
        assert type(failure) is io.SpannerTransportReset
        assert failure.message == 'transport_reset'
    else:
        assert failure is None
    assert io._SPANNER_RPC_MAX_SECONDS.get() is None
    assert io._SPANNER_RPC_DEADLINE.get() is None


@pytest.mark.parametrize('reads', [[.6, .05], [.05, .6]])
@pytest.mark.parametrize('commit_seconds', [.2, .6])
def test_regional_read_uses_remaining_total_budget(sdk_database, reads, commit_seconds):
    db, clock, sleeps = sdk_database
    db.read_durations = list(reads)
    db.commit_duration = commit_seconds
    identity = dt.datetime.now(dt.UTC).date().isoformat()+'/regional-tail'
    started = clock.monotonic()
    failure = None
    try:
        EvidenceStore(db).flush(identity, {'sequence': 1}, started+1)
    except Exception as error:
        failure = error
    assert [name for name, _ in db.calls] == ['read', 'read', 'commit']
    assert [timeout for _, timeout in db.calls] == pytest.approx([1, 1-reads[0], .35])
    assert clock.monotonic()-started == pytest.approx(min(1, .65+commit_seconds))
    assert sleeps == []
    if commit_seconds > .35:
        assert type(failure) is DeadlineExceeded
    else:
        assert failure is None


def test_midnight_counter_rollover_defers_second_regional_transaction(sdk_database):
    from tests.test_async_settle_ticket import runtime, settings
    from trusted_router.services.async_settle_shadow import FLUSH_BUDGET_SECONDS, Runtime

    db, clock, sleeps = sdk_database
    db.read_duration, db.commit_duration = .1, .35
    today = dt.datetime.combine(dt.datetime.now(dt.UTC).date(), dt.time(), dt.UTC)
    wall = [(today - dt.timedelta(seconds=10)).timestamp()]
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
    rt.counters.clock = lambda: wall[0]
    try:
        rt.flush(clock.monotonic()+FLUSH_BUDGET_SECONDS)
        clock.now += 6
        wall[0] = today.timestamp()+1
        db.calls.clear()
        started = clock.monotonic()
        rt.flush(started+FLUSH_BUDGET_SECONDS)
        assert [name for name, _ in db.calls] == ['read', 'read', 'commit']
        assert clock.monotonic()-started == pytest.approx(.55)
        assert rt.counters.retired_through == (today.date()-dt.timedelta(days=1)).isoformat()
        assert all(not row['drops'] for row in rt.counters.days.values())
        # The next bounded maintenance pass registers the new day. The old
        # closed writer has already been acknowledged and cannot be reopened.
        clock.now += 6
        db.calls.clear()
        rt.flush(clock.monotonic()+FLUSH_BUDGET_SECONDS)
        assert [name for name, _ in db.calls] == ['read', 'read', 'commit']
        current = rt.counters.days[today.date().isoformat()]
        assert not current['closed'] and not current['drops']
        assert sleeps == []
    finally:
        rt.executor.shutdown()


@pytest.mark.parametrize('error_type', [ServiceUnavailable, InternalServerError])
@pytest.mark.parametrize('budget', [.2, 1.])
def test_shadow_snapshot_restart_cannot_renew_deadline(sdk_database, error_type, budget):
    db, clock, sleeps = sdk_database
    db.read_duration = .08 if budget == .2 else .32
    db.read_errors = [error_type('RST_STREAM private evidence') for _ in range(4)]
    started = clock.monotonic()
    with pytest.raises(DeadlineExceeded):
        EvidenceStore.query(db.session.snapshot(), point_statement(COUNTER, 'offline'), started+budget)
    assert clock.monotonic()-started == pytest.approx(budget)
    assert sleeps == []
    # Each restart is given the read budget, clipped by what the caller's
    # deadline has left, until nothing is.
    expected, left = [], budget
    while left > 1e-9:
        expected.append(min(RPC_BUDGET_SECONDS, left))
        left -= db.read_duration
    assert [name for name, _ in db.calls] == ['read']*len(expected)
    assert [timeout for _, timeout in db.calls] == pytest.approx(expected)
    assert io._SPANNER_RPC_MAX_SECONDS.get() is None
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
    with io.spanner_rpc_deadline(clock.monotonic()+WRITE_BUDGET_SECONDS,
                                max_rpc_seconds=TRANSACTION_RPC_BUDGET_SECONDS):
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
    with io.spanner_rpc_deadline(clock.monotonic()+WRITE_BUDGET_SECONDS,
                                max_rpc_seconds=TRANSACTION_RPC_BUDGET_SECONDS):
        with pytest.raises(DerivedInternalServerError) as caught:
            getattr(db.spanner_api, method)()
    assert caught.value is error
    error = InternalServerError('RST_STREAM')
    errors.append(error)
    token = io._SPANNER_RPC_MAX_SECONDS.set(TRANSACTION_RPC_BUDGET_SECONDS)
    try:
        assert io._SPANNER_RPC_DEADLINE.get() is None
        with pytest.raises(InternalServerError) as caught:
            getattr(db.spanner_api, method)()
        assert caught.value is error
    finally:
        io._SPANNER_RPC_MAX_SECONDS.reset(token)


@pytest.mark.parametrize('read_seconds,commit_seconds', [(.5, .8), (.45, .8), (.2, .3)])
def test_a_regional_observation_fits_its_budget_end_to_end(shadow_deadline_clock, read_seconds, commit_seconds):
    """One observation from the slowest region: the permit reservation, the
    booking read, the sample and a counter flush, each transaction two leader
    reads and a cross-region commit, finishes inside the observation budget
    with no drop. The budget is checked through the Runtime, each store call
    refusing a deadline its regional cost would pass, not transaction by
    transaction."""
    from types import SimpleNamespace

    from google.api_core.exceptions import DeadlineExceeded

    from tests.test_async_settle_shadow import NOW, context, signer, wire
    from tests.test_async_settle_ticket import runtime, settings
    from trusted_router.async_settle_shadow_compare import Booking
    from trusted_router.async_settle_shadow_evidence import dimensions
    from trusted_router.services.async_settle_shadow import (
        OBSERVATION_BUDGET_SECONDS,
        Capture,
        Runtime,
    )

    clock = shadow_deadline_clock
    transaction = 2*read_seconds+commit_seconds
    costs = {'reserve': transaction, 'booking': read_seconds, 'insert_sample': transaction, 'flush': transaction}
    answers = {'reserve': 100, 'booking': Booking(2, 'settled', True), 'insert_sample': 'inserted', 'flush': None}
    calls = []

    def step(name):
        def run(*args):
            deadline = args[-1]
            calls.append(name)
            if clock.monotonic()+costs[name] > deadline:
                raise DeadlineExceeded(f'{name} needs {costs[name]}s, {deadline-clock.monotonic():.2f}s left')
            clock.now += costs[name]
            return answers[name]
        return run

    store = SimpleNamespace(**{name: step(name) for name in costs})
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40, async_settle_shadow_workspaces='ws-v1'),
                 runtime(), store)
    rt.signer = signer()
    rt.last_flush = -1e9  # a flush is due
    rt.counters.clock = lambda: NOW  # the counters' day is the capture's, so its flush runs now
    dims = dimensions('openai', 'chat.completions', None)
    with rt.counters.day(NOW):
        for field in ('settle_attempts', 'observed_attempts', 'observed_unknown'):
            rt.counters.increment(dims, field)
    ctx = context()
    started = clock.monotonic()
    try:
        rt.process(Capture(rt, ctx.body, 'settle', NOW, 0, ctx.authorization), wire(), {'data': {'settled': True}}, 1, dims)
        body = rt.counters.snapshot()[0][1]
        assert body['drops'] == [], [d['reason'] for d in body['drops']]
        assert calls == ['reserve', 'booking', 'insert_sample', 'flush']
        assert clock.monotonic()-started == pytest.approx(3*transaction+read_seconds)
        assert 3*transaction+read_seconds <= OBSERVATION_BUDGET_SECONDS
    finally:
        rt.executor.shutdown()


def test_a_maintenance_flush_is_budgeted_from_when_it_runs(sdk_database):
    """A maintenance flush queued behind the worker's observations gets its
    budget from when it runs: the queue wait, thirty seconds here, takes
    nothing from it. The flush is queued through maintain_counters, behind a
    task that holds the single worker until the clock has moved."""
    import asyncio
    import threading

    from tests.test_async_settle_ticket import runtime, settings
    from trusted_router.services.async_settle_shadow import Runtime

    db, clock, sleeps = sdk_database
    db.read_duration = .5
    db.commit_duration = .8
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
    rt.last_flush = -1e9
    held, release = threading.Event(), threading.Event()

    def hold():
        held.set()
        release.wait(10)

    async def run():
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        blocker = loop.run_in_executor(rt.executor, hold)
        maintenance = asyncio.ensure_future(rt.maintain_counters(stopped))
        await loop.run_in_executor(None, held.wait, 10)
        clock.now += 30  # the flush waits in the queue while the worker is held
        stopped.set()
        release.set()
        await blocker
        await maintenance

    try:
        asyncio.run(run())
        assert sleeps == []
        # The transaction ran to its commit: a deadline from the queue time
        # would have ended it before its first read.
        assert [name for name, _ in db.calls] == ['read', 'read', 'commit']
    finally:
        rt.executor.shutdown()


def test_a_flush_defers_a_row_its_remainder_cannot_hold(sdk_database):
    """A flush that closes a day that retired and registers the current one
    runs two transactions, each on a budget of its own within the flush's;
    given a budget that holds one transaction and a fraction, it runs the
    close whole, attempts nothing for the current day, which waits
    unacknowledged, and the next flush with a whole budget persists it."""
    from tests.test_async_settle_ticket import runtime, settings
    from trusted_router.async_settle_shadow_evidence import day_at, dimensions
    from trusted_router.services.async_settle_shadow import FLUSH_BUDGET_SECONDS, Runtime

    db, clock, sleeps = sdk_database
    started = clock.monotonic()
    assert Runtime.transaction_deadline(started + WRITE_BUDGET_SECONDS) == pytest.approx(started + WRITE_BUDGET_SECONDS)
    assert Runtime.transaction_deadline(started + 2*WRITE_BUDGET_SECONDS) == pytest.approx(started + WRITE_BUDGET_SECONDS)
    assert Runtime.transaction_deadline(started + WRITE_BUDGET_SECONDS - .01) is None
    db.read_duration = .5
    db.commit_duration = .8
    transaction = 2*.5+.8
    now = [1791244800.]
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
    rt.counters.clock = lambda: now[0]
    dims = dimensions('openai', 'responses', False)
    acknowledged = []
    acknowledge = rt.counters.acknowledge
    rt.counters.acknowledge = lambda identity, body: (acknowledged.append(identity), acknowledge(identity, body))
    try:
        rt.counters.increment(dims, 'authorize_attempts')
        rt.last_flush = 0
        rt.flush(clock.monotonic() + FLUSH_BUDGET_SECONDS)  # day one's writer registered
        assert [name for name, _ in db.calls] == ['read', 'read', 'commit']
        old = day_at(now[0]) + '/' + rt.counters.instance
        db.calls.clear()
        now[0] += 86400  # day one retires; the current day's counters begin
        rt.counters.increment(dims, 'authorize_attempts')
        new = day_at(now[0]) + '/' + rt.counters.instance
        rt.last_flush = 0
        started = clock.monotonic()
        rt.flush(started + WRITE_BUDGET_SECONDS + .5)  # holds one transaction and a fraction
        # Day one's close ran whole; the current day was not attempted.
        assert [name for name, _ in db.calls] == ['read', 'read', 'commit']
        assert clock.monotonic()-started == pytest.approx(transaction)
        assert acknowledged[-1:] == [old] and new not in acknowledged
        db.calls.clear()
        rt.last_flush = 0
        started = clock.monotonic()
        rt.flush(started + FLUSH_BUDGET_SECONDS)  # a whole budget: the current day persists
        assert [name for name, _ in db.calls] == ['read', 'read', 'commit']
        assert new in acknowledged
        assert all(timeout >= .5 for _, timeout in db.calls)
        assert sleeps == []
        assert all(not body['drops'] for body in rt.counters.days.values())
    finally:
        rt.executor.shutdown()


def test_a_current_day_flush_with_a_short_remainder_attempts_nothing(sdk_database):
    """A flush of the current day alone whose budget cannot hold a whole
    transaction attempts no RPC, acknowledges nothing and drops nothing: the
    row waits, and the next flush with a whole budget persists it. This is
    the remainder gate on its own, apart from the after-a-close rule."""
    from tests.test_async_settle_ticket import runtime, settings
    from trusted_router.async_settle_shadow_evidence import dimensions
    from trusted_router.services.async_settle_shadow import FLUSH_BUDGET_SECONDS, Runtime

    db, clock, sleeps = sdk_database
    db.read_duration = .5
    db.commit_duration = .8
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
    rt.counters.clock = lambda: 1791244800.
    acknowledged = []
    acknowledge = rt.counters.acknowledge
    rt.counters.acknowledge = lambda identity, body: (acknowledged.append(identity), acknowledge(identity, body))
    try:
        rt.counters.increment(dimensions('openai', 'responses', False), 'authorize_attempts')
        rt.last_flush = 0
        started = clock.monotonic()
        rt.flush(started + WRITE_BUDGET_SECONDS - .01)
        assert db.calls == [] and acknowledged == []
        assert clock.monotonic() == started
        assert all(not body['drops'] for body in rt.counters.days.values())
        rt.last_flush = 0
        rt.flush(clock.monotonic() + FLUSH_BUDGET_SECONDS)
        assert [name for name, _ in db.calls] == ['read', 'read', 'commit']
        assert len(acknowledged) == 1
        assert sleeps == []
    finally:
        rt.executor.shutdown()
