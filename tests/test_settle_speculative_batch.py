"""Frozen T3 differential and speculative transaction disposal contracts."""
from __future__ import annotations

import copy
import logging
from typing import Any

import pytest
from google.api_core.exceptions import AlreadyExists, FailedPrecondition, ServiceUnavailable
from google.cloud.spanner_v1 import param_types
from google.rpc import code_pb2
from google.rpc.status_pb2 import Status

from tests.fakes import settle_finalize_sequential as frozen
from tests.fakes.spanner import _FakeTransaction
from tests.fakes.spanner_order import credit_before_key, record_statements, transaction_statements
from tests.test_spanner_batch_dml import NOW, _authorization, _authorize, _database, _state
from trusted_router import storage_gcp_authorize as current
from trusted_router import storage_gcp_settle_outbox as outbox
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_generation_records import generation_record_body
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
)
from trusted_router.storage_models import Generation, SettleOutboxRow


def fixture() -> tuple[Any, dict[str, Any]]:
    db = _database()
    db.now = NOW
    accepted = _authorize(db)
    aid, rid = accepted['authorization_id'], accepted['reservation_id']
    auth = _authorization(aid, rid)
    gen = Generation.from_settle_body(
        authorization=auth, provider_name='provider', model_id='model', usage_type='Credits',
        provider='provider', body={}, input_tokens=5, output_tokens=7, actual_cost_microdollars=70,
    )
    gen.id, gen.created_at = 'stable-generation', NOW.isoformat()
    auth.record_finalization(success=True, actual_microdollars=70,
                             selected_usage_type='Credits', generation=gen)
    outbox.SpannerSettleOutbox(db, param_types).enqueue(SettleOutboxRow(
        authorization_id=aid, reservation_id=rid, intent_kind='settle', settle_origin='typed',
        actual_cost_micro=70, settle_body='{"repair":"keep until done"}', attempts=3,
    ))
    return db, dict(
        reservation_id=rid, authorization_id=aid, success=True, actual_micro=70,
        settled_usage_type='Credits', now=NOW, outbox_available=True, authorization=auth,
        auth_body_settled=json_body(auth), generation=gen, persist_generation_record=True,
        generation_writes=[('generation', gen.id, generation_record_body(gen))],
        settle_outbox_done=(aid, 'settle'),
    )


def clone(db: Any) -> Any:
    other = _database()
    other.now = NOW
    for name in ('typed', 'rows', 'reservations', 'gateway_authorizations', 'settle_outbox',
                 'generation_records', 'operational_analytics_outbox'):
        setattr(other, name, copy.deepcopy(getattr(db, name)))
    return other


def state(db: Any) -> Any:
    return (_state(db), copy.deepcopy(db.generation_records),
            copy.deepcopy(db.operational_analytics_outbox))


def invoke(db: Any, options: dict[str, Any], impl: Any = current.typed_finalize_atomic) -> Any:
    return impl(db, param_types, operational_analytics_outbox=SpannerOperationalAnalyticsOutbox(
        db, param_types,
    ), **options)


SCENARIOS = [
    'fresh', 'charged', 'refund_winner', 'free_reaper', 'snapshot_reaper', 'missing',
    'typed_absent', 'typed_terminal', 'legacy', 'overlap',
    'leased', 'done', 'dead', 'absent', 'foreign', 'foreign_guarded', 'empty', 'null',
    'missing_target', 'wrong_identity', 'refund', 'no_done', 'no_outbox',
]


