"""Differential proof against frozen authorize T1, including speculative failure."""
from __future__ import annotations

import copy
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.exceptions import AlreadyExists
from google.cloud.spanner_v1 import param_types
from google.rpc import code_pb2
from google.rpc.status_pb2 import Status

from tests.fakes import authorize_sequential as frozen
from tests.fakes.spanner import _FakeTransaction
from tests.fakes.spanner_order import record_statements
from tests.test_spanner_batch_dml import NOW, _authorization, _database, _state
from trusted_router import storage_gcp_authorize as current
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counter_dml import entity_insert_statement


@pytest.fixture
def stable_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(current, 'utcnow', lambda: NOW)
    monkeypatch.setattr(frozen, 'utcnow', lambda: NOW)
    monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=1))


def _options(mode: str = 'typed') -> dict[str, Any]:
    return dict(
        workspace_id='workspace', key_hash='key', estimate=100,
        has_credit_candidate=True, reservation_usage_type='Credits',
        idempotency_scope='scope', idempotency_fingerprint='fingerprint',
        expires_at=NOW + timedelta(minutes=5), request_record_write_mode=mode,
        build_authorization=_authorization,
        build_auth_body=lambda aid, rid: json_body(_authorization(aid, rid)),
    )


SCENARIOS = [
    'accepted', 'insufficient', 'missing', 'uncapped', 'byok_excluded',
    'byok_included', 'byok_insufficient', 'skip', 'no_credit', 'zero_estimate',
    'later_shard', 'missing_first_shard', 'all_missing', 'mixed_missing_exhausted',
    'rollover', 'usage_and_byok_cap', 'paused', 'receipt_unbound',
]


@pytest.mark.parametrize('scenario', SCENARIOS)
@pytest.mark.parametrize('armed', [False, True])
@pytest.mark.parametrize('mode', ['typed', 'legacy'])
@pytest.mark.parametrize('hint', [True, False], ids=['speculate', 'sequential-hint'])
def test_frozen_sequential_equivalence(
    stable_ids: None, scenario: str, armed: bool, mode: str, hint: bool,
) -> None:
    def run(module: Any) -> tuple[Any, Any]:
        db = _database()
        db.now = NOW
        opts = _options(mode)
        if module is current:
            opts['speculate_key_limit'] = hint
        opts['trust_settings'] = SimpleNamespace(spend_lease_trust_eligibility_enabled=armed)
        key = db.typed['tr_key_limit'][('key', 0)]
        if scenario == 'insufficient':
            key['limit_micro'] = 99
        elif scenario in ('missing', 'all_missing'):
            db.typed['tr_key_limit'].clear()
            if scenario == 'all_missing':
                opts['key_shard_candidates'] = (0, 1)
        elif scenario == 'uncapped':
            key['limit_micro'] = None
        elif scenario.startswith('byok_'):
            opts.update(has_credit_candidate=False, reservation_usage_type='BYOK')
            key['include_byok'] = scenario != 'byok_excluded'
            key['limit_micro'] = 0 if scenario != 'byok_included' else 1000
        elif scenario == 'skip':
            opts['skip_key_limit'] = True
            db.typed['tr_key_limit'].clear()
        elif scenario == 'no_credit':
            db.typed['tr_credit_balance'][('workspace', 0)]['total_credits'] = 0
        elif scenario == 'zero_estimate':
            opts['estimate'] = 0
        elif scenario in ('later_shard', 'missing_first_shard', 'mixed_missing_exhausted'):
            opts['key_shard_candidates'] = (0, 1)
            db.typed['tr_key_limit'][('key', 1)] = {**key, 'shard': 1}
            if scenario == 'later_shard':
                key['limit_micro'] = 0
            else:
                del db.typed['tr_key_limit'][('key', 0)]
                if scenario == 'mixed_missing_exhausted':
                    db.typed['tr_key_limit'][('key', 1)]['limit_micro'] = 0
        elif scenario == 'rollover':
            key.update(day_start=NOW - timedelta(days=40), day_usage=10000,
                       week_start=NOW - timedelta(days=40), week_usage=10000,
                       month_start=NOW - timedelta(days=40), month_usage=10000)
        elif scenario == 'usage_and_byok_cap':
            key.update(usage=400, byok_usage=450, reserved=100)
        elif scenario == 'paused':
            db.typed['tr_credit_balance'][('workspace', 0)]['billing_pause_causes'] = ['manual']
        elif scenario == 'receipt_unbound':
            opts['spend_lease_receipt_hash'] = 'receipt'

        # Simulate transactional hook bookkeeping. Its row must disappear along
        # with credit/reservation writes on rejection, and persist once on success.
        def hook(tx: Any, shard: int) -> dict[str, Any]:
            sql, params, types = entity_insert_statement(param_types, 'hook', 'id', '{}')
            tx.execute_update(sql, params=params, param_types=types)
            return {'bound': False, 'no_lease_reason': None, 'spend_lease_outcome': None}

        opts['spend_lease_hook'] = hook
        result = module.authorize_atomic(db, param_types, **opts)
        # Replay must return the stored winner and preserve every hold and row.
        if result['outcome'] == current.AuthorizeOutcome.ACCEPTED:
            before = _state(db)
            replay = module.authorize_atomic(db, param_types, **opts)
            assert replay['outcome'] == current.AuthorizeOutcome.REPLAY
            assert _state(db) == before
        return result, _state(db)

    assert run(current) == run(frozen)


