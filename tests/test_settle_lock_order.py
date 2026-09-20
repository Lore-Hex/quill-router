"""Observe real counter DML order and rollback at the fake Spanner boundary."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest

from tests.fakes.spanner import _FakeTransaction, _ParamTypes
from tests.test_stage_d_heartbeat import NOW, _seed, _seed_reaper_counters
from trusted_router import storage_gcp_authorize as billing
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE


@pytest.fixture
def updates(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    calls: list[tuple[Any, str]] = []
    original = _FakeTransaction.execute_update

    def record(transaction: Any, sql: str, **kwargs: Any) -> int:
        calls.append((transaction, sql))
        return original(transaction, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_update", record)
    return calls


def _statements(updates: list[tuple[Any, str]]) -> list[str]:
    assert len({id(tx) for tx, _ in updates}) == 1
    return [sql for _, sql in updates]


def _credit_before_key(statements: list[str]) -> None:
    credit = [i for i, sql in enumerate(statements) if sql.startswith("UPDATE tr_credit_balance")]
    key = [i for i, sql in enumerate(statements) if sql.startswith("UPDATE tr_key_limit")]
    assert credit and key
    assert max(credit) < min(key), statements
    assert key[-1] == len(statements) - 1, statements


@pytest.mark.parametrize("skip_key_limit", [False, True], ids=["capped", "uncapped"])
def test_authorize_credit_before_key(updates: list[tuple[Any, str]], skip_key_limit: bool) -> None:
    # Always exercise capped ordering as well, so reverting the flip invalidates
    # the uncapped compatibility case too.
    for skip in (False, skip_key_limit):
        db, _ = _seed()
        _seed_reaper_counters(db, hold=0)
        db.typed[KEY_LIMIT_TABLE][("key", 0)]["limit_micro"] = 1_000
        updates.clear()
        result = _authorize(db, skip_key_limit=skip)
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        statements = _statements(updates)
        counters = [
            sql
            for sql in statements
            if sql.startswith(("UPDATE tr_credit_balance", "UPDATE tr_key_limit"))
        ]
        if skip:
            assert len(counters) == 1
            assert counters[0].startswith("UPDATE tr_credit_balance")
        else:
            _credit_before_key(counters)
        res = db.reservations[result["reservation_id"]]
        assert res["credit_reserved_micro"] == 100
        assert res["key_reserved_micro"] == (0 if skip else 100)


def _authorize(db: Any, *, skip_key_limit: bool = False) -> dict[str, Any]:
    return billing.authorize_atomic(
        db,
        _ParamTypes,
        workspace_id="workspace",
        key_hash="key",
        estimate=100,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        idempotency_scope=None,
        idempotency_fingerprint=None,
        expires_at=NOW + timedelta(seconds=300),
        build_auth_body=lambda aid, rid: "{}",
        skip_key_limit=skip_key_limit,
    )


@pytest.mark.parametrize("failure", ["credit", "key", "missing_key"])
def test_authorize_rejection_order_and_rollback(
    updates: list[tuple[Any, str]], failure: str
) -> None:
    db, _ = _seed()
    _seed_reaper_counters(db, hold=0)
    db.typed[KEY_LIMIT_TABLE][("key", 0)]["limit_micro"] = 1_000
    if failure == "credit":
        db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_credits"] = 0
    elif failure == "key":
        db.typed[KEY_LIMIT_TABLE][("key", 0)]["reserved"] = 1_000
    else:
        db.typed[KEY_LIMIT_TABLE].clear()
    before = copy.deepcopy((db.typed, db.reservations, db.gateway_authorizations))
    updates.clear()
    result = _authorize(db)
    assert (
        result["outcome"]
        == {
            "credit": billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,
            "key": billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED,
            "missing_key": billing.AuthorizeOutcome.KEY_MISSING,
        }[failure]
    )
    statements = _statements(updates)
    if failure == "credit":
        assert statements and all(sql.startswith("UPDATE tr_credit_balance") for sql in statements)
    else:
        _credit_before_key(statements)
    assert (db.typed, db.reservations, db.gateway_authorizations) == before


@pytest.mark.parametrize("path", ["settle", "typed_finalize", "reaper"])
@pytest.mark.parametrize("failure", [None, "credit", "key"])
def test_release_credit_before_key_and_rollback(
    updates: list[tuple[Any, str]],
    path: str,
    failure: str | None,
) -> None:
    # A healthy transaction pins order even in the row-count error cases.
    for broken in (None, failure) if failure else (None,):
        db, auth = _seed(cohort=False)
        _seed_reaper_counters(db)
        if broken:
            table = CREDIT_BALANCE_TABLE if broken == "credit" else KEY_LIMIT_TABLE
            row_key = ("workspace", 0) if broken == "credit" else ("key", 0)
            db.typed[table][row_key]["reserved"] = 0
        before = copy.deepcopy((db.typed, db.reservations, db.gateway_authorizations))
        updates.clear()
        if path == "settle":
            result = billing.settle_atomic(
                db,
                _ParamTypes,
                reservation_id="reservation",
                actual_micro=70,
                settled_usage_type="Credits",
                success=True,
            )["outcome"]
        elif path == "typed_finalize":
            auth.settled = True
            result = billing.typed_finalize_atomic(
                db,
                _ParamTypes,
                reservation_id="reservation",
                authorization_id=auth.id,
                actual_micro=70,
                settled_usage_type="Credits",
                success=True,
                now=NOW,
                authorization=auth,
                auth_body_settled="{}",
                outbox_available=False,
            )["outcome"]
        else:
            result = billing._finalize_reaped_reservation_atomic(
                db,
                _ParamTypes,
                reservation_id="reservation",
                reap_now=NOW + timedelta(seconds=301),
                guard_outbox=False,
                snapshot_booking_enabled=False,
                operational_analytics_outbox=None,
            ).outcome
        statements = _statements(updates)
        if broken:
            assert result == billing.SettleOutcome.ERROR
            assert (db.typed, db.reservations, db.gateway_authorizations) == before
        else:
            assert result == billing.SettleOutcome.SETTLED
            _credit_before_key(statements)
            assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["reserved"] == 0
            assert db.typed[KEY_LIMIT_TABLE][("key", 0)]["reserved"] == 0
            amount = 0 if path == "reaper" else 70
            assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == amount
            assert db.typed[KEY_LIMIT_TABLE][("key", 0)]["usage"] == amount