@pytest.mark.parametrize('scenario', SCENARIOS)
@pytest.mark.parametrize('sibling', ['absent', 'pending', 'dead', 'done', 'release_approved'])
@pytest.mark.parametrize('armed', [False, True])
def test_complete_finalize_differential(
    monkeypatch: pytest.MonkeyPatch, scenario: str, sibling: str, armed: bool,
) -> None:
    monkeypatch.setattr(outbox, '_iso_now', lambda: NOW.isoformat())
    initial, options = fixture()
    aid, rid = options['authorization_id'], options['reservation_id']
    auth, res = initial.gateway_authorizations[aid], initial.reservations[rid]
    auth['terminal_at'] = res['terminal_at'] = NOW if armed else None
    intent = initial.settle_outbox[(aid, 'settle')]
    if sibling != 'absent':
        initial.settle_outbox[(aid, 'refund')] = dict(intent, intent_kind='refund', status=sibling)
    if scenario in {'charged', 'refund_winner', 'free_reaper', 'snapshot_reaper'}:
        # A real prior winner releases the holds; then exercise the stale input.
        winner = dict(options)
        if scenario in {'refund_winner', 'free_reaper'}:
            refunded = copy.deepcopy(options['authorization'])
            refunded.record_finalization(
                success=False, actual_microdollars=0, selected_usage_type='Credits', generation=None,
            )
            winner.update(success=False, actual_micro=0, generation=None, authorization=refunded,
                          auth_body_settled=json_body(refunded))
        invoke(initial, winner, frozen.typed_finalize_atomic)
    elif scenario == 'missing':
        del initial.reservations[rid]
    elif scenario in {'legacy', 'overlap', 'typed_absent', 'typed_terminal'}:
        if scenario in {'legacy', 'overlap'}:
            from tests.fakes.spanner import _Row
            initial.rows[('gateway_authorization', aid)] = _Row(json_body(options['authorization']), 1)
        if scenario in {'legacy', 'typed_absent'}:
            del initial.gateway_authorizations[aid]
        if scenario == 'legacy':
            options['authorization'] = None
        if scenario == 'typed_terminal':
            auth['settled'] = True
    elif scenario == 'leased':
        intent.update(lease_owner='worker', leased_until=NOW)
    elif scenario in {'done', 'dead'}:
        intent['status'] = scenario
    elif scenario == 'absent':
        del initial.settle_outbox[(aid, 'settle')]
    elif scenario in {'foreign', 'foreign_guarded', 'missing_target', 'empty', 'null'}:
        intent['reservation_id'] = {'empty': '', 'null': None}.get(scenario, 'foreign')
        if scenario.startswith('foreign'):
            initial.reservations['foreign'] = dict(res, reservation_id='foreign',
                                                  authorization_id='other', settled=True)
        if scenario == 'foreign_guarded':
            initial.settle_outbox[('other', 'settle')] = dict(intent, authorization_id='other')
    elif scenario == 'wrong_identity':
        options['settle_outbox_done'] = ('other', 'settle')
    elif scenario == 'refund':
        options.update(success=False, actual_micro=0, generation=None)
    elif scenario == 'no_done':
        options['settle_outbox_done'] = None
    elif scenario == 'no_outbox':
        options['outbox_available'] = False
    observations = []
    for impl in (frozen.typed_finalize_atomic, current.typed_finalize_atomic):
        db = clone(initial)
        try:
            result = invoke(db, copy.deepcopy(options), impl)
            result.pop('attempts', None)
        except Exception as exc:
            result = (type(exc), str(exc))
        observations.append((result, state(db)))
    assert observations[0] == observations[1]


@pytest.mark.parametrize('index', range(7))
@pytest.mark.parametrize('failure', ['sql', 'transport', 'count', 'truncated', 'duplicate'])
def test_every_successful_prefix_is_discarded(
    monkeypatch: pytest.MonkeyPatch, index: int, failure: str,
) -> None:
    db, options = fixture()
    before, commits = state(db), db.commits
    original = _FakeTransaction.batch_update

    def batch(tx: Any, statements: Any, **kw: Any) -> Any:
        status, counts = original(tx, statements[:index], **kw)
        assert status.code == 0
        if failure == 'transport':
            raise ServiceUnavailable('transport')
        if failure == 'truncated':
            return Status(), counts
        if failure == 'count':
            _, tail = original(tx, statements[index:], **kw)
            return Status(), counts + [2] + tail[1:]
        return Status(code=code_pb2.ALREADY_EXISTS if failure == 'duplicate'
                      else code_pb2.FAILED_PRECONDITION, message='injected'), counts

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    error = {'transport': ServiceUnavailable, 'duplicate': AlreadyExists}.get(failure, FailedPrecondition)
    with pytest.raises(error):
        invoke(db, options)
    assert state(db) == before and db.commits == commits and db.rollback_calls == 1


