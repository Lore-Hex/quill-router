"""C1's frozen-main differential and guarded-tail failure contracts."""
from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import datetime, timedelta
from typing import Any

import pytest
from google.api_core.exceptions import FailedPrecondition
from google.cloud.spanner_v1 import param_types
from google.rpc.status_pb2 import Status

from tests.fakes import settle_c1_main as main
from tests.fakes.spanner import _FakeTransaction, _Row
from tests.test_settle_speculative_batch import clone, fixture, invoke, state
from tests.test_spanner_batch_dml import NOW
from trusted_router import storage_gcp_authorize as current
from trusted_router import storage_gcp_settle_outbox as outbox
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_models import CreditAccount, TrustEvent

SCENARIOS = [
    'ordinary', 'equal', 'overrun', 'refund', 'replay_settled', 'replay_refunded',
    'reaped', 'snapshot_booked', 'byok', 'byok_excluded', 'strict', 'auto_refill',
    'later_shard', 'concurrent', 'durable_intent', 'debt', 'foreign_debt',
    'nonpayment_debt', 'credit_underflow', 'key_underflow', 'rollover',
    'deleted_key', 'reshard', 'heartbeat',
]


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import storage_gcp_counter_dml, storage_models

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return NOW

    monkeypatch.setattr(storage_gcp_counter_dml, 'datetime', Clock)
    monkeypatch.setattr(main, 'datetime', Clock)
    monkeypatch.setattr(storage_models, 'utcnow', lambda: NOW)
    monkeypatch.setattr(current, 'utcnow', lambda: NOW)
    monkeypatch.setattr(main, 'utcnow', lambda: NOW)
    monkeypatch.setattr(outbox, '_iso_now', lambda: NOW.isoformat())


def debt_row(workspace: str = 'workspace', kind: str = 'payment') -> dict[str, Any]:
    row = dict.fromkeys(field.name for field in fields(TrustEvent))
    row.update(workspace_id=workspace, event_id='debt', kind=kind, provider='stripe',
               occurred_at=NOW, recorded_at=NOW, unrecovered_micro=50,
               recovered_micro=0, recovery_target=50, debit_status='unrecovered')
    return row


