"""C3 differential: frozen sequential money path and returning-DML transport."""
from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from google.cloud.spanner_v1 import param_types

from tests.fakes import authorize_pause_sequential as frozen
from tests.fakes.spanner import _FakeTransaction
from tests.test_authorize_speculative_batch import _options, configured_sdk  # noqa: F401
from tests.test_spanner_batch_dml import NOW, _database, _state
from trusted_router import storage_gcp_authorize as current
from trusted_router import trust_eligibility
from trusted_router.storage_gcp_counter_dml import reserve_credit_with_pause


@pytest.mark.parametrize('armed', [False, True])
@pytest.mark.parametrize('causes', [None, [], '', '[]', ['abuse'], '["abuse"]', ' [ ] ', '{}'])
@pytest.mark.parametrize('scenario', [
    'funded', 'later_funded', 'later_paused', 'insufficient', 'missing',
    'byok', 'strict', 'strict_window', 'zero', 'key_exhausted',
])
def test_pause_fold_frozen_differential(monkeypatch, armed, causes, scenario):
    monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=1))
    for module in (frozen, current):
        monkeypatch.setattr(module, 'utcnow', lambda: NOW)
    from trusted_router import storage_gcp_strict_budget
    monkeypatch.setattr(storage_gcp_strict_budget, 'utcnow', lambda: NOW)
    results = []
    for module in (frozen, current):
        db = _database()
        opts = _options()
        opts['trust_settings'] = SimpleNamespace(spend_lease_trust_eligibility_enabled=armed)
        credit = db.typed['tr_credit_balance'][('workspace', 0)]
        credit.update(billing_pause_causes=copy.deepcopy(causes), pause_epoch=17)
        if scenario.startswith('later_'):
            opts['credit_shard_candidates'] = (0, 1)
            credit['total_credits'] = 0
            # Contradictory shard evidence intentionally detects the wrong row.
            db.typed['tr_credit_balance'][('workspace', 1)] = {
                **credit, 'shard': 1, 'total_credits': 1000,
                'billing_pause_causes': ['later'] if scenario == 'later_paused' else [],
            }
        elif scenario == 'insufficient':
            credit['total_credits'] = 0
        elif scenario == 'missing':
            db.typed['tr_credit_balance'].clear()
        elif scenario == 'byok':
            opts.update(has_credit_candidate=False, reservation_usage_type='BYOK')
        elif scenario.startswith('strict'):
            opts['strict_budget'] = True
            if scenario == 'strict_window':
                from trusted_router.spend_windows import window_floors
                floors = window_floors(NOW)
                db.typed['tr_key_limit'][('key', 0)].update(
                    day_start=floors['daily'], week_start=floors['weekly'], month_start=floors['monthly'],
                    day_limit_micro=50, day_usage=0,
                )
        elif scenario == 'zero':
            opts['estimate'] = 0
        elif scenario == 'key_exhausted':
            db.typed['tr_key_limit'][('key', 0)]['limit_micro'] = 0
        before = _state(db)
        result = module.authorize_atomic(db, param_types, **opts)
        if scenario in ('missing', 'insufficient'):
            assert result['outcome'] == 'insufficient_credits'
        if result['outcome'] != 'accepted':
            assert _state(db) == before
        else:
            held = _state(db)
            # Replay precedes even a newly committed pause and exhausted credit.
            for row in db.typed['tr_credit_balance'].values():
                row.update(billing_pause_causes=['new-pause'], pause_epoch=18)
            replay_state = _state(db)
            replay = module.authorize_atomic(db, param_types, **opts)
            assert replay['outcome'] == 'replay'
            assert _state(db) == replay_state
            results.append((replay, held))
        results.append((result, _state(db)))
    half = len(results) // 2
    assert results[:half] == results[half:]


@pytest.mark.parametrize('causes', [None, [], '', '[]', ['abuse'], ' [ ] '])
@pytest.mark.parametrize('epoch', [None, 0, 37])
def test_reader_and_returned_row_use_same_predicate(causes, epoch):
    row = [causes, epoch]
    reader = SimpleNamespace(execute_sql=lambda *a, **kw: [row])
    expected = str(causes or '') not in ('', '[]')
    assert trust_eligibility.billing_paused_row(row) == expected
    assert trust_eligibility.billing_paused_tx(reader, param_types, 'workspace', shard=0) == expected