@pytest.mark.parametrize('zero', [None, 0, 1, 2])
@pytest.mark.parametrize('index', range(7))
def test_aborted_precedes_even_zero_prefix_and_retries_entire_transaction(
    monkeypatch: pytest.MonkeyPatch, index: int, zero: int | None,
) -> None:
    monkeypatch.setattr(outbox, '_iso_now', lambda: NOW.isoformat())
    db, options = fixture()
    original = _FakeTransaction.batch_update
    batches = []
    calls = record_statements(monkeypatch)

    def batch(tx: Any, statements: Any, **kw: Any) -> Any:
        batches.append(copy.deepcopy(statements))
        if len(batches) == 1:
            _, counts = original(tx, statements[:index], **kw)
            if zero is not None and zero < len(counts):
                counts[zero] = 0
            return Status(code=code_pb2.ABORTED), counts
        return original(tx, statements, **kw)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    result = invoke(db, options)
    assert result['outcome'] == 'settled' and result['attempts'] == 2
    assert db.aborts == 1 and db.rollback_calls == 0
    assert len(batches) == 2 and batches[0] == batches[1]
    transactions = list(dict.fromkeys(tx for tx, _ in calls))
    assert len(transactions) == 2
    for tx in transactions:
        statements = transaction_statements([call for call in calls if call[0] is tx])
        assert statements[0].startswith('select reservation_id, workspace_id')
        credit_before_key(statements, key_last=tx is transactions[-1], require_both=False)
    assert not any('tr_credit_balance' in sql or 'tr_key_limit' in sql
                   for tx, sql in calls if tx is transactions[0])
    assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 70
    assert db.typed['tr_key_limit'][('key', 0)]['usage'] == 70
    assert len(db.generation_records) == len(db.operational_analytics_outbox) == 1


@pytest.mark.parametrize('reason', ['claim_zero', 'typed_zero', 'done_zero'])
@pytest.mark.parametrize('later_error', [False, True])
def test_fallback_prefix_precedence_and_no_counter_access(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    reason: str, later_error: bool,
) -> None:
    db, options = fixture()
    original = _FakeTransaction.batch_update
    transactions: list[Any] = []
    calls = record_statements(monkeypatch)

    def batch(tx: Any, statements: Any, **kw: Any) -> Any:
        transactions.append(tx)
        status, counts = original(tx, statements, **kw)
        if len(transactions) == 1:
            counts[['claim_zero', 'typed_zero', 'done_zero'].index(reason)] = 0
            if later_error:
                return Status(code=code_pb2.ALREADY_EXISTS), counts[:5]
        return status, counts

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    with caplog.at_level(logging.INFO):
        result = invoke(db, options)
    assert result['outcome'] == 'settled' and result['attempts'] == 2
    assert db.rollback_calls == 1
    first = transaction_statements([call for call in calls if call[0] is transactions[0]])
    assert not any('tr_credit_balance' in sql or 'tr_key_limit' in sql for sql in first)
    for tx in set(t for t, _ in calls):
        statements = transaction_statements([call for call in calls if call[0] is tx])
        if any('tr_credit_balance' in sql for sql in statements):
            credit_before_key(statements, key_last=True)
    assert f'fallback_reason={reason}' in caplog.text
    assert 'fallback_outcome=settled' in caplog.text and 'eligible_attempts=1' in caplog.text
    assert 'rollback_ms=' in caplog.text and 'remaining_ms=' in caplog.text