def prepare(scenario: str, capped: bool, intent: bool) -> tuple[Any, dict[str, Any]]:
    db, options = fixture()
    aid, rid = options['authorization_id'], options['reservation_id']
    res = db.reservations[rid]
    credit = db.typed['tr_credit_balance'][('workspace', 0)]
    key = db.typed['tr_key_limit'][('key', 0)]
    key.update(day_start=NOW, week_start=NOW, month_start=NOW)
    if not capped:
        key.update(limit_micro=None, reserved=0)
        res['key_reserved_micro'] = 0
    if not intent:
        db.settle_outbox.clear()
        options['settle_outbox_done'] = None
    if scenario in {'equal', 'overrun'}:
        options['actual_micro'] = 100 if scenario == 'equal' else 130
    elif scenario == 'refund':
        options.update(success=False, actual_micro=0, generation=None)
    elif scenario in {'byok', 'byok_excluded'}:
        options['settled_usage_type'] = 'BYOK'
        res.update(credit_reserved_micro=0, hold_usage_type='BYOK')
        credit['reserved'] = 0
        if scenario == 'byok_excluded':
            key.update(include_byok=False, reserved=0)
            res['key_reserved_micro'] = 0
    elif scenario == 'strict':
        # Strict authorize takes a hold even on an uncapped lifetime key.
        key.update(reserved=100, day_limit_micro=200, week_limit_micro=300,
                   month_limit_micro=400)
        res['key_reserved_micro'] = 100
        options['authorization'].budget_strict = True
    elif scenario == 'auto_refill':
        db.rows[('credit', 'workspace')] = _Row(json_body(CreditAccount(
            workspace_id='workspace', auto_refill_enabled=True,
            auto_refill_threshold_microdollars=2000, auto_refill_amount_microdollars=5000,
            stripe_customer_id='cus_c1_test', stripe_payment_method_id='pm_c1_test',
        )), 1)
        if intent:
            db.settle_outbox[(aid, 'settle')].update(
                auto_refill_workspace_id='workspace', auto_refill_status='pending',
                auto_refill_attempts=0, auto_refill_next_attempt_at=NOW,
            )
    elif scenario == 'later_shard':
        db.typed['tr_credit_balance'][('workspace', 3)] = dict(credit, shard=3)
        credit.update(reserved=0, total_credits=0)
        res['credit_shard'] = 3
    elif scenario in {'debt', 'foreign_debt', 'nonpayment_debt'}:
        row = debt_row('other' if scenario == 'foreign_debt' else 'workspace',
                       'refund' if scenario == 'nonpayment_debt' else 'payment')
        db.typed['tr_trust_event'] = {(row['workspace_id'], 'debt'): row}
    elif scenario == 'credit_underflow':
        credit['reserved'] = 99
    elif scenario == 'key_underflow' and capped:
        key['reserved'] = 99
    elif scenario == 'rollover':
        key['day_start'] = NOW - timedelta(days=1)
    elif scenario == 'deleted_key':
        db.typed['tr_key_limit'].clear()
    elif scenario == 'reshard':
        # The recorded shard was removed after an uncapped authorization.
        res.update(key_shard=7, key_reserved_micro=0)
        key['reserved'] = 0
    elif scenario == 'heartbeat':
        db.gateway_authorizations[aid].update(
            heartbeat_seq=4, heartbeat_hash='fresh', heartbeat_at=NOW,
            started_at=NOW, delivered_usage='{"output_tokens":4}',
        )
    auth = options['authorization']
    auth.record_finalization(success=options['success'], actual_microdollars=options['actual_micro'],
                             selected_usage_type=options['settled_usage_type'],
                             generation=options['generation'])
    options['auth_body_settled'] = json_body(auth)
    if scenario in {'replay_settled', 'replay_refunded'}:
        winning = copy.deepcopy(options)
        if scenario == 'replay_refunded':
            winning.update(success=False, actual_micro=0, generation=None)
            winning['authorization'].record_finalization(
                success=False, actual_microdollars=0, selected_usage_type='Credits', generation=None,
            )
            winning['auth_body_settled'] = json_body(winning['authorization'])
        invoke(db, winning, main.typed_finalize_atomic)
    elif scenario in {'reaped', 'snapshot_booked'}:
        from tests.test_stage_d_heartbeat import NOW as STAGE_NOW
        from tests.test_stage_d_heartbeat import _heartbeat, _seed, _seed_reaper_counters
        from trusted_router.storage_gcp_authorize import reap_expired_reservations_result
        from trusted_router.storage_gcp_request_records import read_gateway_authorization

        db, _ = _seed()
        _seed_reaper_counters(db)
        if scenario == 'snapshot_booked':
            assert _heartbeat(db).accepted
        result = reap_expired_reservations_result(
            db, param_types, now=STAGE_NOW + timedelta(seconds=301),
            snapshot_booking_enabled=scenario == 'snapshot_booked',
        )
        assert result.count == 1
        terminal = read_gateway_authorization(db.snapshot(), param_types, 'gwa-stage-d-fixture')
        options.update(reservation_id='reservation', authorization_id='gwa-stage-d-fixture',
                       authorization=terminal, auth_body_settled=json_body(terminal),
                       settle_outbox_done=None)
    return db, options


