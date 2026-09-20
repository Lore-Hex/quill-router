"""Observe credit-before-key statement order and rollback at the fake Spanner boundary."""

from __future__ import annotations

import copy
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes.spanner import _ParamTypes
from tests.fakes.spanner_order import (
    authorize_credit_before_key,
    credit_before_key,
    record_statements,
    transaction_statements,
)
from tests.test_credit_row_sharding_increment3 import _seed as _seed_fragmented_credit
from tests.test_spend_lease_authorize import _atomic_harness
from tests.test_stage_d_heartbeat import NOW, _seed, _seed_reaper_counters
from trusted_router import storage_gcp_authorize as billing
from trusted_router.storage_errors import StoreUnavailable
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    return record_statements(monkeypatch)


@pytest.mark.parametrize("skip_key_limit", [False, True], ids=["capped", "uncapped"])
def test_authorize_credit_before_key(calls: list[tuple[Any, str]], skip_key_limit: bool) -> None:
    # Always exercise capped ordering as well, so reverting the flip invalidates
    # the uncapped compatibility case too.
    for skip in (False, skip_key_limit):
        db, _ = _seed()
        _seed_reaper_counters(db, hold=0)
        db.typed[KEY_LIMIT_TABLE][("key", 0)]["limit_micro"] = 1_000
        calls.clear()
        result = _authorize(db, skip_key_limit=skip)
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        statements = transaction_statements(calls)
        if skip:
            assert not any("tr_key_limit" in sql for sql in statements)
            assert any(sql.startswith("update tr_credit_balance") for sql in statements)
        else:
            authorize_credit_before_key(statements)
        res = db.reservations[result["reservation_id"]]
        assert res["credit_reserved_micro"] == 100
        assert res["key_reserved_micro"] == (0 if skip else 100)