@pytest.mark.parametrize('index', [1, 2])
@pytest.mark.parametrize('code', [code_pb2.ALREADY_EXISTS, code_pb2.FAILED_PRECONDITION])
def test_zero_key_count_precedes_later_insert_error(
    monkeypatch: pytest.MonkeyPatch, index: int, code: int,
) -> None:
    db = _database()
    db.typed['tr_key_limit'][('key', 0)]['limit_micro'] = 0
    before = _state(db)
    original = _FakeTransaction.batch_update

    def partial(tx: Any, statements: Any, **kwargs: Any) -> Any:
        status, counts = original(tx, statements[:index], **kwargs)
        assert status.code == 0 and counts[0] == 0
        return Status(code=code, message='later insert failure'), counts

    monkeypatch.setattr(_FakeTransaction, 'batch_update', partial)
    assert current.authorize_atomic(db, param_types, **_options()) == {
        'outcome': current.AuthorizeOutcome.KEY_LIMIT_EXCEEDED,
    }
    assert _state(db) == before
    assert db.commits == 0


def test_zero_count_retries_with_same_ids_and_releases_unique_scope(
    monkeypatch: pytest.MonkeyPatch, stable_ids: None,
) -> None:
    db = _database()
    db.typed['tr_key_limit'][('key', 0)]['limit_micro'] = None
    original = _FakeTransaction.batch_update
    batches = []

    def record(tx: Any, statements: Any, **kwargs: Any) -> Any:
        batches.append(copy.deepcopy(statements))
        return original(tx, statements, **kwargs)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', record)
    result = current.authorize_atomic(db, param_types, **_options())
    assert result['outcome'] == current.AuthorizeOutcome.ACCEPTED
    assert db.rollback_calls == 1 and db.commits == 1
    assert len(batches) == 2
    speculative, sequential = batches
    predicted = speculative[1][1]
    final = sequential[0][1]
    assert predicted == {**final, 'key_reserved_micro': 100}
    assert speculative[2] == sequential[1]
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    assert next(iter(db.reservations.values()))['key_reserved_micro'] == 0


def test_speculative_inserts_follow_credit_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_statements(monkeypatch)
    current.authorize_atomic(_database(), param_types, **_options())
    sql = [statement for _, statement in calls]
    credit = next(i for i, s in enumerate(sql) if s.startswith('update tr_credit_balance'))
    key = next(i for i, s in enumerate(sql) if s.startswith('update tr_key_limit'))
    inserts = [i for i, s in enumerate(sql) if s.startswith('insert into')]
    assert inserts and credit < key < min(inserts)


def test_already_exists_after_successful_key_uses_replay(
    monkeypatch: pytest.MonkeyPatch, stable_ids: None,
) -> None:
    db = _database()
    first = current.authorize_atomic(db, param_types, **_options())
    before = _state(db)
    read = current.read_reservation_by_idempotency
    hidden = False

    def hide_winner_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal hidden
        if not hidden:
            hidden = True
            return None
        return read(*args, **kwargs)

    monkeypatch.setattr(current, 'read_reservation_by_idempotency', hide_winner_once)
    replay = current.authorize_atomic(db, param_types, **_options())
    assert replay['outcome'] == current.AuthorizeOutcome.REPLAY
    assert replay['authorization_id'] == first['authorization_id']
    assert _state(db) == before
    assert db.rollback_calls == 1


