"""Absent counters complete settlement through every committed-warning caller."""
# ruff: noqa: F811 - shared fixture
from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from tests.keyrow_assertions import assert_unbooked
from tests.test_async_settle_handler import (  # noqa: F401
    _drain_async,
    call,
    env,
    prepare,
)
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE


@pytest.mark.parametrize('other_shard', [False, True])
def test_frozen_drain_missing_key_rows(env, caplog, other_shard):
    store, db, _, _ = env
    body, auth, key = prepare(env, authorize={'skip_key_limit': True})
    rid = auth.credit_reservation_id
    assert db.reservations[rid]['key_reserved_micro'] == 0
    original = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    db.typed[KEY_LIMIT_TABLE] = (
        {(key.hash, 1): dict(original, shard=1)} if other_shard else {}
    )
    assert call(env, body).status_code == 202
    before = copy.deepcopy(db.settle_outbox[(auth.id, 'settle')])
    caplog.clear()
    result = _drain_async(concurrency=1)
    assert result['claimed'] == 1
    row = db.settle_outbox[(auth.id, 'settle')]
    if other_shard:
        assert not db.reservations[rid]['settled']
        assert row['status'] == 'pending'
        assert row['attempts'] == before['attempts'] + 1
        for name in ('settle_body', 'actual_cost_micro'):
            assert row[name] == before[name]
        assert 'key usage booking failed' in row['last_error']
        assert not any(getattr(r, 'metric', None) == 'key_usage_unbooked_total'
                       for r in caplog.records)
    else:
        assert db.reservations[rid]['settled']
        assert row['status'] == 'done'
        assert row['attempts'] == before['attempts'] + 1 == 1
        assert result['outcomes'] == {'settled_now': 1}
        assert db.typed[KEY_LIMIT_TABLE] == {}
        assert_unbooked(caplog, key.hash, 0, body['terminal']['charge_micro'], False)
        assert _drain_async(concurrency=1)['claimed'] == 0
        assert_unbooked(caplog, key.hash, 0, body['terminal']['charge_micro'], False)


@pytest.mark.parametrize('byok', [False, True])
def test_leased_finalize_missing_key_rows(caplog, byok):
    from tests.test_fastpath_pilot_store import _authorize
    from tests.test_leased_key_usage_sharding import _setup
    from trusted_router.storage_gcp_counters import leased_key_usage_shard

    store, db, workspace, key = _setup(4)
    aid = 'missing-key-leased'
    _, auth = _authorize(store, workspace, key, envelope='sealed', authorization_id=aid)
    db.typed[KEY_LIMIT_TABLE].clear()
    caplog.clear()
    result = store.typed_finalize_leased_result(
        aid, success=True, actual_microdollars=700,
        selected_usage_type='BYOK' if byok else 'Credits', authorization_snapshot=auth,
    )
    assert result is not None and result[0].finalized
    assert db.reservations[auth.credit_reservation_id]['settled']
    assert db.typed[KEY_LIMIT_TABLE] == {}
    assert_unbooked(caplog, key.hash, leased_key_usage_shard(aid, 4), 700, byok)
    assert store.typed_finalize_leased_result(
        aid, success=True, actual_microdollars=700,
        selected_usage_type='BYOK' if byok else 'Credits',
    ) is None
    assert_unbooked(caplog, key.hash, leased_key_usage_shard(aid, 4), 700, byok)


def test_snapshot_reaper_missing_key_rows(caplog):
    from tests.fakes.spanner import _ParamTypes
    from tests.test_stage_d_heartbeat import NOW, _heartbeat, _seed, _seed_reaper_counters
    from trusted_router.storage_gcp_authorize import reap_expired_reservations_result

    db, auth = _seed()
    _seed_reaper_counters(db)
    db.reservations['reservation']['key_reserved_micro'] = 0
    db.typed[KEY_LIMIT_TABLE].clear()
    assert _heartbeat(db).accepted
    caplog.clear()
    result = reap_expired_reservations_result(
        db, _ParamTypes, now=NOW + timedelta(seconds=301), snapshot_booking_enabled=True,
    )
    assert result.count == result.snapshot_bookings == 1
    assert db.reservations['reservation']['settled']
    assert db.reservations['reservation']['actual_micro'] == 120
    assert db.typed[KEY_LIMIT_TABLE] == {}
    assert_unbooked(caplog, auth.key_hash, 0, 120, False)
    replay = reap_expired_reservations_result(
        db, _ParamTypes, now=NOW + timedelta(seconds=301), snapshot_booking_enabled=True,
    )
    assert replay.count == 0
    assert_unbooked(caplog, auth.key_hash, 0, 120, False)


def test_typed_finalize_missing_key_rows(caplog):
    from tests.test_settle_c1 import prepare
    from tests.test_settle_speculative_batch import invoke

    db, options = prepare('deleted_key', False, False)
    caplog.clear()
    assert invoke(db, options)['outcome'] == 'settled'
    assert db.reservations[options['reservation_id']]['settled']
    assert db.typed[KEY_LIMIT_TABLE] == {}
    assert_unbooked(caplog, 'key', 0, 70, False)
    assert invoke(db, options)['outcome'] == 'already_settled'
    assert_unbooked(caplog, 'key', 0, 70, False)