@pytest.mark.parametrize('scenario', SCENARIOS)
@pytest.mark.parametrize('capped', [False, True])
@pytest.mark.parametrize('intent', [False, True])
def test_main_money_differential(scenario: str, capped: bool, intent: bool) -> None:
    initial, options = prepare(scenario, capped, intent)
    observations = []
    for impl in (main.typed_finalize_atomic, current.typed_finalize_atomic):
        db = clone(initial)
        try:
            if scenario == 'concurrent':
                db._ready_barrier = threading.Barrier(2, action=lambda db=db: setattr(db, '_ready_barrier', None))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(lambda _, db=db, impl=impl: invoke(db, copy.deepcopy(options), impl), range(2)))
                db._ready_barrier = None
                result = sorted(item['outcome'] for item in outcomes)
                assert result == ['already_settled', 'settled'] and db.aborts > 0
            else:
                result = invoke(db, copy.deepcopy(options), impl)
                result.pop('attempts', None)
        except Exception as exc:
            result = (type(exc).__name__, str(exc))
        if scenario != 'concurrent':
            if scenario == 'deleted_key' and not capped:
                assert isinstance(result, tuple) and result[0] == 'RuntimeError'
            else:
                expected = (
                    'already_settled' if scenario in {'replay_settled', 'replay_refunded', 'reaped', 'snapshot_booked'}
                    else 'error' if scenario == 'credit_underflow' or (scenario == 'key_underflow' and capped)
                    else 'settled'
                )
                assert isinstance(result, dict) and result['outcome'] == expected, result
        if scenario.startswith('byok'):
            key = db.typed['tr_key_limit'][('key', 0)]
            assert key['usage'] == 0 and key['byok_usage'] == 70
            assert key['day_usage'] == (0 if scenario == 'byok_excluded' else 70)
            assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 0
        # Include every durable fake table; versions/RPC telemetry are not money state.
        observations.append((result, state(db), copy.deepcopy(db.analytics_outbox),
                             copy.deepcopy(db.stage_d_policy_watermarks), copy.deepcopy(db.reservation_idemp)))
        if scenario == 'overrun':
            # Main books the overrun; it does NOT clamp actual to the original hold.
            assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 130
        if scenario == 'debt':
            assert db.typed['tr_trust_event'][('workspace', 'debt')]['unrecovered_micro'] == 20
        if scenario in {'credit_underflow', 'key_underflow'} and (capped or scenario == 'credit_underflow'):
            assert result == {'outcome': 'error'}
            assert state(db) == state(initial)
    assert observations[0] == observations[1]


@pytest.mark.parametrize('position', [7, 8])
@pytest.mark.parametrize('bad_count', [0, 2, -1, 'truncated'])
def test_tail_counts_require_rollback(monkeypatch: pytest.MonkeyPatch, position: int, bad_count: Any) -> None:
    db, options = prepare('ordinary', True, True)
    before = state(db)
    original = _FakeTransaction.batch_update
    fired = False

    def batch(tx: Any, statements: Any, **kwargs: Any) -> Any:
        nonlocal fired
        status, counts = original(tx, statements, **kwargs)
        if not fired:
            fired = True
            counts = counts[:position] if bad_count == 'truncated' else [
                bad_count if i == position else count for i, count in enumerate(counts)
            ]
        return status, counts

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    if bad_count == 0:
        result = invoke(db, options)
        assert result['outcome'] == 'settled' and result['attempts'] == 2
        assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 70
        assert db.typed['tr_key_limit'][('key', 0)]['usage'] == 70
    else:
        with pytest.raises(FailedPrecondition):
            invoke(db, options)
        assert state(db) == before
    assert db.rollback_calls == 1


def test_refund_never_folds_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    db, options = prepare('refund', True, True)
    original = _FakeTransaction.batch_update
    batches = []

    def batch(tx: Any, statements: Any, **kwargs: Any) -> Any:
        batches.append(statements)
        return original(tx, statements, **kwargs)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    assert invoke(db, options)['outcome'] == 'settled'
    assert all('tr_credit_balance' not in sql and 'tr_key_limit' not in sql
               for statements in batches for sql, _, _ in statements)
    assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 0


def test_outbox_resolution_is_in_charge_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db, options = prepare('ordinary', True, True)
    original = db._try_commit
    seen = []

    def commit(tx: Any) -> Any:
        result = original(tx)
        if result:
            seen.append((db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'],
                         db.settle_outbox[(options['authorization_id'], 'settle')]['status'],
                         len(db.generation_records), len(db.operational_analytics_outbox)))
        return result

    monkeypatch.setattr(db, '_try_commit', commit)
    assert invoke(db, options)['outcome'] == 'settled'
    assert seen == [(70, 'done', 1, 1)]


def test_earlier_bad_count_cannot_be_hidden_by_tail_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    db, options = prepare('ordinary', True, True)
    before = state(db)
    monkeypatch.setattr(_FakeTransaction, 'batch_update',
                        lambda *_a, **_k: (Status(), [1, 1, 1, 2, 1, 1, 1, 0, 1]))
    with pytest.raises(FailedPrecondition):
        invoke(db, options)
    assert state(db) == before and db.rollback_calls == 1