def test_unscoped_duplicate_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def collide(*args: Any, **kwargs: Any) -> Any:
        raise AlreadyExists('unscoped collision')

    db = _database()
    monkeypatch.setattr(_FakeTransaction, 'batch_update', collide)
    with pytest.raises(AssertionError):
        current.authorize_atomic(db, param_types, **{**_options(), 'idempotency_scope': None})
    assert db.commits == 0 and not db.reservations


@pytest.mark.parametrize('elapsed', [6, 21])
def test_sequential_fallback_shares_authorize_deadline(
    monkeypatch: pytest.MonkeyPatch, elapsed: int,
) -> None:
    from google.api_core.exceptions import DeadlineExceeded

    from trusted_router import storage_gcp_io as io

    clock = [100.0]
    monkeypatch.setattr(io.time, 'monotonic', lambda: clock[0])
    db = _database()
    db.typed['tr_key_limit'][('key', 0)]['limit_micro'] = None
    original = _FakeTransaction.batch_update

    def batch(tx: Any, statements: Any, **kwargs: Any) -> Any:
        if len(statements) == 3:
            clock[0] += elapsed
        return original(tx, statements, **kwargs)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    if elapsed > 20:
        with pytest.raises(DeadlineExceeded):
            current.authorize_atomic(db, param_types, **_options())
        assert db.commits == 0 and not db.reservations
    else:
        assert current.authorize_atomic(db, param_types, **_options())['outcome'] == 'accepted'
        assert db.last_timeout_secs == 20 - elapsed
    assert db.rollback_calls == 1


