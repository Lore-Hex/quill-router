"""Fresh-batch differential against main 7fc31bd5 and rollback/race controls."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import DeadlineExceeded
from google.cloud.spanner_v1 import param_types
from google.rpc import code_pb2
from google.rpc.status_pb2 import Status

from tests.fakes import authorize_main_7fc31bd5 as main
from tests.fakes.spanner import _FakeTransaction
from tests.test_authorize_speculative_batch import (
    _options,
    configured_sdk,  # noqa: F401 - installed SDK fixture
)
from tests.test_gateway_authorize_spanner_operations import (
    _lookup_body,
    _request,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401
    spanner_operations,  # noqa: F401
)
from tests.test_spanner_batch_dml import NOW, _database, _inject, _state
from trusted_router import spend_windows
from trusted_router import storage_gcp_authorize as current
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway


@pytest.mark.parametrize('hint', [False, True])
def test_null_scope_never_speculates(monkeypatch, hint):
    db = _database()
    batch = _FakeTransaction.batch_update
    sizes = []

    def record(tx, statements, **kwargs):
        sizes.append(len(statements))
        return batch(tx, statements, **kwargs)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', record)
    assert current.authorize_atomic(db, param_types, **(_options() | {
        'idempotency_scope': None, 'speculate_key_limit': hint,
    }))['outcome'] == 'accepted'
    assert sizes == [2], 'NULL_FILTERED scopes must use sequential reserve checks'
    assert db.transaction_execute_update_calls == 2
    assert db.rollback_calls == 0


@pytest.mark.parametrize('index', range(4))
@pytest.mark.parametrize('count', [0, 2, None])
def test_every_row_count_mismatch_rolls_back_before_fallback(monkeypatch, index, count):
    db = _database()
    batch = _FakeTransaction.batch_update
    transactions = []

    def corrupt_once(tx, statements, **kwargs):
        if transactions:
            assert transactions[0].rolled_back, 'sequential path entered before rollback'
            assert tx is not transactions[0]
        transactions.append(tx)
        status, counts = batch(tx, statements, **kwargs)
        if len(transactions) == 1:
            assert len(statements) == 4
            if count is None:
                counts = counts[:index]
            else:
                counts[index] = count
        return status, counts

    monkeypatch.setattr(_FakeTransaction, 'batch_update', corrupt_once)
    result = current.authorize_atomic(db, param_types, **_options())
    assert result['outcome'] == 'accepted'
    assert db.rollback_calls == 1, 'row-count mismatch must roll back'
    assert len(transactions) == 2
    assert db.commits == 1
    assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 100
    assert db.typed['tr_key_limit'][('key', 0)]['reserved'] == 100
    assert len(db.reservations) == len(db.gateway_authorizations) == 1


def test_zero_credit_batch_never_commits(monkeypatch):
    db = _database()
    db.typed['tr_credit_balance'][('workspace', 0)]['total_credits'] = 0
    before = _state(db)
    assert current.authorize_atomic(db, param_types, **_options()) == {'outcome': 'insufficient_credits'}
    assert db.commits == 0, 'zero credit UPDATE committed speculative inserts'
    assert _state(db) == before
    assert db.rollback_calls == 2


@pytest.mark.parametrize('fingerprint', ['fingerprint', 'changed'])
def test_in_status_already_exists_rolls_back_before_replay(monkeypatch, fingerprint):
    db = _database()
    first = current.authorize_atomic(db, param_types, **_options())
    before = _state(db)
    batch = _FakeTransaction.batch_update
    seen = []

    def collision(tx, statements, **kwargs):
        status, counts = batch(tx, statements, **kwargs)
        assert status.code == code_pb2.ALREADY_EXISTS
        assert counts == [1, 1], 'the credit/key prefix really staged before collision'
        seen.append(tx)
        return status, counts

    read = current.read_reservation_by_idempotency

    def replay_read(tx, *args):
        assert seen and seen[0].rolled_back, 'replay must begin after protected rollback'
        assert tx is not seen[0]
        return read(tx, *args)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', collision)
    monkeypatch.setattr(current, 'read_reservation_by_idempotency', replay_read)
    result = current.authorize_atomic(db, param_types, **(_options() | {
        'idempotency_fingerprint': fingerprint,
    }))
    assert result['outcome'] == ('replay' if fingerprint == 'fingerprint' else 'idempotency_mismatch')
    if fingerprint == 'fingerprint':
        assert result['authorization_id'] == first['authorization_id']
    assert _state(db) == before
    assert db.rollback_calls == (1 if fingerprint == 'fingerprint' else 2)


@pytest.mark.parametrize('index', range(4))
@pytest.mark.parametrize('mode', ['typed', 'legacy'])
def test_abort_at_every_fresh_batch_index_retries_speculation(monkeypatch, index, mode):
    db = _database()
    batches = _inject(monkeypatch, index, 'aborted', once=True)
    result = current.authorize_atomic(db, param_types, **_options(mode))
    assert result['outcome'] == 'accepted'
    assert len(batches) == 2 and batches[0] == batches[1], 'ABORTED rerun must speculate again'
    assert len(batches[0]) == 4
    assert db.aborts == 1 and db.commits == 1 and db.rollback_calls == 0
    assert db.transaction_execute_sql_calls == db.transaction_execute_update_calls == 0
    assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 100
    assert db.typed['tr_key_limit'][('key', 0)]['reserved'] == 100


@pytest.mark.parametrize('index', range(4))
@pytest.mark.parametrize('winner', [False, True], ids=['fresh', 'same-scope-winner'])
def test_sdk_abort_rerun_keeps_speculation_and_shared_deadline(configured_sdk, monkeypatch, index, winner):  # noqa: F811 - fixture
    from google.cloud.spanner_v1 import _helpers
    from google.cloud.spanner_v1.types import (
        ExecuteBatchDmlResponse,
        PartialResultSet,
        ResultSet,
        ResultSetStats,
    )

    sdk = configured_sdk
    monkeypatch.setattr(_helpers.time, 'sleep', lambda _: None)
    batches = []

    def batch(**kwargs):
        request = kwargs['request']
        batches.append(request)
        assert len(request.statements) == 4, 'SDK rerun bypassed speculation'
        assert request.transaction.begin.read_write is not None
        attempt = len(batches)
        sdk.clock[0] += 2
        code = code_pb2.ABORTED if attempt == 1 else code_pb2.ALREADY_EXISTS if winner else 0
        size = index if attempt == 1 else 2 if winner else 4
        # At index zero no result set exists: ABORTED must still retry.
        return ExecuteBatchDmlResponse(status=Status(code=code), result_sets=[
            ResultSet(metadata={'transaction': {'id': f'tx-{attempt}'.encode()}},
                      stats=ResultSetStats(row_count_exact=1)) for _ in range(size)
        ])

    sdk.rpcs.execute_batch_dml.side_effect = batch
    if winner:
        def read(**kwargs):
            assert sdk.rpcs.rollback.call_count == 1, 'fallback read preceded rollback'
            assert 'tr_reservation' in kwargs['request'].sql
            types = ['STRING', 'INT64', 'INT64', 'STRING', 'STRING', 'STRING', 'BOOL', 'INT64', 'INT64', 'INT64']
            response = PartialResultSet(metadata={
                'transaction': {'id': b'tx-3'},
                'row_type': {'fields': [{'name': f'c{i}', 'type': {'code': kind}}
                                        for i, kind in enumerate(types)]},
            })
            for value in ['winner-res', '100', '100', 'Credits', 'winner-auth', 'fingerprint', False, '0', '0', '0']:
                response._pb.values.add(**({'bool_value': value} if isinstance(value, bool) else {'string_value': value}))
            return iter([response])
        sdk.rpcs.execute_streaming_sql.side_effect = read
    result = current.authorize_atomic(sdk.db, param_types, **_options())
    assert result['outcome'] == ('replay' if winner else 'accepted')
    assert len(batches) == 2 and batches[0].statements == batches[1].statements
    assert len(sdk.transactions) == (3 if winner else 2)
    assert sdk.rpcs.rollback.call_count == int(winner)
    assert sdk.rpcs.commit.call_count == 1
    assert sdk.rpcs.execute_sql.call_count == 0
    assert sdk.rpcs.execute_streaming_sql.call_count == int(winner)
    assert [c.kwargs['timeout'] for c in sdk.rpcs.execute_batch_dml.call_args_list] == [16, 14]
    assert sdk.rpcs.commit.call_args.kwargs['timeout'] == (16 if winner else 12)


@pytest.mark.parametrize('armed', [False, True])
def test_concurrent_same_scope_abort_loser_replays_without_second_hold(armed):
    db = _database()
    db._ready_barrier = Barrier(2)
    opts = _options() | {'trust_settings': Settings(spend_lease_trust_eligibility_enabled=armed)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: current.authorize_atomic(db, param_types, **opts), range(2)))
    assert sorted(r['outcome'] for r in results) == ['accepted', 'replay']
    assert results[0]['authorization_id'] == results[1]['authorization_id']
    assert results[0]['reservation_id'] == results[1]['reservation_id']
    assert db.aborts >= 1
    assert db.rollback_calls == 1
    assert db.transaction_batch_update_calls == 3, 'loser must speculate on SDK rerun'
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 100
    assert db.typed['tr_key_limit'][('key', 0)]['reserved'] == 100


ROUTE_SCENARIOS = [
    'credits', 'stage-d', 'byok', 'strict', 'strict-window-exceeded', 'window', 'window-exceeded',
    'uncapped', 'key-exceeded', 'credit-exceeded', 'paused', 'paused-credit-exceeded',
    'paused-key-exceeded', 'replay', 'mismatch', 'replay-exhausted', 'replay-paused',
    'delayed-key-zero-credit', 'delayed-key-funded', 'replay-missing-authorization', 'null-scope', 'abort-rerun', 'race-already-exists', 'race-aborted',
]


@pytest.mark.parametrize('armed', [False, True])
@pytest.mark.parametrize('scenario', ROUTE_SCENARIOS)
def test_gateway_money_differential_against_main(monkeypatch, fixed_operation_catalog, scenario, armed):  # noqa: F811 - fixture
    """Compare HTTP status/body/headers and every durable money/request/outbox row.

    Only the additive timing payload is excluded: fewer RPCs is intentional.
    Both implementations receive the hint production computes from the key.
    """
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(dt, 'datetime', FixedDateTime)
    monkeypatch.setattr(current, 'utcnow', lambda: NOW)
    monkeypatch.setattr(main, 'utcnow', lambda: NOW)
    monkeypatch.setattr(spend_windows, 'utcnow', lambda: NOW)
    from trusted_router import storage_gcp_strict_budget
    monkeypatch.setattr(storage_gcp_strict_budget, 'utcnow', lambda: NOW)
    store, db, key = _seed_typed_gateway_store()
    store.trust_settings = Settings(environment='test', spend_lease_trust_eligibility_enabled=armed)
    body = _lookup_body(key)
    route_request, raw_body = _request(), None
    route_settings = Settings(environment='test')
    if scenario == 'stage-d':
        from tests.conformance.test_gateway_auth_boot_fold import signed_request
        route_request, body, raw_body, boot = signed_request(store, key)
        route_settings = Settings(environment='test', stage_d_eligibility_enabled=True,
                                  stage_d_pilot_workspace_ids='',
                                  spend_lease_accepted_gcp_image_digests=boot.image_digest)
    if scenario == 'byok':
        store.upsert_byok_provider(workspace_id=key.workspace_id, provider='anthropic',
                                   secret_ref='fixture/byok', key_hint='test')  # noqa: S106
        body.provider = {'usage': 'byok'}
    if scenario == 'uncapped':
        store.update_key(key.hash, {'limit_microdollars': None})
    if scenario.startswith('strict'):
        store.update_key(key.hash, {'budget_strict': True})
    if 'window' in scenario:
        store.update_key(key.hash, {'limit_daily_microdollars': 1 if 'exceeded' in scenario else 1_000_000})
    credit = db.typed['tr_credit_balance'][(key.workspace_id, 0)]
    cap = db.typed['tr_key_limit'][(key.hash, 0)]
    if 'credit-exceeded' in scenario or scenario == 'delayed-key-zero-credit':
        credit['total_credits'] = 0
    if 'key-exceeded' in scenario:
        cap['limit_micro'] = 0
    if scenario.startswith('paused'):
        credit['billing_pause_causes'] = ['manual']
    if scenario == 'null-scope':
        body.idempotency_key = None
    initial = {name: copy.deepcopy(value) for name, value in vars(db).items() if isinstance(value, dict)}
    original = current.authorize_atomic
    results = []
    original_batch = _FakeTransaction.batch_update
    original_update = _FakeTransaction.execute_update
    from trusted_router import storage_gcp_io as io
    clock = [100.0]
    if scenario.startswith('delayed-key'):
        monkeypatch.setattr(io.time, 'monotonic', lambda: clock[0])
    for implementation in (main.authorize_atomic, original):
        for name, value in initial.items():
            setattr(db, name, copy.deepcopy(value))
        store._lifetime_cap_exhausted_keys.discard(key.hash)
        counter = iter(range(1, 1000))
        monkeypatch.setattr(uuid, 'uuid4', lambda counter=counter: uuid.UUID(int=next(counter)))
        request_body = body.model_copy(deep=True)
        aborted = False
        winner = None
        clock[0] = 100.0
        key_waits = []
        before_delay = _state(db)

        def delayed_update(tx, sql, key_waits=key_waits, **kwargs):
            if scenario.startswith('delayed-key') and 'UPDATE tr_key_limit' in sql:
                remaining = io.remaining_rpc_budget(20)
                key_waits.append(remaining)
                clock[0] += remaining
                raise DeadlineExceeded('injected key lock wait')
            return original_update(tx, sql, **kwargs)

        def dispatch(*args, implementation=implementation, **kwargs):
            return implementation(*args, **kwargs)

        def batch(tx, statements, **kwargs):
            nonlocal aborted, winner
            if scenario in ('race-already-exists', 'race-aborted') and winner is None:
                # Both transaction callbacks are live. Let the competing request
                # commit before resuming this loser's batch (a deterministic
                # concurrency schedule, independently covered with two threads).
                winner = {}  # prevent recursively scheduling another winner
                winner = gateway._authorize_gateway_sync(_request(), body, Settings(environment='test'))
                if scenario == 'race-aborted':
                    aborted = True
                    return Status(code=code_pb2.ABORTED), []
            if scenario == 'abort-rerun' and not aborted:
                aborted = True
                return Status(code=code_pb2.ABORTED), []
            return original_batch(tx, statements, **kwargs)

        monkeypatch.setattr(_FakeTransaction, 'execute_update', delayed_update)
        monkeypatch.setattr(current, 'authorize_atomic', dispatch)
        monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
        settings = route_settings
        if scenario in ('replay', 'mismatch', 'replay-exhausted', 'replay-paused', 'replay-missing-authorization'):
            winner = gateway._authorize_gateway_sync(_request(), request_body, settings)
            if scenario == 'replay-missing-authorization':
                db.gateway_authorizations.clear()
                replay_state = _state(db)
            if scenario == 'mismatch':
                request_body.max_output_tokens += 1
            elif scenario == 'replay-exhausted':
                db.typed['tr_credit_balance'][(key.workspace_id, 0)]['total_credits'] = 0
                db.typed['tr_key_limit'][(key.hash, 0)]['limit_micro'] = 0
            elif scenario == 'replay-paused':
                db.typed['tr_credit_balance'][(key.workspace_id, 0)]['billing_pause_causes'] = ['manual']
        try:
            response = gateway._authorize_gateway_sync(route_request, request_body, settings, raw_body)
            status, headers = 200, None
        except HTTPException as exc:
            response, status, headers = exc.detail, exc.status_code, exc.headers
        except DeadlineExceeded as exc:
            from trusted_router.main import app
            http_response = asyncio.run(app.exception_handlers[DeadlineExceeded](route_request, exc))
            response = json.loads(http_response.body)
            status, headers = http_response.status_code, dict(http_response.headers)
        response = copy.deepcopy(response)
        response.get('data', {}).pop('timing', None)
        state = copy.deepcopy((db.typed, {k: v.body for k, v in db.rows.items()},
                               db.reservations, db.gateway_authorizations, db.settle_outbox,
                               db.operational_analytics_outbox, db.analytics_outbox))
        results.append((status, response, headers, state))
        if scenario.startswith('delayed-key'):
            assert _state(db) == before_delay, 'key delay must leave all rows untouched'
            if scenario == 'delayed-key-zero-credit':
                assert status == 402 and headers is None
                assert len(key_waits) == int(implementation is original and not armed)
                assert clock[0] <= 116
            else:
                assert status == 503 and headers['retry-after'] == '1'
                assert key_waits, 'funded main must also wait on the key'
                assert clock[0] == 120, 'fallback must not renew the 20s budget'
        if scenario == 'credit-exceeded':
            assert status == 402 and not db.reservations
        if scenario == 'key-exceeded':
            assert status == 402 and not db.reservations
        if scenario in ('window-exceeded', 'strict-window-exceeded'):
            assert status == 429
        if scenario == 'mismatch':
            assert status == 409
        if scenario in ('credits', 'byok', 'strict', 'window', 'uncapped', 'null-scope', 'abort-rerun'):
            assert status == 200
        if scenario == 'stage-d':
            assert status == 200 and response['data']['stage_d'] == {'eligible': True, 'reason': 'ok'}
        if scenario == 'paused':
            assert status == (403 if armed else 200)
        if scenario == 'replay-missing-authorization':
            assert status == 500
            assert _state(db) == replay_state, 'missing authorization must not create rows or holds'
        if winner is not None and scenario not in ('mismatch', 'replay-missing-authorization'):
            assert response['data']['authorization_id'] == winner['data']['authorization_id']
            assert len(db.reservations) == 1
    assert results[0] == results[1]


@pytest.mark.parametrize('armed', [False, True])
@pytest.mark.parametrize('balance', ['later-funded', 'missing-first', 'all-empty', 'all-missing'])
def test_credit_candidate_fallback_matches_main(monkeypatch, armed, balance):
    monkeypatch.setattr(current, 'utcnow', lambda: NOW)
    monkeypatch.setattr(main, 'utcnow', lambda: NOW)
    monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=1))
    results = []
    for implementation in (main.authorize_atomic, current.authorize_atomic):
        db = _database()
        credits = db.typed['tr_credit_balance']
        credits[('workspace', 1)] = credits[('workspace', 0)] | {'shard': 1}
        if balance.startswith('all'):
            credits[('workspace', 1)]['total_credits'] = 0
        credits[('workspace', 0)]['total_credits'] = 0
        if balance in ('missing-first', 'all-missing'):
            del credits[('workspace', 0)]
        if balance == 'all-missing':
            del credits[('workspace', 1)]
        result = implementation(db, param_types, **(_options() | {
            'credit_shard_candidates': (0, 1),
            'trust_settings': Settings(spend_lease_trust_eligibility_enabled=armed),
            'speculate_key_limit': True,
        }))
        if not balance.startswith('all'):
            assert result['outcome'] == 'accepted' and result['credit_shard'] == 1
        else:
            assert result['outcome'] == 'insufficient_credits'
        results.append((result, _state(db)))
    assert results[0] == results[1]


@pytest.mark.parametrize('armed', [False, True])
def test_warm_lookup_replay_exact_sequence(armed, fixed_operation_catalog, spanner_operations):  # noqa: F811 - fixtures
    store, db, key = _seed_typed_gateway_store()
    store.trust_settings = Settings(environment='test', spend_lease_trust_eligibility_enabled=armed)
    settings = Settings(environment='test')
    first = gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)
    spanner_operations.clear()
    before = db.rollback_calls
    replay = gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)
    assert replay['data']['idempotent_replay']
    assert replay['data']['authorization_id'] == first['data']['authorization_id']
    assert [op[0] for op in spanner_operations] == (
        ['RO'] + (['T1 DML', 'T1 SELECT'] if armed else [])
        + ['T1 BATCH', 'ROLLBACK', 'T1 SELECT', 'COMMIT', 'RO']
    )
    assert db.rollback_calls == before + 1
    assert len(spanner_operations) == 6 + 2 * int(armed)
    assert 'tr_reservation' in spanner_operations[-3][1]
    assert 'tr_gateway_authorization' in spanner_operations[-1][1]


def test_sdk_fresh_timing_counts_batch_and_commit(configured_sdk):  # noqa: F811 - fixture
    from google.cloud.spanner_v1.types import ExecuteBatchDmlResponse, ResultSet, ResultSetStats

    from trusted_router.storage_gcp_io import count_spanner_rpcs

    sdk = configured_sdk
    sdk.rpcs.execute_batch_dml.side_effect = None
    sdk.rpcs.execute_batch_dml.return_value = ExecuteBatchDmlResponse(
        status=Status(), result_sets=[ResultSet(
            metadata={'transaction': {'id': b'tx-1'}}, stats=ResultSetStats(row_count_exact=1),
        ) for _ in range(4)],
    )
    with count_spanner_rpcs() as counter:
        assert current.authorize_atomic(sdk.db, param_types, **_options())['outcome'] == 'accepted'
    assert counter.count == 2  # gateway's preceding strong auth snapshot adds one
    assert sdk.rpcs.execute_streaming_sql.call_count == sdk.rpcs.execute_sql.call_count == 0


@pytest.mark.parametrize('cleanup_fails', [False, True])
@pytest.mark.parametrize('transport_error', [False, True])
def test_deadline_cleanup_failure_still_classifies_in_new_transaction(configured_sdk, cleanup_fails, transport_error):  # noqa: F811
    """Real SDK drops failed callbacks even if rollback cannot release their locks."""
    from google.api_core.exceptions import ServiceUnavailable
    from google.cloud.spanner_v1.types import ExecuteBatchDmlResponse, ResultSet, ResultSetStats

    from trusted_router import storage_gcp_io as io

    sdk = configured_sdk

    def blocked_batch(**kwargs):
        assert kwargs['timeout'] == 16
        sdk.clock[0] += kwargs['timeout']
        if transport_error:
            sdk.transactions[-1]._transaction_id = b'tx-1'
            raise DeadlineExceeded('injected transport timeout after staging credit')
        # A staged credit prefix and transaction ID really exist at the failure.
        return ExecuteBatchDmlResponse(status=Status(code=code_pb2.DEADLINE_EXCEEDED), result_sets=[
            ResultSet(metadata={'transaction': {'id': b'tx-1'}},
                      stats=ResultSetStats(row_count_exact=1)),
        ])

    def cleanup(**kwargs):
        if kwargs['transaction_id'] != b'tx-1':
            return
        assert kwargs['timeout'] == 2
        sdk.clock[0] += 2
        if cleanup_fails:
            raise ServiceUnavailable('injected cleanup failure')

    sdk.rpcs.execute_batch_dml.side_effect = blocked_batch
    sdk.rpcs.rollback.side_effect = cleanup
    # The authoritative credit check rejects before any key access or INSERT.
    sdk.rpcs.execute_sql.return_value = ResultSet(stats=ResultSetStats(row_count_exact=0))
    assert current.authorize_atomic(sdk.db, param_types, **_options()) == {
        'outcome': 'insufficient_credits',
    }
    assert len(sdk.transactions) == 2 and sdk.transactions[0] is not sdk.transactions[1]
    assert all(tx.committed is None for tx in sdk.transactions)
    sdk.rpcs.commit.assert_not_called()
    assert sdk.rpcs.execute_batch_dml.call_count == 1
    assert sdk.rpcs.execute_streaming_sql.call_args.kwargs['timeout'] == 2
    assert sdk.rpcs.execute_sql.call_count == 1
    assert 'tr_credit_balance' in sdk.rpcs.execute_sql.call_args.kwargs['request'].sql
    assert sdk.clock[0] <= 120
    assert io._SPANNER_RPC_DEADLINE.get() is None


def test_rollback_then_sequential_lock_order_has_separate_transactions(monkeypatch):
    """Prove per-transaction credit/key order across fallback, not lock release,
    unique-index ordering, real lock waits, or production deadlock freedom.
    """
    from tests.fakes.lock_order import recorder

    recorder.reset()
    db = _database()
    db.typed['tr_key_limit'][('key', 0)]['limit_micro'] = None
    read = current.read_reservation_by_idempotency

    def after_cleanup(tx, *args):
        assert db.rollback_calls == 1
        return read(tx, *args)

    monkeypatch.setattr(current, 'read_reservation_by_idempotency', after_cleanup)
    assert current.authorize_atomic(db, param_types, **_options())['outcome'] == 'accepted'
    traces = list(recorder._tx.values())
    assert len(traces) == 2
    for steps in traces:
        classes = [kind for kinds, _ in steps for kind in kinds]
        assert classes[0] == 'credit' and classes[-1] == 'key'
    assert recorder.both_tables_seen == 2
    recorder.check('rolled-back speculation followed by sequential transaction')