@pytest.mark.parametrize('scenario', [s for s in SCENARIOS if s != 'concurrent'])
@pytest.mark.parametrize('capped', [False, True])
@pytest.mark.parametrize('intent', [False, True])
def test_http_and_all_state_differential(
    monkeypatch: pytest.MonkeyPatch, scenario: str, capped: bool, intent: bool,
) -> None:

    import uuid
    from itertools import count

    from tests.fakes.spanner import make_fake_store
    from tests.test_settle_outbox_drain import (
        ENDPOINT_ID,
        MODEL_ID,
        PROVIDER,
        _client,
        _settle_json,
    )
    from trusted_router import gateway_timing
    from trusted_router.config import Settings
    from trusted_router.routes.internal import gateway
    from trusted_router.storage import configure_store
    from trusted_router.storage_gcp_analytics_outbox import SpannerAnalyticsOutbox
    from trusted_router.storage_gcp_request_records import read_gateway_authorization

    initial, options = prepare(scenario, capped, intent)
    aid = options['authorization_id']
    auth = read_gateway_authorization(initial.snapshot(), param_types, aid)
    assert auth is not None
    auth.model_id = auth.requested_model_id = MODEL_ID
    auth.provider = PROVIDER
    auth.candidate_model_ids = [MODEL_ID]
    endpoint_id = ENDPOINT_ID.replace('/prepaid', '/byok') if scenario.startswith('byok') else ENDPOINT_ID
    auth.endpoint_id = endpoint_id
    auth.candidate_endpoint_ids = [endpoint_id]
    if scenario.startswith('byok'):
        auth.usage_type = 'BYOK'
        initial.gateway_authorizations[aid]['usage_type'] = 'BYOK'
    # Isolate finalization from price changes: both implementations receive the
    # exact same resolved cost. Full pricing/Stage D suites run separately.
    initial.gateway_authorizations[aid].update(model_id=MODEL_ID, provider=PROVIDER,
                                               payload=json_body(auth))
    monkeypatch.setattr(gateway, '_endpoint_cost_microdollars', lambda *_a, **_k: options['actual_micro'])
    monkeypatch.setattr(gateway, 'record_successful_api_call_safely', lambda *_a, **_k: None)
    monkeypatch.setattr(gateway, 'should_drain_inline', lambda *_a: False)
    # The durable T-I attachment is real; external post-response charging is outside this differential.
    monkeypatch.setattr(gateway, '_schedule_auto_refill', lambda *_a, **_k: None)
    monkeypatch.setattr('trusted_router.services.budget_alerts.maybe_send_budget_alerts', lambda **_k: None)
    monkeypatch.setattr(gateway_timing, 'perf_counter', lambda: 0.0)
    observations = []
    for impl in (main.typed_finalize_atomic, current.typed_finalize_atomic):
        store, db = make_fake_store(operational_analytics_outbox_enabled=True,
                                   generation_records_enabled=True, request_record_write_mode='typed')
        db.now = NOW
        for name in ('typed', 'rows', 'reservations', 'gateway_authorizations', 'settle_outbox',
                     'generation_records', 'operational_analytics_outbox', 'analytics_outbox',
                     'stage_d_policy_watermarks', 'reservation_idemp'):
            setattr(db, name, copy.deepcopy(getattr(initial, name)))
        store.generation_store._analytics_outbox = SpannerAnalyticsOutbox(db, param_types)
        configure_store(store)

        def finalize(*args: Any, impl: Any = impl, **kwargs: Any) -> Any:
            kwargs['now'] = NOW
            return impl(*args, **kwargs)

        with monkeypatch.context() as patch:
            ids = count(1)
            patch.setattr(uuid, 'uuid4', lambda ids=ids: uuid.UUID(int=next(ids)))
            patch.setattr(current, 'typed_finalize_atomic', finalize)
            if scenario == 'durable_intent':
                # A transient finalize failure after the independently committed
                # intent must retain frozen money/auto-refill repair authority.
                from google.api_core.exceptions import DeadlineExceeded
                patch.setattr(current, 'typed_finalize_atomic',
                              lambda *_a, **_k: (_ for _ in ()).throw(DeadlineExceeded('injected')))
            response = _client(Settings(environment='test', service_surface='internal' if intent else 'combined',
                                        settle_outbox_enabled=intent),
                               raise_server_exceptions=False).post(
                '/v1/internal/gateway/refund' if scenario == 'refund' else '/v1/internal/gateway/settle',
                json={**_settle_json(aid), 'selected_endpoint': endpoint_id},
                headers={'x-request-id': 'c1-differential'},
            )
        body = response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text
        if scenario == 'durable_intent' and intent:
            assert response.status_code == 200 and body['data']['disposition'] == 'intent_durable'
            assert db.settle_outbox[(aid, 'settle')]['status'] == 'pending'
            assert not db.reservations[options['reservation_id']]['settled']
        if scenario in {'ordinary', 'equal', 'overrun', 'auto_refill', 'later_shard', 'rollover', 'strict'}:
            assert response.status_code == 200 and body['data']['disposition'] == 'finalized', body
        # Headers and payload (including timing with a frozen observational clock)
        # are exact. No stored fields are normalized away.
        observations.append((response.status_code, response.content, dict(response.headers), state(db),
                             copy.deepcopy(db.analytics_outbox), copy.deepcopy(db.stage_d_policy_watermarks), copy.deepcopy(db.reservation_idemp)))
    assert observations[0] == observations[1]