def test_concurrent_authorizes_same_key_never_overreserve() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db = _database()
    db.typed['tr_credit_balance'][('workspace', 0)]['total_credits'] = 2000
    db._ready_barrier = Barrier(2)

    def authorize(index: int) -> Any:
        return current.authorize_atomic(db, param_types, **{
            **_options(), 'estimate': 700, 'idempotency_scope': f'scope-{index}',
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(authorize, range(2)))
    assert sorted(r['outcome'] for r in results) == ['accepted', 'key_limit_exceeded']
    assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 700
    assert db.typed['tr_key_limit'][('key', 0)]['reserved'] == 700
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    assert db.aborts >= 1


@pytest.mark.parametrize('change', ['funded', 'deleted', 'paused', 'resharded'])
def test_fallback_rechecks_concurrent_state_change(
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    db = _database()
    key = db.typed['tr_key_limit'][('key', 0)]
    key['limit_micro'] = 0
    rollback = _FakeTransaction.rollback
    changed = False

    def rolled_back(tx: Any) -> None:
        nonlocal changed
        rollback(tx)
        if changed:
            return
        changed = True
        if change == 'funded':
            key['limit_micro'] = 1000
        elif change == 'deleted':
            db.typed['tr_key_limit'].clear()
        elif change == 'paused':
            db.typed['tr_credit_balance'][('workspace', 0)]['billing_pause_causes'] = ['manual']
        else:
            db.typed['tr_key_limit'][('key', 1)] = {**key, 'shard': 1, 'limit_micro': 1000}
            del db.typed['tr_key_limit'][('key', 0)]

    monkeypatch.setattr(_FakeTransaction, 'rollback', rolled_back)
    result = current.authorize_atomic(db, param_types, **{
        **_options(), 'key_shard_candidates': (0, 1),
        'trust_settings': SimpleNamespace(spend_lease_trust_eligibility_enabled=True),
    })
    expected = {'funded': 'accepted', 'deleted': 'key_missing',
                'paused': 'billing_paused', 'resharded': 'accepted'}[change]
    assert result['outcome'] == expected
    assert len(db.reservations) == int(expected == 'accepted')
    if change == 'resharded':
        assert result['key_shard'] == 1


@pytest.fixture(params=[False, True], ids=['regular', 'multiplexed'])
def configured_sdk(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Real SDK Session/Transaction methods; only RPCs and telemetry are mocked."""
    from contextlib import nullcontext
    from unittest.mock import Mock

    from google.cloud.spanner_v1 import session, snapshot, transaction
    from google.cloud.spanner_v1.types import (
        CommitResponse,
        ExecuteBatchDmlResponse,
        PartialResultSet,
        ResultSet,
        ResultSetStats,
    )

    from trusted_router import storage_gcp_io as io

    clock = [100.0]
    monkeypatch.setattr(io.time, 'monotonic', lambda: clock[0])
    for module in (session, snapshot, transaction):
        monkeypatch.setattr(module, 'trace_call', lambda *a, **kw: nullcontext(Mock()))
        monkeypatch.setattr(module, 'MetricsCapture', lambda *a, **kw: nullcontext())
    api = SimpleNamespace(
        execute_sql=Mock(return_value=ResultSet(stats=ResultSetStats(row_count_exact=1))),
        execute_batch_dml=Mock(),
        execute_streaming_sql=Mock(),
        rollback=Mock(),
        commit=Mock(return_value=CommitResponse(commit_timestamp=NOW)),
    )
    db = SimpleNamespace(
        spanner_api=api, log_commit_stats=False, database_id='database',
        name='projects/project/instances/instance/databases/database',
        _instance=SimpleNamespace(instance_id='instance', _client=SimpleNamespace(
            project='project', _query_options=None, _client_context=None,
        )),
        _route_to_leader_enabled=False,
        default_transaction_options=transaction.DefaultTransactionOptions(),
        _next_nth_request=1,
        with_error_augmentation=lambda n, a, m, *rest: (m, nullcontext()),
        metadata_with_request_id=lambda n, a, m, *rest: m,
        metadata_and_request_id=lambda n, a, m, *rest: (m, None),
    )
    sdk_session = session.Session(db, is_multiplexed=request.param)
    sdk_session._session_id = 'session'
    transactions = []
    factory = sdk_session.transaction

    def new_transaction(**kwargs: Any) -> Any:
        tx = factory(**kwargs)
        transactions.append(tx)
        return tx

    monkeypatch.setattr(sdk_session, 'transaction', new_transaction)
    db.run_in_transaction = sdk_session.run_in_transaction

    def read(**kwargs: Any) -> Any:
        # Every attempt begins with the authoritative idempotency read.
        assert 'tr_reservation' in kwargs['request'].sql
        return iter([PartialResultSet(metadata={
            'transaction': {'id': f'tx-{len(transactions)}'.encode()},
            'row_type': {'fields': []},
        })])

    def batch(**kwargs: Any) -> Any:
        size = len(kwargs['request'].statements)
        return ExecuteBatchDmlResponse(
            status=Status(),
            result_sets=[ResultSet(stats=ResultSetStats(row_count_exact=n))
                         for n in ([0, 1, 1] if size == 3 else [1, 1])],
        )

    api.execute_streaming_sql.side_effect = read
    api.execute_batch_dml.side_effect = batch
    # Retain the underlying mocks: assertions count RPCs actually sent, not
    # wrapper invocations that can fail before reaching the RPC.
    rpcs = SimpleNamespace(**vars(api))
    io.configure_spanner_rpc_deadlines(db)
    return SimpleNamespace(db=db, rpcs=rpcs, clock=clock, transactions=transactions,
                           multiplexed=request.param)


@pytest.mark.parametrize('elapsed', [6, 21], ids=['remaining-budget', 'exhausted-budget'])
@pytest.mark.parametrize('cleanup', ['ok', 'failed', 'expired'])
def test_speculation_miss_configured_rollback_floor(
    configured_sdk: Any, elapsed: int, cleanup: str,
) -> None:
    from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable

    from trusted_router import storage_gcp_io as io

    sdk = configured_sdk
    batch = sdk.rpcs.execute_batch_dml.side_effect

    def spend_budget(**kwargs: Any) -> Any:
        response = batch(**kwargs)
        if len(kwargs['request'].statements) == 3:
            sdk.clock[0] += elapsed
        return response

    def rollback(**kwargs: Any) -> None:
        if cleanup == 'failed':
            raise ServiceUnavailable('rollback failed')
        if cleanup == 'expired':
            sdk.clock[0] += kwargs['timeout'] + 0.01
            raise DeadlineExceeded('rollback expired')

    sdk.rpcs.execute_batch_dml.side_effect = spend_budget
    sdk.rpcs.rollback.side_effect = rollback
    if elapsed > 20 or cleanup == 'expired':
        with pytest.raises(DeadlineExceeded) as caught:
            current.authorize_atomic(sdk.db, param_types, **_options())
        # Even at exhaustion, count an actual RPC below the deadline wrapper.
        sdk.rpcs.rollback.assert_called_once()
        # Same failure as the parent entering a transaction with no shared
        # budget. Cleanup must not turn this into a rollback error or renew T1.
        token = io._SPANNER_RPC_DEADLINE.set(sdk.clock[0] - 1)
        try:
            with pytest.raises(DeadlineExceeded) as parent_error:
                frozen.authorize_atomic(sdk.db, param_types, **_options())
        finally:
            io._SPANNER_RPC_DEADLINE.reset(token)
        assert caught.value.message == parent_error.value.message
        assert len(sdk.transactions) == 1
        sdk.rpcs.commit.assert_not_called()
    else:
        assert current.authorize_atomic(sdk.db, param_types, **_options())['outcome'] == 'accepted'
        assert len(sdk.transactions) == 2
        sdk.rpcs.commit.assert_called_once()
        # The fallback reruns both authoritative checks before inserting.
        assert sdk.rpcs.execute_streaming_sql.call_count == 2
        assert ['tr_credit_balance' in call.kwargs['request'].sql
                for call in sdk.rpcs.execute_sql.call_args_list] == [True, True, False]
        assert [len(call.kwargs['request'].statements)
                for call in sdk.rpcs.execute_batch_dml.call_args_list] == [3, 2]
    sdk.rpcs.rollback.assert_called_once()
    call = sdk.rpcs.rollback.call_args.kwargs
    assert call['transaction_id'] == b'tx-1'
    assert call['timeout'] == (io._ROLLBACK_FLOOR_SECONDS if elapsed > 20 else 20 - elapsed)
    assert sdk.transactions[0].committed is None
    assert io._SPANNER_RPC_DEADLINE.get() is None


def test_aborted_during_sequential_fallback(configured_sdk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from google.cloud.spanner_v1 import _helpers
    from google.cloud.spanner_v1.types import ExecuteBatchDmlResponse, ResultSet, ResultSetStats
    from google.protobuf.any_pb2 import Any as AnyProto
    from google.rpc.error_details_pb2 import RetryInfo

    sdk = configured_sdk
    sleep = Mock()
    monkeypatch.setattr(_helpers.time, 'sleep', sleep)
    retry_info = AnyProto()
    retry_info.Pack(RetryInfo(retry_delay={'seconds': 1}))
    sdk.rpcs.execute_batch_dml.side_effect = [
        ExecuteBatchDmlResponse(status=Status(), result_sets=[
            ResultSet(stats=ResultSetStats(row_count_exact=n)) for n in [0, 1, 1]
        ]),
        ExecuteBatchDmlResponse(status=Status(code=code_pb2.ABORTED, details=[retry_info])),
        ExecuteBatchDmlResponse(status=Status(), result_sets=[
            ResultSet(stats=ResultSetStats(row_count_exact=1)) for _ in range(2)
        ]),
    ]
    result = current.authorize_atomic(sdk.db, param_types, **_options())
    assert result['outcome'] == 'accepted'
    assert len(sdk.transactions) == 3
    assert [len(c.kwargs['request'].statements)
            for c in sdk.rpcs.execute_batch_dml.call_args_list] == [3, 2, 2]
    # ABORTED stays with the SDK; the retry retains sequential shape and IDs.
    batches = sdk.rpcs.execute_batch_dml.call_args_list
    assert batches[1].kwargs['request'].statements == batches[2].kwargs['request'].statements
    assert sdk.transactions[2]._multiplexed_session_previous_transaction_id == (
        b'tx-2' if sdk.multiplexed else None
    )
    sdk.rpcs.rollback.assert_called_once()
    assert sdk.rpcs.rollback.call_args.kwargs['transaction_id'] == b'tx-1'
    sdk.rpcs.commit.assert_called_once()
    assert sdk.rpcs.commit.call_args.kwargs['request'].transaction_id == b'tx-3'
    sleep.assert_called_once_with(1.0)


def _operation_count(db: Any) -> int:
    # T1 RPCs, excluding transaction/session acquisition, including cleanup.
    return (db.transaction_execute_sql_calls + db.transaction_execute_update_calls
            + db.transaction_batch_update_calls + db.rollback_calls + db.commits)


@pytest.mark.parametrize(('scenario', 'parent_count', 'round2_count'), [
    ('accepted', 5, 4), ('byok_excluded', 5, 5), ('uncapped_direct', 6, 6),
    ('key_rejection', 5, 9), ('credit_rejection', 3, 3), ('skip', 4, 4),
])
def test_operation_counts_with_metadata_hint(
    stable_ids: None, scenario: str, parent_count: int, round2_count: int,
) -> None:
    results = []
    for module, expected_count in ((frozen, parent_count), (current, round2_count)):
        db = _database()
        opts = _options()
        key = db.typed['tr_key_limit'][('key', 0)]
        if scenario == 'byok_excluded':
            opts.update(has_credit_candidate=False, reservation_usage_type='BYOK')
            key['include_byok'] = False
        elif scenario == 'uncapped_direct':
            key['limit_micro'] = None
        elif scenario == 'key_rejection':
            key['limit_micro'] = 0
        elif scenario == 'credit_rejection':
            db.typed['tr_credit_balance'][('workspace', 0)]['total_credits'] = 0
        elif scenario == 'skip':
            opts['skip_key_limit'] = True
        if module is current:
            opts['speculate_key_limit'] = scenario not in ('byok_excluded', 'uncapped_direct')
        result = module.authorize_atomic(db, param_types, **opts)
        assert _operation_count(db) == expected_count
        results.append((result, _state(db)))
    assert results[0] == results[1]


@pytest.mark.parametrize('stale_metadata', ['uncapped', 'byok_excluded'])
@pytest.mark.parametrize('authoritative', ['funded', 'exhausted', 'missing', 'later_shard'])
def test_stale_no_hold_hint_retains_authoritative_enforcement(
    stable_ids: None, stale_metadata: str, authoritative: str,
) -> None:
    results = []
    for module in (frozen, current):
        db = _database()
        opts = _options()
        opts['has_credit_candidate'] = stale_metadata != 'byok_excluded'
        opts['reservation_usage_type'] = 'BYOK' if stale_metadata == 'byok_excluded' else 'Credits'
        key = db.typed['tr_key_limit'][('key', 0)]
        key['include_byok'] = True
        if authoritative == 'missing':
            db.typed['tr_key_limit'].clear()
        elif authoritative == 'exhausted':
            key['limit_micro'] = 0
        elif authoritative == 'later_shard':
            opts['key_shard_candidates'] = (0, 1)
            db.typed['tr_key_limit'][('key', 1)] = {**key, 'shard': 1}
            key['limit_micro'] = 0
        if module is current:
            # Authenticated entity metadata says no hold; counter state differs.
            opts['speculate_key_limit'] = False
        result = module.authorize_atomic(db, param_types, **opts)
        if authoritative in ('funded', 'later_shard'):
            assert result['outcome'] == 'accepted'
            assert next(iter(db.reservations.values()))['key_reserved_micro'] == 100
        else:
            assert result['outcome'] == ('key_missing' if authoritative == 'missing'
                                         else 'key_limit_exceeded')
            assert not db.reservations and not db.gateway_authorizations
            assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 0
        results.append((result, _state(db), _operation_count(db)))
    assert results[0] == results[1]


@pytest.mark.parametrize('winner_change', ['same', 'fingerprint'])
def test_idempotency_winner_commits_between_speculation_and_fallback(
    monkeypatch: pytest.MonkeyPatch, winner_change: str,
) -> None:
    db = _database()
    db.typed['tr_key_limit'][('key', 0)]['limit_micro'] = None
    rollback = _FakeTransaction.rollback
    winner = None
    winner_state = None
    operations_after_winner = None

    def commit_winner(tx: Any) -> None:
        nonlocal winner, winner_state, operations_after_winner
        rollback(tx)
        # Called before the loser starts its sequential fallback. The rolled
        # back speculative INSERTs no longer own the unique idempotency scope.
        if winner is not None:
            return
        assert not db.reservations and not db.gateway_authorizations
        winner_options = _options()
        if winner_change == 'fingerprint':
            winner_options['idempotency_fingerprint'] = 'other'
        winner = frozen.authorize_atomic(db, param_types, **winner_options)
        assert winner['outcome'] == 'accepted'
        winner_state = _state(db)
        operations_after_winner = (db.transaction_execute_update_calls,
                                   db.transaction_batch_update_calls)

    monkeypatch.setattr(_FakeTransaction, 'rollback', commit_winner)
    loser = current.authorize_atomic(db, param_types, **_options())
    assert winner is not None
    assert loser['outcome'] == ('replay' if winner_change == 'same' else 'idempotency_mismatch')
    if winner_change == 'same':
        assert loser['authorization_id'] == winner['authorization_id']
        assert loser['reservation_id'] == winner['reservation_id']
    assert _state(db) == winner_state
    assert (db.transaction_execute_update_calls,
            db.transaction_batch_update_calls) == operations_after_winner
    assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 100
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    assert db.rollback_calls == (1 if winner_change == 'same' else 2)