@pytest.mark.parametrize('winner', ['delete', 'refund', 'charge', 'reaper', 'changed_holds'])
def test_fallback_rereads_after_intervening_winner(monkeypatch: pytest.MonkeyPatch, winner: str) -> None:
    from datetime import timedelta

    monkeypatch.setattr(outbox, '_iso_now', lambda: NOW.isoformat())
    db, options = fixture()
    original = _FakeTransaction.rollback
    aid, rid = options['authorization_id'], options['reservation_id']
    db.settle_outbox[(aid, 'settle')]['reservation_id'] = None  # done_zero
    expected = []
    commits = db.commits

    def rollback(tx: Any) -> None:
        original(tx)
        if winner == 'delete':
            del db.reservations[rid]
        elif winner == 'changed_holds':
            db.reservations[rid].update(
                actual_micro=19, credit_reserved_micro=160, key_reserved_micro=180,
            )
            db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] = 160
            db.typed['tr_key_limit'][('key', 0)]['reserved'] = 180
        elif winner == 'reaper':
            reaped = current._finalize_reaped_reservation_atomic(
                db, param_types, reservation_id=rid, reap_now=NOW + timedelta(minutes=6),
                guard_outbox=False, snapshot_booking_enabled=False,
                operational_analytics_outbox=None,
            )
            assert reaped.outcome == 'settled'
        else:
            winning = dict(options, success=winner == 'charge', actual_micro=70 if winner == 'charge' else 0)
            if winner == 'refund':
                refunded = copy.deepcopy(options['authorization'])
                refunded.record_finalization(
                    success=False, actual_microdollars=0, selected_usage_type='Credits', generation=None,
                )
                winning.update(authorization=refunded, auth_body_settled=json_body(refunded), generation=None)
            invoke(db, winning, frozen.typed_finalize_atomic)
        oracle = clone(db)
        invoke(oracle, options, frozen.typed_finalize_atomic)
        expected.append(state(oracle))

    monkeypatch.setattr(_FakeTransaction, 'rollback', rollback)
    result = invoke(db, options)
    assert result['outcome'] == (
        'not_found' if winner == 'delete' else
        'settled' if winner == 'changed_holds' else 'already_settled'
    )
    assert result['attempts'] == 2
    assert len(expected) == 1 and state(db) == expected[0]
    assert db.commits - commits == (1 if winner in {'delete', 'changed_holds'} else 2)
    assert db.rollback_calls == 1
    assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == (
        70 if winner in {'charge', 'changed_holds'} else 0
    )
    if winner == 'changed_holds':
        assert db.reservations[rid]['actual_micro'] == 70
        assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 0
        assert db.typed['tr_key_limit'][('key', 0)]['reserved'] == 0


# Reuse #1355's real SDK Session/Transaction + configured RPC deadline fixture.
from tests.test_authorize_speculative_batch import configured_sdk as _configured_sdk  # noqa: E402

configured_sdk = _configured_sdk