@pytest.mark.parametrize('funded', [False, True])
def test_fake_returning_only_affected_row(funded):
    db = _database()
    row = db.typed['tr_credit_balance'][('workspace', 0)]
    row.update(billing_pause_causes=['abuse'], pause_epoch=23, total_credits=1000 if funded else 0)
    db.typed['tr_credit_balance'][('workspace', 1)] = {**row, 'shard': 1, 'pause_epoch': 99}
    tx = _FakeTransaction(db)
    result = reserve_credit_with_pause(tx, param_types, 'workspace', 100)
    assert result == (funded, funded)
    assert db.transaction_execute_sql_calls == 1
    assert db.transaction_execute_update_calls == 0
    assert len(tx.pending_writes) == int(funded)
    assert set(tx.read_versions) == {('typed', 'tr_credit_balance', ('workspace', 0))}


@pytest.mark.parametrize('paused', [False, True])
@pytest.mark.parametrize('funded', [False, True])
def test_installed_sdk_returning_dml_one_rpc(configured_sdk, paused, funded):  # noqa: F811 - pytest fixture
    from google.cloud.spanner_v1.types import PartialResultSet

    from trusted_router.storage_gcp_io import count_spanner_rpcs

    sdk = configured_sdk
    def stream(**kwargs):
        response = PartialResultSet(
            metadata={
                'transaction': {'id': b'returning'},
                'row_type': {'fields': [
                    {'name': 'billing_pause_causes', 'type_': {'code': 'ARRAY', 'array_element_type': {'code': 'STRING'}}},
                    {'name': 'pause_epoch', 'type_': {'code': 'INT64'}},
                ]},
            }, stats={'row_count_exact': int(funded)},
        )
        if funded:
            array = response._pb.values.add().list_value
            array.SetInParent()
            if paused:
                array.values.add(string_value='abuse')
            response._pb.values.add(string_value='17')
        return iter([response])

    sdk.rpcs.execute_streaming_sql.side_effect = stream
    with count_spanner_rpcs() as counter:
        result = sdk.db.run_in_transaction(
            lambda tx: reserve_credit_with_pause(tx, param_types, 'workspace', 100),
        )
    assert result == (funded, paused and funded)
    assert counter.value() == 2  # one streaming DML, one commit; inline begin
    sdk.rpcs.execute_sql.assert_not_called()
    sdk.rpcs.execute_streaming_sql.assert_called_once()
    request = sdk.rpcs.execute_streaming_sql.call_args.kwargs['request']
    assert request.sql.endswith('THEN RETURN billing_pause_causes, pause_epoch')
    assert request.transaction.begin.read_write is not None


def test_reserve_credit_with_pause_drains_the_returning_stream():
    """The THEN RETURN result must be consumed to completion, not peeked.

    Spanner finalizes a DML statement's result (and its stats) only when the
    stream is drained; a first-row peek would also misread a stream whose first
    chunk is empty. Review P3: a first-row-only consumer survived every test.
    """
    drained = []

    def stream():
        yield [['abuse'], 17]
        drained.append(True)

    class _Txn:
        def execute_sql(self, sql, params, param_types):  # noqa: ARG002 - transport shape
            assert 'THEN RETURN billing_pause_causes, pause_epoch' in sql
            return stream()

    assert reserve_credit_with_pause(_Txn(), param_types, 'workspace', 5, shard=0) == (True, True)
    assert drained == [True]

    def empty_then_row():
        yield []
        yield [['abuse'], 17]

    class _ChunkedTxn(_Txn):
        def execute_sql(self, sql, params, param_types):  # noqa: ARG002
            return empty_then_row()

    with pytest.raises(IndexError):
        # An empty leading row is not a Spanner row shape; the verdict must not
        # silently fall through to "unpaused" by reading only the first item.
        reserve_credit_with_pause(_ChunkedTxn(), param_types, 'workspace', 5, shard=0)
