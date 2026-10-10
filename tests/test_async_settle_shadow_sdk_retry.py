"""Offline witnesses through real Transaction.commit and the SDK reset retry loop."""
from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import DeadlineExceeded, InternalServerError
from google.cloud.spanner_v1 import _helpers as sdk_helpers
from google.cloud.spanner_v1 import transaction as sdk_transaction
from google.cloud.spanner_v1.types import CommitResponse

from trusted_router import storage_gcp_io as io
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore

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

    # Replace module references only; never patch the process-wide time module.
    # Transaction.commit and _helpers._retry both remain the real SDK code.
    monkeypatch.setattr(sdk_helpers, 'time', SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(sdk_transaction, 'trace_call', lambda *a, **kw: nullcontext(
        SimpleNamespace(add_event=lambda *a, **kw: None)))
    monkeypatch.setattr(sdk_transaction, 'MetricsCapture', lambda *a, **kw: nullcontext())

    class Database:
        name = 'projects/offline/instances/offline/databases/offline'
        database_id = 'offline'
        _route_to_leader_enabled = False
        _next_nth_request = 1
        _instance = SimpleNamespace(instance_id='offline', _client=SimpleNamespace(
            project='offline', _client_context=None))

        def __init__(self):
            self.calls = []
            self.errors = []
            self.spanner_api = SimpleNamespace(execute_sql=self.query, commit=self.commit)

        def query(self, **kwargs):
            self.calls.append(('read', kwargs['timeout']))
            clock.now += .01
            return []

        def commit(self, **kwargs):
            self.calls.append(('commit', kwargs['timeout']))
            clock.now += min(.45, kwargs['timeout'])
            if kwargs['timeout'] < .45:
                raise DeadlineExceeded('fake transport deadline')
            if self.errors:
                raise self.errors.pop(0)
            return CommitResponse(commit_timestamp=dt.datetime(2026, 10, 10, tzinfo=dt.UTC))

        def with_error_augmentation(self, *args):
            return [], nullcontext()

        def run_in_transaction(self, callback, **kwargs):
            session = SimpleNamespace(_database=self, name=self.name+'/sessions/offline',
                                      is_multiplexed=False)
            tx = sdk_transaction.Transaction(session)
            tx._transaction_id = b'offline'
            # Only reads/transport are fake. Mutations and commit are real SDK.
            tx.execute_sql = lambda sql, **kw: self.spanner_api.execute_sql(
                timeout=kw['timeout'], retry=kw['retry'])
            result = callback(tx)
            tx.commit(request_options=kwargs.get('commit_request_options'))
            return result

    db = Database()
    io.configure_spanner_rpc_deadlines(db)
    return db, clock, sleeps


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
            store.transaction(lambda tx: None, started+1)
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
    assert elapsed == pytest.approx(.45 if operation == 'transaction' else .47)
    assert type(failure) is io.SpannerCommitTransportReset
    assert failure.message == 'transport_reset'
    assert failure.__cause__ is None and failure.__suppress_context__
    assert db.calls == ([('read', .2), ('read', .2)] if operation == 'flush' else []) + [('commit', .5)]
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
        assert db.run_in_transaction(lambda tx: 'billed', timeout_secs=10) == 'billed'
    assert sleeps == [2]
    assert clock.monotonic()-started == pytest.approx(2.9)
    assert [timeout for _, timeout in db.calls] == pytest.approx([10, 7.55])


@pytest.mark.parametrize('error_type', [InternalServerError, DeadlineExceeded])
@pytest.mark.parametrize('shadow', [False, True])
def test_other_commit_errors_propagate_unchanged(sdk_database, error_type, shadow):
    db, clock, sleeps = sdk_database
    error = error_type('ordinary server failure')
    db.errors = [error]
    started = clock.monotonic()
    with pytest.raises(error_type) as caught:
        if shadow:
            EvidenceStore(db).transaction(lambda tx: None, started+1)
        else:
            db.run_in_transaction(lambda tx: None, timeout_secs=10)
    assert caught.value is error
    assert sleeps == [] and len(db.calls) == 1
    assert clock.monotonic()-started == pytest.approx(.45)


def test_capped_context_with_room_for_first_backoff_keeps_sdk_retry(sdk_database):
    db, clock, sleeps = sdk_database
    db.errors = [InternalServerError('RST_STREAM')]
    with io.spanner_rpc_deadline(clock.monotonic()+3, max_rpc_seconds=.5):
        db.run_in_transaction(lambda tx: None)
    assert sleeps == [2]
    assert db.calls == [('commit', .5), ('commit', .5)]


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
