"""Upgraded DSQL rows must behave exactly like explicit tier/epoch zeroes."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest

from tests.fakes.postgres import SqlitePostgresConn, postgres_store_on, sqlite_postgres_conn
from trusted_router.config import Settings
from trusted_router.storage_legacy_trust import (
    BillingPausedError,
    postgres_pause,
    recover_released_postgres,
    reject_postgres_reservation,
)
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance, Workspace
from trusted_router.types import UsageType


@pytest.fixture(params=[None, 0], ids=["NULL", "zero"])
def upgraded(request: pytest.FixtureRequest) -> Iterator[tuple[Any, SqlitePostgresConn, Workspace]]:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    store.trust_settings = Settings(
        environment="test", spend_lease_trust_eligibility_enabled=True,
    )
    owner = store.ensure_user("dsql@example.com", trial_credit_microdollars=0)
    workspace = store.create_workspace(owner.id, "upgraded", trial_credit_microdollars=1_000)
    conn.execute(
        "UPDATE tr_credit_balance SET trust_tier = %s, pause_epoch = %s "
        "WHERE workspace_id = %s",
        (request.param, request.param, workspace.id),
    )
    try:
        yield store, conn, workspace
    finally:
        conn._raw.close()


def _tiers(conn: SqlitePostgresConn, workspace: Workspace) -> list[int | None]:
    return [
        row[0] for row in conn.execute(
            "SELECT trust_tier FROM tr_credit_balance WHERE workspace_id = %s",
            (workspace.id,),
        ).fetchall()
    ]


def _abuse(store: Any, workspace: Workspace, reference: str) -> None:
    assert store.record_workspace_abuse_and_demote(
        workspace.id, abuse_ref=reference, operator_identity="operator", reason="abuse",
    )


def test_postgres_pause_reads_upgraded_epoch_as_zero(upgraded: Any) -> None:
    _store, conn, workspace = upgraded
    assert postgres_pause(conn, workspace.id) == (False, 0)


@pytest.mark.parametrize("operation", ["pause", "clear"])
def test_postgres_abuse_epoch_increments_from_zero(upgraded: Any, operation: str) -> None:
    store, conn, workspace = upgraded
    for expected in (1, 2):
        if operation == "pause":
            _abuse(store, workspace, f"case-{expected}")
        else:
            assert store.clear_workspace_abuse_pause(
                workspace.id, abuse_ref=f"case-{expected}",
                operator_identity="operator", reason="cleared",
            )
        assert postgres_pause(conn, workspace.id) == (operation == "pause", expected)


@pytest.mark.parametrize("pause_then_clear", [False, True])
def test_postgres_armed_reservation_rejected_after_pause(
    upgraded: Any, pause_then_clear: bool,
) -> None:
    store, conn, workspace = upgraded
    _raw, key = store.create_api_key(
        workspace_id=workspace.id, name="key", creator_user_id=workspace.owner_user_id,
        limit_microdollars=1_000,
    )
    store.reserve_key_limit(key.hash, 100, usage_type=UsageType.CREDITS)
    reservation = store.reserve(workspace.id, key.hash, 100, idempotency_key="before-pause")
    assert conn.balance(workspace.id) == (1_000, 0, 100)
    assert store._read_entity("reservation_pause_epoch", reservation.id, dict) == {"pause_epoch": 0}
    _abuse(store, workspace, "case")
    if pause_then_clear:
        assert store.clear_workspace_abuse_pause(
            workspace.id, abuse_ref="case", operator_identity="operator", reason="cleared",
        )
        # With causes cleared, only the changed epoch can invalidate the old reservation.
        assert postgres_pause(conn, workspace.id) == (False, 2)
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(
            workspace_id=workspace.id, key_hash=key.hash, model_id="m", provider="p",
            usage_type=UsageType.CREDITS, estimated_microdollars=100,
            credit_reservation_id=reservation.id, idempotency_key="before-pause",
        )
    assert conn.balance(workspace.id) == (1_000, 0, 0)
    assert conn.execute(
        "SELECT reserved FROM tr_key_limit WHERE key_hash = %s", (key.hash,),
    ).fetchone() == (0,)
    assert conn.count_entities("gateway_authorization") == 0


def test_postgres_null_reservation_epoch_is_zero(upgraded: Any) -> None:
    store, _conn, workspace = upgraded
    _raw, key = store.create_api_key(
        workspace_id=workspace.id, name="key", creator_user_id=workspace.owner_user_id,
    )
    reservation = store.reserve(workspace.id, key.hash, 100)
    store._run_transaction(lambda tx: store._write_entity_tx(
        tx, "reservation_pause_epoch", reservation.id, {"pause_epoch": None},
    ))
    authorization = store.create_gateway_authorization(
        workspace_id=workspace.id, key_hash=key.hash, model_id="m", provider="p",
        usage_type=UsageType.CREDITS, estimated_microdollars=100,
        credit_reservation_id=reservation.id,
    )
    assert authorization.credit_reservation_id == reservation.id


@pytest.mark.parametrize("operation", ["identity", "remainder"])
def test_postgres_demotion_never_promotes_null_tier(upgraded: Any, operation: str) -> None:
    store, conn, workspace = upgraded
    if operation == "identity":
        store.set_user_identity_status(workspace.owner_user_id, status="approved")
        store.set_user_identity_status(workspace.owner_user_id, status="declined")
    else:
        conn.execute(
            "INSERT INTO tr_trust_demotion_remainder "
            "(owner_user_id, workspace_id, target_identity_ceiling) VALUES (%s, %s, 1)",
            (workspace.owner_user_id, workspace.id),
        )
        assert store.process_trust_demotion_remainders() == 1
        assert store.process_trust_demotion_remainders() == 0
    assert _tiers(conn, workspace) == [0]


def test_postgres_tier_computation_and_latch_tolerate_null(upgraded: Any) -> None:
    store, conn, workspace = upgraded
    store.set_workspace_trust_override(
        workspace.id, tier=0, identity_bypass=False,
        operator_identity="operator", reason="compute unpaid tier",
    )
    assert _tiers(conn, workspace) == [0]
    _abuse(store, workspace, "latched")
    store.set_workspace_trust_override(
        workspace.id, tier=3, identity_bypass=True,
        operator_identity="operator", reason="latch still wins",
    )
    assert _tiers(conn, workspace) == [0]
    assert conn.execute(
        "SELECT trust_latched_at IS NOT NULL FROM tr_credit_balance WHERE workspace_id = %s",
        (workspace.id,),
    ).fetchone() == (1,)


def _payment(store: Any, workspace: Workspace) -> dt.datetime:
    now = dt.datetime.now(dt.UTC)
    assert store.credit_workspace_typed_direct(
        workspace.id, 1_000, "payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_dsql", now),
        payment_amount_microdollars=1_000, currency="usd",
    )
    return now


def test_postgres_principal_recovery_pause_epoch(upgraded: Any) -> None:
    store, conn, workspace = upgraded
    now = _payment(store, workspace)
    reservation = store.reserve(workspace.id, "key", 2_000)
    result = store.record_adverse_trust_event(AdverseTrustEvent(
        event_id="dispute", provider="stripe", kind="dispute", adverse_ref="dp_dsql",
        original_payment_ref="pi_dsql", amount_micro=1_000,
        provider_subtype="charge.dispute.created", lifecycle_status="succeeded",
        occurred_at=now, provider_ordering_watermark="01:dispute", payload="{}",
    ))
    assert (result.outcome, result.recovered_micro, result.unrecovered_micro) == ("applied", 0, 1_000)
    assert postgres_pause(conn, workspace.id) == (True, 1)
    store._run_transaction(lambda tx: reject_postgres_reservation(tx, store, reservation.id))
    assert postgres_pause(conn, workspace.id) == (False, 2)
    assert conn.balance(workspace.id) == (1_000, 0, 0)


def test_postgres_release_recovery_increments_upgraded_epoch(upgraded: Any) -> None:
    store, conn, workspace = upgraded
    _payment(store, workspace)
    for expected in (1, 2):
        conn.execute(
            "UPDATE tr_trust_event SET unrecovered_micro = 100 "
            "WHERE workspace_id = %s AND event_id = 'payment'",
            (workspace.id,),
        )
        store._run_transaction(lambda tx: recover_released_postgres(tx, workspace.id, store))
        assert postgres_pause(conn, workspace.id) == (False, expected)
        assert conn.balance(workspace.id) == (2_000 - 100 * expected, 0, 0)