def _authorize(
    db: Any, *, skip_key_limit: bool = False, has_credit_candidate: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    return billing.authorize_atomic(
        db,
        _ParamTypes,
        workspace_id="workspace",
        key_hash="key",
        estimate=100,
        has_credit_candidate=has_credit_candidate,
        reservation_usage_type="Credits" if has_credit_candidate else "BYOK",
        idempotency_scope=None,
        idempotency_fingerprint=None,
        expires_at=NOW + timedelta(seconds=300),
        build_auth_body=lambda aid, rid: "{}",
        skip_key_limit=skip_key_limit,
        **kwargs,
    )


@pytest.mark.parametrize("failure", ["credit", "key", "missing_key"])
def test_authorize_rejection_order_and_rollback(
    calls: list[tuple[Any, str]], failure: str
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
    calls.clear()
    result = _authorize(db)
    assert (
        result["outcome"]
        == {
            "credit": billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,
            "key": billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED,
            "missing_key": billing.AuthorizeOutcome.KEY_MISSING,
        }[failure]
    )
    statements = transaction_statements(calls)
    if failure == "credit":
        assert statements and all(sql.startswith("update tr_credit_balance") for sql in statements)
    else:
        authorize_credit_before_key(statements)
    assert (db.typed, db.reservations, db.gateway_authorizations) == before


@pytest.mark.parametrize("path", ["settle", "typed_finalize", "reaper"])
@pytest.mark.parametrize("failure", [None, "credit", "key"])
def test_release_credit_before_key_and_rollback(
    calls: list[tuple[Any, str]],
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
        calls.clear()
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
        statements = transaction_statements(calls)
        if broken:
            assert result == billing.SettleOutcome.ERROR
            assert (db.typed, db.reservations, db.gateway_authorizations) == before
        else:
            assert result == billing.SettleOutcome.SETTLED
            credit_before_key(statements, key_last=True)
            assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["reserved"] == 0
            assert db.typed[KEY_LIMIT_TABLE][("key", 0)]["reserved"] == 0
            amount = 0 if path == "reaper" else 70
            assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == amount
            assert db.typed[KEY_LIMIT_TABLE][("key", 0)]["usage"] == amount


@pytest.mark.parametrize("has_credit_candidate", [False, True], ids=["byok", "credits"])
@pytest.mark.parametrize("paused", [False, True], ids=["active", "paused"])
def test_armed_pause_precedes_capped_key_and_rolls_back(
    calls: list[tuple[Any, str]], has_credit_candidate: bool, paused: bool,
) -> None:
    db, _ = _seed()
    _seed_reaper_counters(db, hold=0)
    # A pause intentionally takes precedence over an exhausted key cap.
    db.typed[KEY_LIMIT_TABLE][("key", 0)]["limit_micro"] = 0 if paused else 1_000
    row = db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]
    row.update(billing_pause_causes=["abuse"] if paused else [], pause_epoch=int(paused))
    before = copy.deepcopy((db.typed, db.reservations, db.gateway_authorizations))
    calls.clear()
    result = _authorize(
        db, has_credit_candidate=has_credit_candidate,
        trust_settings=SimpleNamespace(spend_lease_trust_eligibility_enabled=True),
    )
    statements = transaction_statements(calls)
    pause = next(i for i, sql in enumerate(statements)
                 if sql.startswith("select billing_pause_causes, pause_epoch"))
    if paused:
        assert result["outcome"] == "billing_paused"
        assert not any("tr_key_limit" in sql for sql in statements)
        assert (db.typed, db.reservations, db.gateway_authorizations) == before
        if has_credit_candidate:
            assert statements[0].startswith("update tr_credit_balance")
    else:
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        assert pause < next(i for i, sql in enumerate(statements) if "tr_key_limit" in sql)
        authorize_credit_before_key(statements)


def _fragmented_authorize(store: Any, key: Any) -> tuple[str, Any]:
    return store.authorize_gateway_typed(
        workspace_id=key.workspace_id, key_hash=key.hash, estimate=10_000,
        has_credit_candidate=True, reservation_usage_type="Credits",
        model_id="model", provider="provider", requested_model_id=None,
        candidate_model_ids=["model"], region="us", endpoint_id="endpoint",
        candidate_endpoint_ids=["endpoint"], idempotency_key=None,
        idempotency_fingerprint=None, key_usage_shards=4,
    )


def _fragmented_store(credits: list[int], key_limit: int) -> tuple[Any, Any, Any]:
    store, db, key = _seed_fragmented_credit(credits)
    key.usage_shard_count = 4
    store._write_entity("api_key", key.hash, key)
    rows = db.typed[KEY_LIMIT_TABLE]
    for row_key in list(rows):
        if row_key[0] == key.hash and row_key[1] >= 4:
            del rows[row_key]
    for shard in range(1, 4):
        rows[(key.hash, shard)] = {**rows[(key.hash, 0)], "shard": shard}
    assert store.api_keys.update(key.hash, {"limit_microdollars": key_limit}) is not None
    return store, db, key


@pytest.mark.parametrize(
    ("credits", "key_limit", "outcome"),
    [
        ([6_000, 6_000], 20_000, billing.AuthorizeOutcome.ACCEPTED),
        ([6_000, 6_000], 40_000, billing.AuthorizeOutcome.ACCEPTED),
        ([12_000, 0], 20_000, billing.AuthorizeOutcome.ACCEPTED),
        ([6_000, 6_000], 8_000, billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED),
        ([4_000, 4_000], 20_000, billing.AuthorizeOutcome.INSUFFICIENT_CREDITS),
    ],
    ids=["both", "credit_only", "key_only", "key_exhausted", "credit_exhausted"],
)
def test_fragmented_credit_and_key_repair_conserves_totals(
    credits: list[int], key_limit: int, outcome: str,
) -> None:
    store, db, key = _fragmented_store(credits, key_limit)
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == outcome
    hold = 10_000 if outcome == billing.AuthorizeOutcome.ACCEPTED else 0
    credit_rows = list(db.typed[CREDIT_BALANCE_TABLE].values())
    key_rows = list(db.typed[KEY_LIMIT_TABLE].values())
    assert sum(row["total_credits"] for row in credit_rows) == sum(credits)
    assert sum(row["total_usage"] for row in credit_rows) == 0
    assert sum(row["reserved"] for row in credit_rows) == hold
    assert sum(row["limit_micro"] for row in key_rows) == key_limit
    assert sum(row["usage"] + row["byok_usage"] for row in key_rows) == 0
    assert sum(row["reserved"] for row in key_rows) == hold
    assert len(db.reservations) == int(bool(hold))
    if hold:
        assert authorization is not None
        reservation = db.reservations[authorization.credit_reservation_id]
        assert reservation["credit_reserved_micro"] == reservation["key_reserved_micro"] == hold
    else:
        assert authorization is None


def test_key_repair_retries_on_the_credit_shard_that_held_funds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Funds sit outside the bounded credit prefix AND the key escrow is fragmented.

    The credit fix is "retry that exact funded shard", not a rebalance. The key
    repair that follows must retry on that same shard; retrying on the original
    bounded prefix misses the funds again and turned this affordable request
    into a retryable 503.
    """
    from trusted_router import storage_gcp

    shards = billing.MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION + 2
    credits = [0] * (shards - 1) + [12_000]
    store, db, key = _fragmented_store(credits, 20_000)
    # Deterministic order with the funded shard beyond the per-transaction bound.
    monkeypatch.setattr(
        storage_gcp, "randomized_credit_shards", lambda count: tuple(range(count))
    )
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    reservation = db.reservations[authorization.credit_reservation_id]
    assert reservation["credit_shard"] == shards - 1
    assert reservation["credit_reserved_micro"] == reservation["key_reserved_micro"] == 10_000
    credit_rows = list(db.typed[CREDIT_BALANCE_TABLE].values())
    assert sum(row["reserved"] for row in credit_rows) == 10_000
    assert sum(row["total_credits"] for row in credit_rows) == 12_000


@pytest.mark.parametrize("credits", [[6_000, 6_000], [12_000, 0]], ids=["both", "key_only"])
def test_credit_consumed_after_key_repair_is_retryable(
    monkeypatch: pytest.MonkeyPatch, credits: list[int],
) -> None:
    from trusted_router import storage_gcp_key_escrow as escrow

    store, db, key = _fragmented_store(credits, 20_000)
    rebalance = escrow.rebalance_key_limit_headroom

    def consume_after_repair(*args: Any, **kwargs: Any) -> bool:
        repaired = rebalance(*args, **kwargs)
        assert repaired
        # Another request commits spend between the key repair and our retry.
        row = max(db.typed[CREDIT_BALANCE_TABLE].values(), key=lambda r: r["total_credits"])
        row["total_usage"] += 3_000
        return repaired

    monkeypatch.setattr(escrow, "rebalance_key_limit_headroom", consume_after_repair)
    with pytest.raises(StoreUnavailable, match="credit headroom changed concurrently; retry"):
        _fragmented_authorize(store, key)
    assert sum(row["reserved"] for row in db.typed[CREDIT_BALANCE_TABLE].values()) == 0
    assert sum(row["reserved"] for row in db.typed[KEY_LIMIT_TABLE].values()) == 0
    assert not db.reservations


@pytest.mark.parametrize("escrowed", [False, True], ids=["ordinary_hold", "lease_escrowed"])
@pytest.mark.parametrize("key_exhausted", [False, True], ids=["accepted", "key_rejected"])
def test_spend_lease_credit_before_key_and_rollback(
    calls: list[tuple[Any, str]], escrowed: bool, key_exhausted: bool,
) -> None:
    db, plan, ledger = _atomic_harness()
    if key_exhausted:
        db.typed[KEY_LIMIT_TABLE][("key-hash", 0)]["limit_micro"] = 0
    before = copy.deepcopy((
        db.typed, db.rows, db.reservations, db.gateway_authorizations,
        db.spend_lease_arbitrations, db.spend_lease_open,
    ))
    calls.clear()
    result = billing.authorize_atomic(
        db, _ParamTypes, workspace_id="workspace-1", key_hash="key-hash",
        estimate=500, has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope="scope-1", idempotency_fingerprint="fingerprint-1",
        expires_at=NOW + timedelta(hours=1), build_auth_body=lambda aid, rid: "{}",
        authorization_id=plan.provisional_id,
        spend_lease_hook=lambda tx, shard: plan.transaction_hook(
            tx, _ParamTypes, "workspace-1", shard,
        ),
        credit_escrowed_by_spend_lease=escrowed,
    )
    authorize_credit_before_key(transaction_statements(calls))
    assert ledger.binds == 0
    if key_exhausted:
        assert result["outcome"] == billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED
        assert (
            db.typed, db.rows, db.reservations, db.gateway_authorizations,
            db.spend_lease_arbitrations, db.spend_lease_open,
        ) == before
    else:
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        assert result["bound"] is True
        assert db.typed[CREDIT_BALANCE_TABLE][("workspace-1", 0)]["reserved"] == (
            plan.artifact.cap_micro + (0 if escrowed else 500)
        )
        assert db.typed[KEY_LIMIT_TABLE][("key-hash", 0)]["reserved"] == 500


@pytest.mark.parametrize("receipt", [False, True], ids=["ordinary_fallback", "admission_rejected"])
def test_spend_lease_inverse_credit_before_key_and_admission_rollback(
    calls: list[tuple[Any, str]], receipt: bool,
) -> None:
    from trusted_router.storage_gcp_spend_lease import register_claim

    db, plan, ledger = _atomic_harness()
    db.run_in_transaction(lambda tx: register_claim(tx, _ParamTypes, plan.scope, "winner"))
    if receipt:
        # Admission refusal may now precede the exhausted key, like pause refusal.
        db.typed[KEY_LIMIT_TABLE][("key-hash", 0)]["limit_micro"] = 0
    before = copy.deepcopy((
        db.typed, db.rows, db.reservations, db.gateway_authorizations,
        db.spend_lease_arbitrations, db.spend_lease_open,
    ))
    calls.clear()
    result = billing.authorize_atomic(
        db, _ParamTypes, workspace_id="workspace-1", key_hash="key-hash",
        estimate=500, has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope=plan.scope, idempotency_fingerprint="fingerprint-1",
        expires_at=NOW + timedelta(hours=1), build_auth_body=lambda aid, rid: "{}",
        authorization_id=plan.provisional_id,
        spend_lease_hook=lambda tx, shard: plan.transaction_hook(
            tx, _ParamTypes, "workspace-1", shard,
        ),
        spend_lease_receipt_hash="receipt" if receipt else None,
        credit_escrowed_by_spend_lease=receipt,
    )
    statements = transaction_statements(calls)
    assert any("from tr_trust_event" in sql for sql in statements)
    assert ledger.binds == 0
    if receipt:
        assert result["outcome"] == "admission_rejected:scope_conflict"
        assert not any("tr_key_limit" in sql for sql in statements)
        assert (
            db.typed, db.rows, db.reservations, db.gateway_authorizations,
            db.spend_lease_arbitrations, db.spend_lease_open,
        ) == before
    else:
        authorize_credit_before_key(statements)
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        assert result["bound"] is False
        assert db.typed[CREDIT_BALANCE_TABLE][("workspace-1", 0)]["reserved"] == 500
        assert db.typed[KEY_LIMIT_TABLE][("key-hash", 0)]["reserved"] == 500