@pytest.mark.parametrize('elapsed', [6, 21])
@pytest.mark.parametrize('cleanup', ['ok', 'failed', 'expired'])
def test_real_sdk_finalize_rollback_floor(configured_sdk: Any, elapsed: int, cleanup: str) -> None:
    from google.api_core.exceptions import DeadlineExceeded
    from google.cloud.spanner_v1.types import (
        ExecuteBatchDmlResponse,
        PartialResultSet,
        ResultSet,
        ResultSetStats,
    )

    from trusted_router import storage_gcp_io as io

    sdk = configured_sdk
    fields = [
        ('reservation_id', param_types.STRING, 'r'), ('workspace_id', param_types.STRING, 'w'),
        ('key_hash', param_types.STRING, 'k'), ('ws_shard', param_types.INT64, '0'),
        ('credit_shard', param_types.INT64, '0'), ('key_shard', param_types.INT64, '0'),
        ('credit_reserved_micro', param_types.INT64, '0'), ('key_reserved_micro', param_types.INT64, '0'),
        ('hold_usage_type', param_types.STRING, 'Credits'), ('settled_usage_type', param_types.STRING, 'Credits'),
        ('actual_micro', param_types.INT64, '70'), ('authorization_id', param_types.STRING, 'a'),
        ('settled', param_types.BOOL, True), ('expires_at', param_types.TIMESTAMP, NOW.isoformat()),
    ]

    def read(**kw: Any) -> Any:
        assert 'FROM tr_reservation WHERE reservation_id=@rid' in kw['request'].sql
        response = PartialResultSet(metadata={
            'transaction': {'id': f'tx-{len(sdk.transactions)}'.encode()},
            'row_type': {'fields': [{'name': name, 'type_': typ} for name, typ, _ in fields]},
        })
        for _, _, value in fields:
            if isinstance(value, bool):
                response._pb.values.add(bool_value=value)
            else:
                response._pb.values.add(string_value=value)
        return iter([response])

    def batch(**kw: Any) -> Any:
        assert len(kw['request'].statements) == 2
        sdk.clock[0] += elapsed
        return ExecuteBatchDmlResponse(status=Status(), result_sets=[
            ResultSet(stats=ResultSetStats(row_count_exact=count)) for count in [0, 1]
        ])

    def rollback(**kw: Any) -> None:
        if cleanup == 'failed':
            raise ServiceUnavailable('rollback failed')
        if cleanup == 'expired':
            sdk.clock[0] += kw['timeout'] + 0.01
            raise DeadlineExceeded('rollback expired')

    sdk.rpcs.execute_streaming_sql.side_effect = read
    sdk.rpcs.execute_batch_dml.side_effect = batch
    sdk.rpcs.rollback.side_effect = rollback
    sdk.rpcs.execute_sql.return_value = ResultSet(stats=ResultSetStats(row_count_exact=0))
    options = dict(reservation_id='r', authorization_id='a', success=True, actual_micro=70,
                   settled_usage_type='Credits', now=NOW, outbox_available=True,
                   authorization=_authorization('a', 'r'), auth_body_settled='{}')
    if elapsed > 20 or cleanup == 'expired':
        with pytest.raises(DeadlineExceeded, match='shared RPC deadline exceeded'):
            current.typed_finalize_atomic(sdk.db, param_types, **options)
        assert len(sdk.transactions) == 1
        sdk.rpcs.commit.assert_not_called()
    else:
        result = current.typed_finalize_atomic(sdk.db, param_types, **options)
        assert result['outcome'] == 'already_settled' and result['attempts'] == 2
        assert sdk.rpcs.execute_streaming_sql.call_count == 2
        assert len(sdk.transactions) == 2
        sdk.rpcs.commit.assert_called_once()
    sdk.rpcs.rollback.assert_called_once()
    call = sdk.rpcs.rollback.call_args.kwargs
    assert call['transaction_id'] == b'tx-1'
    assert call['timeout'] == (io._ROLLBACK_FLOOR_SECONDS if elapsed > 20 else 20 - elapsed)
    assert sdk.transactions[0].committed is None
    assert io._SPANNER_RPC_DEADLINE.get() is None


@pytest.mark.parametrize('snapshot_booking', [False, True])
def test_actual_reaper_winner_matches_frozen_finalize(snapshot_booking: bool) -> None:
    from datetime import timedelta

    from tests.test_stage_d_heartbeat import NOW as REAP_NOW
    from tests.test_stage_d_heartbeat import _heartbeat, _seed, _seed_reaper_counters

    initial, auth = _seed()
    _seed_reaper_counters(initial)
    assert _heartbeat(initial).accepted
    result = current._finalize_reaped_reservation_atomic(
        initial, param_types, reservation_id='reservation', reap_now=REAP_NOW + timedelta(seconds=301),
        guard_outbox=True, snapshot_booking_enabled=snapshot_booking, operational_analytics_outbox=None,
    )
    assert result.outcome == 'settled' and result.snapshot_booked == snapshot_booking
    options = dict(reservation_id='reservation', authorization_id=auth.id, authorization=auth,
                   auth_body_settled=json_body(auth), success=True, actual_micro=70,
                   settled_usage_type='Credits', now=REAP_NOW, outbox_available=True)
    observed = []
    for impl in (frozen.typed_finalize_atomic, current.typed_finalize_atomic):
        db = clone(initial)
        result = invoke(db, options, impl)
        assert result['outcome'] == 'already_settled'
        result.pop('attempts')
        observed.append((result, state(db)))
    assert observed[0] == observed[1]


@pytest.mark.parametrize('table', ['generation_records'])
def test_real_duplicate_insert_rolls_back_successful_prefix(table: str) -> None:
    db, options = fixture()
    committed = clone(db)
    invoke(committed, options, frozen.typed_finalize_atomic)
    setattr(db, table, copy.deepcopy(getattr(committed, table)))
    before, commits = state(db), db.commits
    with pytest.raises(AlreadyExists):
        invoke(db, options)
    assert state(db) == before and db.commits == commits and db.rollback_calls == 1


