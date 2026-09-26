"""Batch failure atomicity, SDK status fidelity, and retry/replay contracts."""
from __future__ import annotations

import copy
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import Aborted, FailedPrecondition, from_grpc_status
from google.cloud.spanner_v1 import _helpers, param_types
from google.cloud.spanner_v1 import session as session_module
from google.cloud.spanner_v1.session import Session
from google.cloud.spanner_v1.types import ExecuteBatchDmlResponse, ResultSet, ResultSetStats
from google.protobuf.any_pb2 import Any as AnyProto
from google.rpc import code_pb2
from google.rpc.error_details_pb2 import ErrorInfo, RetryInfo
from google.rpc.status_pb2 import Status

from tests.fakes.spanner import FakeSpannerDatabase, _FakeTransaction
from trusted_router.storage_gcp_authorize import AuthorizeOutcome, authorize_atomic
from trusted_router.storage_gcp_batch_dml import execute_batch_dml
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counter_dml import entity_insert_statement
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_settle_outbox import (
    ENQ_EXISTS_TERMINAL,
    ENQ_INSERTED,
    ENQ_LEASED,
    ENQ_REFRESHED,
    SpannerSettleOutbox,
)
from trusted_router.storage_models import GatewayAuthorization, SettleOutboxRow

NOW = datetime(2026, 9, 25, tzinfo=UTC)


@pytest.fixture
def sdk_runner(monkeypatch: pytest.MonkeyPatch) -> tuple[Session, Mock, Mock]:
    """Real Session runner; only transaction I/O and telemetry are stubbed."""
    database = SimpleNamespace(
        log_commit_stats=False,
        database_id='database',
        _instance=SimpleNamespace(instance_id='instance', _client=SimpleNamespace(project='project')),
    )
    session = Session(database)
    transactions = [Mock(_transaction_id=None), Mock(_transaction_id=b'retried')]
    monkeypatch.setattr(session, 'transaction', Mock(side_effect=transactions))
    monkeypatch.setattr(session_module, 'trace_call', lambda *a, **kw: nullcontext(Mock()))
    monkeypatch.setattr(session_module, 'MetricsCapture', lambda *a, **kw: nullcontext())
    return session, transactions[0], transactions[1]


@pytest.mark.parametrize('detail_kind', ['retry_info', 'zero_delay', 'none', 'unrelated'])
def test_real_sdk_runner_retries_in_status_aborted(
    monkeypatch: pytest.MonkeyPatch, sdk_runner: tuple[Session, Mock, Mock], detail_kind: str,
) -> None:
    session, aborted_tx, committed_tx = sdk_runner
    status = Status(code=code_pb2.ABORTED, message='batch contention')
    if detail_kind != 'none':
        unrelated = AnyProto()
        unrelated.Pack(ErrorInfo(reason='contention'))
        status.details.append(unrelated)
    if detail_kind in ('retry_info', 'zero_delay'):
        retry_info = RetryInfo()
        if detail_kind == 'retry_info':
            retry_info.retry_delay.seconds = 1
            retry_info.retry_delay.nanos = 375_000_000
        packed = AnyProto()
        packed.Pack(retry_info)
        status.details.append(packed)
    aborted_tx.batch_update.return_value = (status, [])
    committed_tx.batch_update.return_value = (Status(), [1])
    sleep = Mock()
    monkeypatch.setattr(_helpers.time, 'sleep', sleep)
    monkeypatch.setattr(_helpers.random, 'random', lambda: 0.25)
    statements = [entity_insert_statement(param_types, 'test', 'a', '{}')]
    callbacks = []

    def callback(tx: Any) -> str:
        callbacks.append(tx)
        execute_batch_dml(tx, statements, [(1,)])
        return 'committed'

    outer_attempts: list[int] = []
    assert run_in_transaction_with_retry(session, callback, attempts_out=outer_attempts) == 'committed'
    assert callbacks == [aborted_tx, committed_tx]
    assert outer_attempts == [1]  # Retry belongs to the SDK, not the outer wrapper.
    aborted_tx.commit.assert_not_called()
    committed_tx.commit.assert_called_once()
    for tx in callbacks:
        tx.batch_update.assert_called_once_with(statements)
        tx.rollback.assert_not_called()
    expected_delay = {'retry_info': 1.375, 'zero_delay': 0.0}.get(detail_kind, 2.25)
    sleep.assert_called_once_with(expected_delay)


