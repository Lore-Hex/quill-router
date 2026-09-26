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
def test_frozen_sequential_equivalence(
    stable_ids: None, scenario: str, armed: bool, mode: str,
) -> None:
    def run(module: Any) -> tuple[Any, Any]:
        db = _database()
        db.now = NOW
        opts = _options(mode)
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