def test_claim_zero_then_deletion_changes_already_settled_to_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback is a new serializable observation, not the first S11 snapshot."""
    db, options = fixture()
    invoke(db, options, frozen.typed_finalize_atomic)
    assert invoke(db, options, frozen.typed_finalize_atomic)['outcome'] == 'already_settled'
    before = copy.deepcopy(db.typed)
    expected = clone(db)
    del expected.reservations[options['reservation_id']]
    rollback = _FakeTransaction.rollback

    def delete_after_discard(tx: Any) -> None:
        rollback(tx)
        del db.reservations[options['reservation_id']]

    monkeypatch.setattr(_FakeTransaction, 'rollback', delete_after_discard)
    result = invoke(db, options)
    assert result['outcome'] == 'not_found' and result['attempts'] == 2
    assert db.rollback_calls == 1 and db.typed == before
    assert state(db) == state(expected)


@pytest.mark.parametrize('stored_id', [None, '', 'foreign'])
@pytest.mark.parametrize('foreign_guarded', [False, True])
def test_speculative_finalize_stored_id_retention(
    monkeypatch: pytest.MonkeyPatch, stored_id: str | None, foreign_guarded: bool,
) -> None:
    monkeypatch.setattr(outbox, '_iso_now', lambda: NOW.isoformat())
    db, options = fixture()
    aid, rid = options['authorization_id'], options['reservation_id']
    row = db.settle_outbox[(aid, 'settle')]
    row['reservation_id'] = stored_id
    row['attempts'] = 3
    db.reservations['foreign'] = dict(
        db.reservations[rid], reservation_id='foreign', authorization_id='other', settled=True,
    )
    if foreign_guarded:
        db.settle_outbox[('other', 'settle')] = dict(
            row, authorization_id='other', reservation_id='foreign',
        )
    oracle = clone(db)
    expected = invoke(oracle, options, frozen.typed_finalize_atomic)
    batches = []
    original = _FakeTransaction.batch_update

    def batch(tx: Any, statements: Any, **kw: Any) -> Any:
        result = original(tx, statements, **kw)
        batches.append(result)
        return result

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    result = invoke(db, options)
    assert result.pop('attempts') == 2
    expected.pop('attempts')
    assert result == expected
    assert batches[0][1][:3] == [1, 1, 0]  # Guarded done UPDATE really missed.
    assert db.rollback_calls == 1
    assert state(db) == state(oracle)
    assert db.settle_outbox[(aid, 'settle')]['settle_body'] is None
    assert db.settle_outbox[(aid, 'settle')]['attempts'] == 4
    assert db.reservations[rid]['terminal_at'] is None
    assert (db.reservations['foreign']['terminal_at'] is not None) == (
        stored_id == 'foreign' and not foreign_guarded
    )


def _sdk_finalize_options(sdk: Any) -> dict[str, Any]:
    """Serve S11 over the real SDK streaming-read decoder, no helper patches."""
    from google.cloud.spanner_v1.types import PartialResultSet

    db, options = fixture()
    options.pop('settle_outbox_done')  # Keep the fallback's batch to the two evidence INSERTs.
    reservation = db.reservations[options['reservation_id']]
    # No unused credit, hence no unrelated payment-recovery scan in this RPC fixture.
    reservation['credit_reserved_micro'] = options['actual_micro']
    fields = [
        ('reservation_id', param_types.STRING), ('workspace_id', param_types.STRING),
        ('key_hash', param_types.STRING), ('ws_shard', param_types.INT64),
        ('credit_shard', param_types.INT64), ('key_shard', param_types.INT64),
        ('credit_reserved_micro', param_types.INT64), ('key_reserved_micro', param_types.INT64),
        ('hold_usage_type', param_types.STRING), ('settled_usage_type', param_types.STRING),
        ('actual_micro', param_types.INT64), ('authorization_id', param_types.STRING),
        ('settled', param_types.BOOL), ('expires_at', param_types.TIMESTAMP),
    ]

    def read(**kw: Any) -> Any:
        request = kw['request']
        assert 'FROM tr_reservation WHERE reservation_id=@rid' in request.sql
        assert request.params['rid'] == options['reservation_id']
        response = PartialResultSet(metadata={
            'transaction': {'id': f'tx-{len(sdk.transactions)}'.encode()},
            'row_type': {'fields': [{'name': name, 'type_': typ} for name, typ in fields]},
        })
        for name, typ in fields:
            value = reservation.get(name)
            if value is None:
                response._pb.values.add(null_value=0)
            elif typ == param_types.BOOL:
                response._pb.values.add(bool_value=value)
            else:
                response._pb.values.add(string_value=(
                    value.isoformat() if typ == param_types.TIMESTAMP else str(value)
                ))
        return iter([response])

    sdk.rpcs.execute_streaming_sql.side_effect = read
    return options


def _batch_response(counts: list[int], code: int = code_pb2.OK) -> Any:
    from google.cloud.spanner_v1.types import ExecuteBatchDmlResponse, ResultSet, ResultSetStats

    return ExecuteBatchDmlResponse(status=Status(code=code), result_sets=[
        ResultSet(stats=ResultSetStats(row_count_exact=count)) for count in counts
    ])


def _assert_timing(
    caplog: pytest.LogCaptureFixture, *, attempts: int, reason: str, outcome: str,
) -> None:
    import re

    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith('typed finalize speculation timing ')]
    assert len(lines) == 1
    match = re.fullmatch(
        r'typed finalize speculation timing eligible_attempts=(\d+) attempts=(\d+) '
        r'fallback_reason=(\w+) rollback_ms=(\d+\.\d) remaining_ms=(\d+\.\d) '
        r'fallback_outcome=(\w+)', lines[0],
    )
    assert match is not None
    eligible, count, fallback, rollback, remaining, result = match.groups()
    assert (eligible, int(count), fallback, result) == ('1', attempts, reason, outcome)
    assert float(rollback) == (0 if reason == 'none' else 125)
    assert float(remaining) == (20000 if reason == 'none' else 19875)
    assert 'PRIVATE_PAYLOAD_SENTINEL' not in caplog.text


@pytest.mark.parametrize('scenario', ['clean', 'claim_zero', 'fallback_aborted', 'fallback_zero_aborted'])
def test_real_sdk_finalize_retry_and_telemetry(
    configured_sdk: Any, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, scenario: str,
) -> None:
    from unittest.mock import Mock

    from google.cloud.spanner_v1 import _helpers

    sdk = configured_sdk
    options = _sdk_finalize_options(sdk)
    options['auth_body_settled'] = 'PRIVATE_PAYLOAD_SENTINEL'
    options['generation'].model_id = 'PRIVATE_PAYLOAD_SENTINEL'
    sleep = Mock()
    monkeypatch.setattr(_helpers.time, 'sleep', sleep)
    responses = [_batch_response([1, 1, 1, 1])]
    if scenario != 'clean':
        responses = [_batch_response([0, 1, 1, 1])]
        if 'aborted' in scenario:
            responses.append(_batch_response(
                [0] if scenario == 'fallback_zero_aborted' else [], code_pb2.ABORTED,
            ))
        responses.append(_batch_response([1, 1]))
    sdk.rpcs.execute_batch_dml.side_effect = responses

    def rollback(**kw: Any) -> None:
        sdk.clock[0] += 0.125

    sdk.rpcs.rollback.side_effect = rollback
    with caplog.at_level(logging.INFO, logger=current.log.name):
        result = invoke(sdk.db, options)
    attempts = 1 if scenario == 'clean' else 3 if 'aborted' in scenario else 2
    assert result['outcome'] == 'settled' and result['attempts'] == attempts
    assert len(sdk.transactions) == sdk.rpcs.execute_streaming_sql.call_count == attempts
    sdk.rpcs.commit.assert_called_once()
    assert sdk.rpcs.commit.call_args.kwargs['request'].transaction_id == f'tx-{attempts}'.encode()
    assert all(tx.committed is None for tx in sdk.transactions[:-1])
    assert sdk.rpcs.rollback.call_count == int(scenario != 'clean')
    batches = [c.kwargs['request'] for c in sdk.rpcs.execute_batch_dml.call_args_list]
    assert [len(b.statements) for b in batches] == [4] + [2] * (attempts - 1)
    if attempts > 1:
        assert sdk.rpcs.rollback.call_args.kwargs['transaction_id'] == b'tx-1'
        assert batches[0].statements[-2:] == batches[1].statements
    if attempts == 3:
        assert batches[1].statements == batches[2].statements
        assert sdk.transactions[2]._multiplexed_session_previous_transaction_id == (
            b'tx-2' if sdk.multiplexed else None
        )
        sleep.assert_called_once()
    else:
        sleep.assert_not_called()
    # Real release DML only executes in the final successful callback.
    updates = [c.kwargs['request'].sql for c in sdk.rpcs.execute_sql.call_args_list]
    assert sum(sql.startswith('UPDATE tr_credit_balance') for sql in updates) == 1
    assert sum(sql.startswith('UPDATE tr_key_limit') for sql in updates) == 1
    _assert_timing(caplog, attempts=attempts, reason='none' if attempts == 1 else 'claim_zero',
                   outcome='not_attempted' if attempts == 1 else 'settled')


@pytest.mark.parametrize('zero_prefix', [False, True])
def test_fallback_aborted_commits_state_once(
    monkeypatch: pytest.MonkeyPatch, zero_prefix: bool,
) -> None:
    monkeypatch.setattr(outbox, '_iso_now', lambda: NOW.isoformat())
    db, options = fixture()
    db.settle_outbox[(options['authorization_id'], 'settle')]['reservation_id'] = None
    oracle = clone(db)
    invoke(oracle, options, frozen.typed_finalize_atomic)
    commits = db.commits
    original = _FakeTransaction.batch_update
    batches = []

    def batch(tx: Any, statements: Any, **kw: Any) -> Any:
        batches.append(copy.deepcopy(statements))
        if len(batches) == 2:
            # Stage an actual successful prefix, then lose the whole transaction.
            original(tx, statements[:1], **kw)
            return Status(code=code_pb2.ABORTED), [0] if zero_prefix else [1]
        return original(tx, statements, **kw)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    result = invoke(db, options)
    assert result['outcome'] == 'settled' and result['attempts'] == 3
    assert db.commits == commits + 1 and db.rollback_calls == 1 and db.aborts == 1
    assert len(batches) == 3 and batches[1] == batches[2]
    assert state(db) == state(oracle)
    assert len(db.generation_records) == len(db.operational_analytics_outbox) == 1


@pytest.mark.parametrize('failed_rpc', ['batch', 'batch_and_rollback', 'rollback_then_fallback_batch'])
def test_real_sdk_finalize_transport_failure_never_commits(
    configured_sdk: Any, failed_rpc: str,
) -> None:
    sdk = configured_sdk
    options = _sdk_finalize_options(sdk)
    error = ServiceUnavailable('batch transport lost')
    sdk.rpcs.execute_batch_dml.side_effect = (
        [_batch_response([0, 1, 1, 1]), error]
        if failed_rpc == 'rollback_then_fallback_batch' else error
    )
    if failed_rpc != 'batch':
        sdk.rpcs.rollback.side_effect = ServiceUnavailable('rollback transport lost')
    with pytest.raises(ServiceUnavailable, match='batch transport lost'):
        invoke(sdk.db, options)
    sdk.rpcs.commit.assert_not_called()
    attempts = 2 if failed_rpc == 'rollback_then_fallback_batch' else 1
    assert len(sdk.transactions) == attempts
    assert sdk.rpcs.execute_batch_dml.call_count == attempts
    assert sdk.rpcs.rollback.call_count == attempts
    assert all(tx.committed is None for tx in sdk.transactions)
    updates = [c.kwargs['request'].sql for c in sdk.rpcs.execute_sql.call_args_list]
    assert not any('tr_credit_balance' in sql or 'tr_key_limit' in sql for sql in updates)