@pytest.mark.parametrize('code', [
    code for code in code_pb2.Code.values() if code not in (code_pb2.OK, code_pb2.ABORTED)
])
def test_real_sdk_runner_preserves_other_status_mappings(
    sdk_runner: tuple[Session, Mock, Mock], code: int,
) -> None:
    session, tx, unused_tx = sdk_runner
    status = Status(code=code, message='batch failure')
    tx.batch_update.return_value = (status, [])
    statements = [entity_insert_statement(param_types, 'test', 'a', '{}')]
    with pytest.raises(type(from_grpc_status(code, status.message))) as caught:
        run_in_transaction_with_retry(
            session, lambda transaction: execute_batch_dml(transaction, statements, [(1,)]),
        )
    assert caught.value.message == status.message
    assert caught.value.errors == []
    tx.batch_update.assert_called_once_with(statements)
    tx.commit.assert_not_called()
    tx.rollback.assert_called_once()
    unused_tx.batch_update.assert_not_called()


def test_fake_rejects_aborted_without_sdk_retry_cause() -> None:
    db = _database()

    def callback(tx: Any) -> None:
        raise Aborted('invalid retry payload', errors=())

    with pytest.raises(IndexError):
        db.run_in_transaction(callback)
    assert db.commits == 0 and db.aborts == 0


def test_fake_retries_a_cause_without_trailing_metadata_like_the_sdk() -> None:
    # The SDK's _get_retry_delay treats missing trailing_metadata as default
    # backoff, so the fake must retry rather than fail on it.
    db = _database()
    attempts: list[Any] = []

    def callback(tx: Any) -> None:
        attempts.append(tx)
        if len(attempts) == 1:
            raise Aborted('no retry metadata', errors=(object(),))

    db.run_in_transaction(callback)
    assert len(attempts) == 2
    assert db.aborts == 1


def _database() -> FakeSpannerDatabase:
    db = FakeSpannerDatabase()
    db.typed['tr_credit_balance'] = {('workspace', 0): {
        'workspace_id': 'workspace', 'shard': 0,
        'total_credits': 1000, 'total_usage': 0, 'reserved': 0,
    }}
    db.typed['tr_key_limit'] = {('key', 0): {
        'key_hash': 'key', 'shard': 0, 'limit_micro': 1000,
        'usage': 0, 'byok_usage': 0, 'reserved': 0, 'include_byok': True,
    }}
    return db


def _authorization(aid: str, rid: str) -> GatewayAuthorization:
    return GatewayAuthorization(
        id=aid, workspace_id='workspace', key_hash='key', model_id='model',
        provider='anthropic', usage_type='Credits', estimated_microdollars=100,
        credit_reservation_id=rid, created_at=NOW.isoformat().replace('+00:00', 'Z'),
    )


def _authorize(db: FakeSpannerDatabase, mode: str = 'typed') -> dict:
    return authorize_atomic(
        db, param_types, workspace_id='workspace', key_hash='key', estimate=100,
        has_credit_candidate=True, reservation_usage_type='Credits',
        idempotency_scope='scope', idempotency_fingerprint='fingerprint',
        expires_at=NOW + timedelta(minutes=5), request_record_write_mode=mode,
        build_authorization=_authorization,
        build_auth_body=lambda aid, rid: json_body(_authorization(aid, rid)),
    )


def _settle(db: FakeSpannerDatabase) -> tuple[SpannerSettleOutbox, SettleOutboxRow]:
    result = _authorize(db)
    aid, rid = result['authorization_id'], result['reservation_id']
    db.gateway_authorizations[aid]['terminal_at'] = NOW
    db.reservations[rid]['terminal_at'] = NOW
    return SpannerSettleOutbox(db, param_types), SettleOutboxRow(
        authorization_id=aid, reservation_id=rid, intent_kind='settle',
        settle_origin='typed', actual_cost_micro=70, auto_refill_workspace_id='workspace',
    )


def _state(db: FakeSpannerDatabase) -> Any:
    return copy.deepcopy((db.typed, db.rows, db.reservations, db.gateway_authorizations,
                          db.settle_outbox))