def test_new_payment_debt_conflicts_with_absence_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    db, options = prepare('ordinary', True, True)
    original = _FakeTransaction.batch_update
    fired = False

    def batch(tx: Any, statements: Any, **kwargs: Any) -> Any:
        nonlocal fired
        result = original(tx, statements, **kwargs)
        if not fired:
            fired = True
            row = debt_row()
            # A different transaction inserts debt after the absence check but
            # before this T-F commits. The range read must force an abort.
            with db.batch() as other:
                other.insert_or_update(table='tr_trust_event', columns=list(row), values=[tuple(row.values())])
        return result

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    result = invoke(db, options)
    assert result['outcome'] == 'settled' and db.aborts == 1
    assert db.rollback_calls == 1  # retry's debt guard miss, then fresh sequential recovery
    assert db.typed['tr_trust_event'][('workspace', 'debt')]['unrecovered_micro'] == 20
    assert db.typed['tr_credit_balance'][('workspace', 0)]['total_credits'] == 970
    assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 70
    assert db.typed['tr_key_limit'][('key', 0)]['usage'] == 70


@pytest.mark.parametrize('reserved,debt,expected', [(99, 0, 0), (100, 50, 0), (100, 0, 1)])
def test_credit_guards_execute_without_fake_predicate_assertions(reserved: int, debt: int, expected: int) -> None:
    """Execute the portable predicate/arithmetic itself; guard deletion makes debt/underflow reachable."""
    import sqlite3

    from trusted_router.storage_gcp_counter_dml import release_credit_no_debt_statement

    with sqlite3.connect(':memory:') as connection:
        connection.executescript('''
            CREATE TABLE tr_credit_balance(workspace_id TEXT, shard INTEGER,
                                           reserved INTEGER, total_usage INTEGER);
            CREATE TABLE tr_trust_event(workspace_id TEXT, kind TEXT, unrecovered_micro INTEGER);
        ''')
        connection.execute('INSERT INTO tr_credit_balance VALUES (?, 0, ?, 0)', ('workspace', reserved))
        connection.execute('INSERT INTO tr_trust_event VALUES (?, ?, ?)', ('workspace', 'payment', debt))
        sql, params, _ = release_credit_no_debt_statement(param_types, 'workspace', 100, 70, shard=0)
        count = connection.execute(sql, params).rowcount
        held, usage = connection.execute('SELECT reserved, total_usage FROM tr_credit_balance').fetchone()
        assert held >= 0
        assert count == expected
        assert (held, usage) == (reserved - 100 * expected, 70 * expected)


@pytest.mark.parametrize('column', ['credit_reserved_micro', 'key_reserved_micro'])
def test_nullable_legacy_hold_preserves_classifier_precedence(column: str) -> None:
    initial, options = prepare('ordinary', True, True)
    initial.reservations[options['reservation_id']][column] = None
    # The typed authorization is already terminal, but the hold record is not.
    # Main classifies the inconsistent request record before reading its hold;
    # C1 must not eagerly int(None) or compare None while building the batch.
    initial.gateway_authorizations[options['authorization_id']]['settled'] = True
    observations = []
    for impl in (main.typed_finalize_atomic, current.typed_finalize_atomic):
        db = clone(initial)
        result = invoke(db, copy.deepcopy(options), impl)
        assert result == {'outcome': 'error'}
        assert state(db) == state(initial)
        observations.append((result, state(db)))
    assert observations[0] == observations[1]
