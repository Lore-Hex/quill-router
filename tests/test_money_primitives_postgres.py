from __future__ import annotations

import pytest
from psycopg.types.numeric import Int8

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from trusted_router.storage_models import CreditAccount, CreditMovement


def _seed_workspace(store: object, conn: object, workspace_id: str) -> None:
    store._write_entity_tx(  # type: ignore[attr-defined]
        conn,
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id),
    )
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO tr_credit_balance "
        "(workspace_id, shard, total_credits, total_usage, reserved) "
        "VALUES (%s, 0, 0, 0, 0)",
        (workspace_id,),
    )


def test_credit_movement_insert_binds_bigint_without_server_preparation() -> None:
    calls: list[dict[str, object]] = []

    class RecordingConnection:
        def execute(
            self,
            sql: str,
            params: tuple[object, ...],
            **kwargs: object,
        ) -> None:
            calls.append({"sql": sql, "params": params, "kwargs": kwargs})

    from trusted_router.storage_postgres import PostgresStore

    PostgresStore._insert_credit_movement_tx(
        RecordingConnection(),
        CreditMovement(
            account_id="user:test",
            movement_id="movement-test",
            kind="custom_model_payout",
            amount_microdollars=1,
        ),
    )
    assert len(calls) == 1
    params = calls[0]["params"]
    assert isinstance(params, tuple)
    assert isinstance(params[3], Int8)
    assert int(params[3]) == 1
    assert calls[0]["kwargs"] == {"prepare": False}


def test_postgres_guarded_debit_runs_real_conditional_sql_once() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace_id = "ws-pg-debit"
    _seed_workspace(store, conn, workspace_id)
    assert store.credit_workspace_typed_direct(workspace_id, 100, "evt-pg-fund")

    assert (
        store.debit_workspace_guarded(
            workspace_id,
            60,
            "evt-pg-debit",
            kind="verification_fee",
        )
        == "accepted"
    )
    assert (
        store.debit_workspace_guarded(
            workspace_id,
            60,
            "evt-pg-debit",
            kind="verification_fee",
        )
        == "duplicate"
    )
    assert (
        store.debit_workspace_guarded(
            workspace_id,
            41,
            "evt-pg-overdraw",
            kind="adjustment",
        )
        == "insufficient"
    )
    assert conn.balance(workspace_id) == (40, 0, 0)


def test_postgres_earnings_transfer_moves_money_atomically() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace_id = "ws-pg-transfer"
    user_id = "user-pg-transfer"
    _seed_workspace(store, conn, workspace_id)

    assert store.credit_user_earnings(
        user_id,
        100,
        "evt-pg-payout",
        custom_model_id="model-pg",
        payer_workspace_id="ws-payer",
    )
    assert not store.credit_user_earnings(user_id, 100, "evt-pg-payout")
    assert (
        store.transfer_earnings_to_workspace(
            user_id,
            workspace_id,
            60,
            "evt-pg-transfer",
        )
        == "accepted"
    )
    assert (
        store.transfer_earnings_to_workspace(
            user_id,
            workspace_id,
            60,
            "evt-pg-transfer",
        )
        == "duplicate"
    )
    assert store.earnings_summary(user_id) == {
        "total_earned": 100,
        "total_transferred": 60,
        "available": 40,
    }
    assert conn.balance(workspace_id) == (60, 0, 0)
    payout = store.list_credit_movements(
        f"user:{user_id}",
        kinds=["custom_model_payout"],
    )
    assert [(movement.custom_model_id, movement.amount_microdollars) for movement in payout] == [
        ("model-pg", 100)
    ]
    assert store.custom_model_earnings_by_model(
        user_id,
        since="1970-01-01T00:00:00Z",
    ) == {"model-pg": 100}


def test_postgres_earnings_transfer_rolls_back_every_write_on_failure() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace_id = "ws-pg-transfer-rollback"
    user_id = "user-pg-transfer-rollback"
    _seed_workspace(store, conn, workspace_id)
    assert store.credit_user_earnings(user_id, 100, "evt-pg-rollback-fund")

    conn.fail_on = "INSERT INTO tr_credit_movement"
    with pytest.raises(RuntimeError, match="connection reset"):
        store.transfer_earnings_to_workspace(
            user_id,
            workspace_id,
            60,
            "evt-pg-transfer-rollback",
        )
    conn.fail_on = None

    assert store.earnings_summary(user_id)["available"] == 100
    assert conn.balance(workspace_id) == (0, 0, 0)
    assert not conn.has_entity("stripe_event", "evt-pg-transfer-rollback")


def test_postgres_lifetime_topup_is_atomic_with_grant_claim() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace_id = "ws-pg-topup"
    user_id = "user-pg-topup"
    _seed_workspace(store, conn, workspace_id)

    assert store.credit_workspace_typed_direct(
        workspace_id,
        25,
        "evt-pg-topup",
        lifetime_topup_user_id=user_id,
    )
    assert not store.credit_workspace_typed_direct(
        workspace_id,
        25,
        "evt-pg-topup",
        lifetime_topup_user_id=user_id,
    )
    assert store.get_lifetime_topup_microdollars(user_id) == 25