def _inject(
    monkeypatch: pytest.MonkeyPatch, index: int, failure: str, *, once: bool = False,
) -> list[Any]:
    """Inject at the statement boundary, so a real prefix is already staged."""
    original_update = _FakeTransaction.execute_update
    original_batch = _FakeTransaction.batch_update
    batches: list[Any] = []
    position = 0
    fired = False

    def batch(tx: Any, statements: Any, **kwargs: Any) -> Any:
        nonlocal position
        position = 0
        batches.append(copy.deepcopy(statements))
        return original_batch(tx, statements, **kwargs)

    def update(tx: Any, sql: str, **kwargs: Any) -> int:
        nonlocal position, fired
        if tx._in_batch:
            current = position
            position += 1
            if current == index and not (once and fired):
                fired = True
                if failure == 'count':
                    original_update(tx, sql, **kwargs)
                    return 2  # impossible for these single-PK statements
                from google.api_core.exceptions import Aborted
                if failure == 'aborted':
                    raise Aborted('injected batch abort')
                raise FailedPrecondition('injected statement failure')
        return original_update(tx, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    monkeypatch.setattr(_FakeTransaction, 'execute_update', update)
    return batches


@pytest.mark.parametrize('mode', ['typed', 'legacy'])
@pytest.mark.parametrize('index', [0, 1, 2])
@pytest.mark.parametrize('failure', ['status', 'count'])
def test_authorize_partial_batch_rolls_back(
    monkeypatch: pytest.MonkeyPatch, mode: str, index: int, failure: str,
) -> None:
    db = _database()
    before = _state(db)
    _inject(monkeypatch, index, failure)
    with pytest.raises(FailedPrecondition):
        _authorize(db, mode)
    assert _state(db) == before  # no rows, holds, or entity authorization leak
    assert db.commits == 0 and db.rollback_calls == 1


@pytest.mark.parametrize('index', [0, 1, 2])
@pytest.mark.parametrize('failure', ['status', 'count'])
def test_settle_partial_batch_rolls_back(
    monkeypatch: pytest.MonkeyPatch, index: int, failure: str,
) -> None:
    db = _database()
    outbox, row = _settle(db)
    before, commits = _state(db), db.commits
    _inject(monkeypatch, index, failure)
    with pytest.raises(FailedPrecondition):
        outbox.enqueue(row)
    assert _state(db) == before  # no intent or retention changes; original holds intact
    assert db.commits == commits and db.rollback_calls == 1


@pytest.mark.parametrize('path', ['authorize', 'legacy', 'settle'])
@pytest.mark.parametrize('index', [0, 1, 2])
def test_aborted_batch_retries_whole_transaction_with_stable_inputs(
    monkeypatch: pytest.MonkeyPatch, path: str, index: int,
) -> None:
    db = _database()
    if path == 'settle':
        outbox, row = _settle(db)
    before_commits = db.commits
    batches = _inject(monkeypatch, index, 'aborted', once=True)
    if path == 'settle':
        assert outbox.enqueue(row) == ENQ_INSERTED
        assert len(db.settle_outbox) == 1
    else:
        result = _authorize(db, 'legacy' if path == 'legacy' else 'typed')
        assert result['outcome'] == AuthorizeOutcome.ACCEPTED
    assert db.aborts == 1 and db.commits == before_commits + 1
    assert len(batches) == 2 and batches[0] == batches[1]
    assert len(db.reservations) == 1
    assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 100
    assert db.typed['tr_key_limit'][('key', 0)]['reserved'] == 100


@pytest.mark.parametrize(('existing', 'preserve', 'expected'), [
    ('pending', False, ENQ_REFRESHED), ('pending', True, 'frozen'),
    ('leased', False, ENQ_LEASED), ('done', False, ENQ_EXISTS_TERMINAL),
    ('dead', False, ENQ_EXISTS_TERMINAL), ('release_approved', False, ENQ_EXISTS_TERMINAL),
])
def test_already_exists_batch_uses_unchanged_intent_replay_path(
    existing: str, preserve: bool, expected: str,
) -> None:
    db = _database()
    outbox, row = _settle(db)
    assert outbox.enqueue(row) == ENQ_INSERTED
    record = db.settle_outbox[(row.authorization_id, row.intent_kind)]
    record['status'] = 'pending' if existing == 'leased' else existing
    if existing == 'leased':
        record['leased_until'] = '2099-01-01T00:00:00Z'
        record['lease_owner'] = 'worker'
    before = _state(db)
    assert outbox.enqueue(replace(row, actual_cost_micro=80), preserve_existing=preserve) == expected
    assert db.rollback_calls == 1  # mapped AlreadyExists rolled back before replay
    if expected == ENQ_REFRESHED:
        assert db.settle_outbox[(row.authorization_id, row.intent_kind)]['actual_cost_micro'] == 80
    else:
        assert _state(db) == before


def test_fake_batch_is_one_rpc_and_leaves_only_successful_prefix_staged() -> None:
    db = _database()
    a = entity_insert_statement(param_types, 'test', 'a', '{}')
    b = entity_insert_statement(param_types, 'test', 'b', '{}')
    tx = _FakeTransaction(db)
    status, counts = tx.batch_update([a, a, b])
    assert isinstance(status, Status)
    assert status.code == code_pb2.ALREADY_EXISTS and counts == [1]
    assert db.transaction_batch_update_calls == 1
    assert db.transaction_execute_update_calls == 0
    assert len(tx.pending_writes) == 1 and not db.rows
    # The fake must not auto-rollback a failed batch: an unchecked caller really
    # can commit its prefix, just as with the SDK. Production checks prevent it.
    assert db._try_commit(tx)
    assert ('test', 'a') in db.rows and ('test', 'b') not in db.rows


@pytest.mark.parametrize('code', [code_pb2.OK, code_pb2.FAILED_PRECONDITION])
@pytest.mark.parametrize('counts', [[1], [1, 1], [1, 1, 1], [1, 0]])
def test_sdk_response_status_and_every_row_count_are_checked(code: int, counts: list[int]) -> None:
    response = ExecuteBatchDmlResponse(
        status=Status(code=code, message='SDK fixture'),
        result_sets=[ResultSet(stats=ResultSetStats(row_count_exact=n)) for n in counts],
    )

    class Transaction:
        def batch_update(self, statements: Any) -> Any:
            return response.status, [r.stats.row_count_exact for r in response.result_sets]

    statements = [entity_insert_statement(param_types, 'test', name, '{}') for name in ('a', 'b')]
    if code == 0 and counts == [1, 1]:
        execute_batch_dml(Transaction(), statements, [(1,), (1,)])
    else:
        with pytest.raises(FailedPrecondition):
            execute_batch_dml(Transaction(), statements, [(1,), (1,)])


def test_key_rejection_rolls_back_speculative_batch() -> None:
    db = _database()
    db.typed['tr_key_limit'][('key', 0)]['limit_micro'] = 0
    before = _state(db)
    assert _authorize(db)['outcome'] == AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    assert _state(db) == before
    assert db.transaction_batch_update_calls == 1
    assert db.rollback_calls == 2
    assert db.commits == 0


@pytest.mark.parametrize('path', ['typed', 'legacy', 'settle'])
def test_batch_matches_original_sequential_dml(
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    """Differential money-path proof against the previous sequential statements."""
    import uuid

    from trusted_router import storage_gcp_authorize as authorize_module
    from trusted_router import storage_gcp_settle_outbox as outbox_module

    monkeypatch.setattr(authorize_module, 'utcnow', lambda: NOW)
    monkeypatch.setattr(authorize_module.uuid, 'uuid4', lambda: uuid.UUID(int=1))
    monkeypatch.setattr(outbox_module, '_iso_now', lambda: NOW.isoformat())

    def run() -> tuple[Any, Any]:
        db = _database()
        if path == 'settle':
            outbox, row = _settle(db)
            result = outbox.enqueue(row)
        else:
            result = _authorize(db, path)
        return result, _state(db)

    batched = run()

    def sequential(tx: Any, statements: Any, expected_counts: Any, **kwargs: Any) -> None:
        for sql, params, types in statements:
            tx.execute_update(sql, params=params, param_types=types)

    monkeypatch.setattr(authorize_module, 'execute_batch_dml', sequential)
    monkeypatch.setattr(outbox_module, 'execute_batch_dml', sequential)
    assert run() == batched


def test_real_sdk_rolls_back_speculative_zero_before_sequential_rejection(
    sdk_runner: tuple[Session, Mock, Mock],
) -> None:
    session, speculative, sequential = sdk_runner
    speculative.execute_sql.return_value = []
    speculative.execute_update.return_value = 1
    speculative.batch_update.return_value = (Status(), [0, 1, 1])
    sequential.execute_sql.side_effect = [[], [[0, True]]]
    sequential.execute_update.side_effect = [1, 0]
    result = _authorize(session)
    assert result == {'outcome': AuthorizeOutcome.KEY_LIMIT_EXCEEDED}
    speculative.rollback.assert_called_once()
    sequential.rollback.assert_called_once()
    speculative.commit.assert_not_called()
    sequential.commit.assert_not_called()
    sequential.batch_update.assert_not_called()


def test_real_sdk_authorize_aborted_prefix_retries_before_business_fallback(
    monkeypatch: pytest.MonkeyPatch, sdk_runner: tuple[Session, Mock, Mock],
) -> None:
    session, aborted, committed = sdk_runner
    monkeypatch.setattr(_helpers.time, 'sleep', Mock())
    for tx in (aborted, committed):
        tx.execute_sql.return_value = []
        tx.execute_update.return_value = 1
    aborted.batch_update.return_value = (Status(code=code_pb2.ABORTED), [0, 1])
    committed.batch_update.return_value = (Status(), [1, 1, 1])
    assert _authorize(session)['outcome'] == AuthorizeOutcome.ACCEPTED
    assert aborted.batch_update.call_args == committed.batch_update.call_args
    aborted.commit.assert_not_called()
    aborted.rollback.assert_not_called()
    committed.commit.assert_called_once()


def _finalize_fixture() -> tuple[FakeSpannerDatabase, Any]:
    from trusted_router.storage_gcp_authorize import typed_finalize_atomic
    from trusted_router.storage_gcp_operational_analytics_outbox import (
        SpannerOperationalAnalyticsOutbox,
    )
    from trusted_router.storage_models import Generation

    db = _database()
    db.now = NOW
    result = _authorize(db)
    aid, rid = result['authorization_id'], result['reservation_id']
    auth = _authorization(aid, rid)
    gen = Generation.from_settle_body(
        authorization=auth, provider_name='provider', model_id='model',
        usage_type='Credits', provider='provider', body={}, input_tokens=5,
        output_tokens=7, actual_cost_microdollars=70,
    )
    auth.record_finalization(success=True, actual_microdollars=70,
                             selected_usage_type='Credits', generation=gen)
    SpannerSettleOutbox(db, param_types).enqueue(SettleOutboxRow(
        authorization_id=aid, reservation_id=rid, intent_kind='settle',
        settle_origin='typed', actual_cost_micro=70,
    ))

    def finalize() -> dict:
        return typed_finalize_atomic(
            db, param_types, reservation_id=rid, authorization_id=aid,
            success=True, actual_micro=70, settled_usage_type='Credits', now=NOW,
            outbox_available=True, authorization=auth, auth_body_settled=json_body(auth),
            generation=gen, persist_generation_record=True,
            operational_analytics_outbox=SpannerOperationalAnalyticsOutbox(db, param_types),
            settle_outbox_done=(aid, 'settle'),
        )
    return db, finalize


@pytest.mark.parametrize('index', range(4))
@pytest.mark.parametrize('failure', ['status', 'count'])
def test_finalize_partial_batch_rolls_back_done_claim_and_evidence(
    monkeypatch: pytest.MonkeyPatch, index: int, failure: str,
) -> None:
    db, finalize = _finalize_fixture()
    before, commits = _state(db), db.commits
    _inject(monkeypatch, index, failure)
    with pytest.raises(FailedPrecondition):
        finalize()
    assert _state(db) == before
    assert not db.generation_records and not db.operational_analytics_outbox
    assert db.commits == commits and db.rollback_calls == 1


@pytest.mark.parametrize('index', range(4))
def test_finalize_batch_abort_retries_claim_done_and_all_evidence(
    monkeypatch: pytest.MonkeyPatch, index: int,
) -> None:
    db, finalize = _finalize_fixture()
    commits = db.commits
    batches = _inject(monkeypatch, index, 'aborted', once=True)
    result = finalize()
    assert result['outcome'] == 'settled' and result['outbox_marked'] is True
    assert db.aborts == 1 and db.commits == commits + 1
    assert len(batches) == 2 and all(len(batch) == 4 for batch in batches)
    assert all(row['status'] == 'done' and row['attempts'] == 1 for row in db.settle_outbox.values())
    assert all(row['settled'] and row['actual_micro'] == 70 for row in db.reservations.values())
    assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 70
    assert db.typed['tr_key_limit'][('key', 0)]['usage'] == 70
    assert len(db.generation_records) == len(db.operational_analytics_outbox) == 1


def test_fake_rejects_returning_batch_and_function_limit() -> None:
    tx = _FakeTransaction(_database())
    with pytest.raises(ValueError, match='Batch DML cannot return rows'):
        tx.batch_update(['UPDATE tr_settle_outbox SET status=\'done\' THEN RETURN reservation_id'])
    with pytest.raises(ValueError, match='1000 functions'):
        tx.execute_update('UPDATE tr_settle_outbox SET attempts=' + 'COALESCE(' * 1001 + '0' + ', 0)' * 1001)


def test_installed_sdk_streams_done_returning_rows_and_advances_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the installed transport/decoder contract without a server."""
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.spanner_v1 import Client, snapshot
    from google.cloud.spanner_v1.types import PartialResultSet, ResultSetMetadata, StructType

    from trusted_router.storage_gcp_settle_outbox import _DONE_ROW_SQL

    database = Client(
        project='offline-project', credentials=AnonymousCredentials(),
        client_options={'api_endpoint': 'localhost:1'}, disable_builtin_metrics=True,
    ).instance('i').database('d')
    database._spanner_api = Mock()
    session = Session(database)
    session._session_id = 'offline-session'
    tx = session.transaction()
    tx._transaction_id = b'claimed-transaction'
    requests = []

    def stream(**kwargs: Any) -> Any:
        request = kwargs['request']
        requests.append(request)
        assert kwargs['transaction'] is tx
        response = PartialResultSet(
            metadata=ResultSetMetadata(row_type=StructType(fields=[
                StructType.Field(name='reservation_id', type_=param_types.STRING),
            ])),
            stats=ResultSetStats(row_count_exact=1),
        )
        response._pb.values.add(string_value='reservation')
        return iter([response])

    monkeypatch.setattr(snapshot, '_restart_on_unavailable', stream)
    parameters = dict(aid='auth', kind='settle', lease_owner=None, now=NOW, status='done')
    types = dict(aid=param_types.STRING, kind=param_types.STRING, lease_owner=param_types.STRING,
                 now=param_types.TIMESTAMP, status=param_types.STRING)
    for _ in range(2):
        rows = tx.execute_sql(_DONE_ROW_SQL, params=parameters, param_types=types)
        assert list(rows) == [['reservation']]
        assert rows.stats.row_count_exact == 1
    assert [request.seqno for request in requests] == [0, 1]
    assert all(request.sql == _DONE_ROW_SQL for request in requests)
    database._spanner_api.execute_streaming_sql.assert_not_called()


@pytest.mark.parametrize('abort', [False, True])
def test_done_returning_failure_rolls_back_or_retries_the_whole_finalize(
    monkeypatch: pytest.MonkeyPatch, abort: bool,
) -> None:
    from tests.fakes.spanner import FakeAborted

    db, finalize = _finalize_fixture()
    before = _state(db)
    commits = db.commits
    original = _FakeTransaction.execute_sql
    fired = False

    def execute(tx: Any, sql: str, **kwargs: Any) -> Any:
        nonlocal fired
        rows = original(tx, sql, **kwargs)
        if 'THEN RETURN' in sql and not fired:
            fired = True
            if abort:
                raise FakeAborted('done response lost')
            raise FailedPrecondition('done failure')
        return rows

    monkeypatch.setattr(_FakeTransaction, 'execute_sql', execute)
    if abort:
        assert finalize()['outcome'] == 'settled'
        assert db.aborts == 1 and db.commits == commits + 1
        assert all(row['attempts'] == 1 for row in db.settle_outbox.values())
    else:
        with pytest.raises(FailedPrecondition, match='done failure'):
            finalize()
        assert _state(db) == before and db.commits == commits
        assert not db.generation_records and not db.operational_analytics_outbox
