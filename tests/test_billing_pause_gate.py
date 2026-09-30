"""The authorize-time billing-pause gate, on every backend that enforces it.

``Settings.spend_lease_trust_eligibility_enabled`` (the name predates the
spend-lease pilot's removal) arms two things: the typed Spanner authorize
transaction reads the selected shard's pause state inside the transaction
and takes no holds while paused, and the legacy entity path refuses to create
an authorization for a paused workspace, releases the holds it already took,
recovers refunded principal, and keeps refusing a paused request's
idempotency key after the pause clears. These tests came from the pilot's
trust suite; the behaviour they pin is live for every workspace.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings

ARMED = Settings(environment="test", spend_lease_trust_eligibility_enabled=True)


def _legacy_store(backend: str) -> Any:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    from trusted_router.storage import InMemoryStore

    store = InMemoryStore() if backend == "memory" else postgres_store_on(sqlite_postgres_conn())
    store.trust_settings = ARMED
    return store


def _set_pause(store: Any, backend: str, workspace_id: str, causes: str, epoch: int | None = None) -> None:
    if backend == "memory":
        store.workspaces[workspace_id].billing_pause_causes = [] if causes == "[]" else ["abuse"]
        store.credit_trust_shards[(workspace_id, 0)]["billing_pause_causes"] = (
            [] if causes == "[]" else ["abuse"]
        )
        if epoch is not None:
            store.credit_trust_shards[(workspace_id, 0)]["pause_epoch"] = epoch
        return
    sql = "UPDATE tr_credit_balance SET billing_pause_causes = %s"
    params: tuple[Any, ...] = (causes,)
    if epoch is not None:
        sql += ", pause_epoch = %s"
        params += (epoch,)
    sql += " WHERE workspace_id = %s"
    params += (workspace_id,)
    store._run_transaction(lambda conn: conn.execute(sql, params))


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("byok", [False, True])
def test_legacy_pause_rejects_atomically_and_terminal_key_survives_unpause(
    backend: str, byok: bool
) -> None:
    from trusted_router.storage_legacy_trust import BillingPausedError
    from trusted_router.types import UsageType

    store = _legacy_store(backend)
    ws = store.create_workspace("owner", "legacy", trial_credit_microdollars=1_000_000)
    _raw, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1_000_000
    )
    usage = UsageType.BYOK if byok else UsageType.CREDITS
    store.reserve_key_limit(key.hash, 100, usage_type=usage)
    reservation = (
        None if byok else store.reserve(ws.id, key.hash, 100, idempotency_key="paused-request")
    )
    _set_pause(store, backend, ws.id, '["abuse"]', epoch=1)
    args = dict(
        workspace_id=ws.id,
        key_hash=key.hash,
        model_id="m",
        provider="p",
        usage_type=usage,
        estimated_microdollars=100,
        credit_reservation_id=reservation.id if reservation else None,
        idempotency_key="paused-request",
    )
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(**args)
    with pytest.raises(BillingPausedError):
        store.get_gateway_authorization_by_idempotency_key(ws.id, key.hash, "paused-request")
    if backend == "memory":
        assert store.credit_money[ws.id].reserved_microdollars == 0
        assert store.api_keys.keys[key.hash].reserved_microdollars == 0
    else:

        def read(conn: Any) -> tuple[int, int]:
            return (
                conn.execute(
                    "SELECT reserved FROM tr_credit_balance WHERE workspace_id = %s", (ws.id,)
                ).fetchone()[0],
                conn.execute(
                    "SELECT reserved FROM tr_key_limit WHERE key_hash = %s", (key.hash,)
                ).fetchone()[0],
            )

        assert store._run_transaction(read) == (0, 0)
    # The paused request's key is terminal: clearing the pause never revives it.
    _set_pause(store, backend, ws.id, "[]")
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(**args)


def test_typed_authorize_reads_the_pause_in_its_transaction_and_takes_no_holds() -> None:
    from trusted_router.storage_gcp_authorize import AuthorizeOutcome

    store, db = make_fake_store(request_record_write_mode="typed")
    store.trust_settings = ARMED
    ws = store.create_workspace("owner", "typed-pause", trial_credit_microdollars=1_000)
    _raw, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1_000
    )
    for (workspace_id, _shard), row in db.typed["tr_credit_balance"].items():
        if workspace_id == ws.id:
            row["billing_pause_causes"] = ["abuse"]
            row["pause_epoch"] = 1
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=ws.id,
        key_hash=key.hash,
        estimate=100,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="m",
        provider="p",
        requested_model_id="m",
        candidate_model_ids=["m"],
        region=None,
        endpoint_id=None,
        candidate_endpoint_ids=[],
        idempotency_key="paused",
        idempotency_fingerprint="f" * 64,
    )
    assert outcome == "billing_paused" and outcome != AuthorizeOutcome.ACCEPTED
    assert authorization is None
    assert all(
        row["reserved"] == 0
        for (workspace_id, _shard), row in db.typed["tr_credit_balance"].items()
        if workspace_id == ws.id
    )
    assert all(
        row["reserved"] == 0
        for (key_hash, _shard), row in db.typed["tr_key_limit"].items()
        if key_hash == key.hash
    )
    assert not db.reservations


def test_legacy_spanner_byok_rechecks_pause_inside_creation() -> None:
    from trusted_router.storage_legacy_trust import BillingPausedError
    from trusted_router.types import UsageType

    store, db = make_fake_store(request_record_write_mode="legacy")
    store.trust_settings = ARMED
    ws = store.create_workspace("owner", "legacy-gcp")
    _raw, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000
    )
    store.reserve_key_limit(key.hash, 100, usage_type=UsageType.BYOK)
    for (workspace_id, _), row in db.typed["tr_credit_balance"].items():
        if workspace_id == ws.id:
            row["billing_pause_causes"] = ["abuse"]
            row["pause_epoch"] = 1
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(
            workspace_id=ws.id,
            key_hash=key.hash,
            model_id="m",
            provider="p",
            usage_type=UsageType.BYOK,
            estimated_microdollars=100,
            credit_reservation_id=None,
            idempotency_key="paused",
        )
    assert not any(kind == "gateway_authorization" for kind, _ in db.rows)
    assert store.api_keys.get_by_hash(key.hash).reserved_microdollars == 0


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_legacy_release_recovers_principal_before_unpause(backend: str) -> None:
    from trusted_router.storage_legacy_trust import BillingPausedError
    from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance
    from trusted_router.types import UsageType

    store = _legacy_store(backend)
    ws = store.create_workspace("owner", "debt-release", trial_credit_microdollars=0)
    now = datetime.now(UTC)
    store.credit_workspace_typed_direct(
        ws.id,
        100,
        "payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_legacy", now),
        payment_amount_microdollars=100,
        currency="USD",
    )
    _raw, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000
    )
    store.reserve_key_limit(key.hash, 100, usage_type=UsageType.CREDITS)
    reservation = store.reserve(ws.id, key.hash, 100, idempotency_key="debt-request")
    store.record_adverse_trust_event(
        AdverseTrustEvent(
            event_id="refund",
            provider="stripe",
            kind="refund",
            adverse_ref="re_legacy",
            original_payment_ref="pi_legacy",
            amount_micro=100,
            provider_subtype="refund",
            lifecycle_status="succeeded",
            occurred_at=now,
            provider_ordering_watermark="1",
            payload="{}",
        )
    )
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(
            workspace_id=ws.id,
            key_hash=key.hash,
            model_id="m",
            provider="p",
            usage_type=UsageType.CREDITS,
            estimated_microdollars=100,
            credit_reservation_id=reservation.id,
            idempotency_key="debt-request",
        )
    if backend == "memory":
        payment = store.trust_events[(ws.id, "payment")]
        assert (payment.recovery_target, payment.recovered_micro, payment.unrecovered_micro) == (
            100,
            100,
            0,
        )
        assert store.credit_money[ws.id].total_credits_microdollars == 0
    else:

        def read(conn: Any) -> Any:
            return conn.execute(
                "SELECT recovery_target, recovered_micro, unrecovered_micro FROM tr_trust_event "
                "WHERE workspace_id = %s AND kind = 'payment'",
                (ws.id,),
            ).fetchone()

        assert store._run_transaction(read) == (100, 100, 0)
    assert not store.get_workspace(ws.id).billing_paused


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("reserved_first", [False, True])
def test_pause_at_reservation_is_terminal_and_cannot_settle_released_hold(
    backend: str, reserved_first: bool
) -> None:
    from trusted_router.storage_legacy_trust import BillingPausedError
    from trusted_router.types import UsageType

    store = _legacy_store(backend)
    ws = store.create_workspace("owner", "reserve-race", trial_credit_microdollars=1000)
    _raw, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000
    )
    store.reserve_key_limit(key.hash, 100, usage_type=UsageType.CREDITS)
    old = store.reserve(ws.id, key.hash, 100, idempotency_key="paused") if reserved_first else None
    if backend == "memory":
        store.credit_trust_shards[(ws.id, 0)]["billing_pause_causes"] = ["abuse"]
    else:
        _set_pause(store, backend, ws.id, '["abuse"]')
    with pytest.raises(BillingPausedError):
        store.reserve(ws.id, key.hash, 100, idempotency_key="paused")
    if backend == "memory":
        store.credit_trust_shards[(ws.id, 0)]["billing_pause_causes"] = []
    else:
        _set_pause(store, backend, ws.id, "[]")
    with pytest.raises(BillingPausedError):
        store.get_gateway_authorization_by_idempotency_key(ws.id, key.hash, "paused")
    with pytest.raises(BillingPausedError):
        store.reserve(ws.id, key.hash, 100, idempotency_key="paused")
    if old is not None:
        store.settle(old.id, 999)
    if backend == "postgres":
        assert store.typed_credit_snapshot(ws.id) == (1000, 0, 0)
    else:
        assert store.credit_money[ws.id].total_usage_microdollars == 0
        assert store.credit_money[ws.id].reserved_microdollars == 0


@pytest.mark.parametrize(
    "backend,byok",
    [
        ("memory", False),
        ("memory", True),
        ("postgres", False),
        ("postgres", True),
        ("spanner", True),
    ],
)
def test_legacy_pause_cleared_between_reserve_and_create_still_refuses(
    backend: str, byok: bool
) -> None:
    from trusted_router.storage_legacy_trust import BillingPausedError, legacy_pause_epoch
    from trusted_router.types import UsageType

    db = None
    if backend == "spanner":
        store, db = make_fake_store(request_record_write_mode="legacy")
        store.trust_settings = ARMED
    else:
        store = _legacy_store(backend)
    ws = store.create_workspace("owner", "epoch-race", trial_credit_microdollars=1000)
    _raw, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000
    )
    usage = UsageType.BYOK if byok else UsageType.CREDITS
    epoch = legacy_pause_epoch(store, ws.id)
    store.reserve_key_limit(key.hash, 100, usage_type=usage)
    reservation = None if byok else store.reserve(ws.id, key.hash, 100, idempotency_key="epoch")
    if backend == "memory":
        store.credit_trust_shards[(ws.id, 0)]["pause_epoch"] = epoch + 2
    elif backend == "postgres":
        store._run_transaction(
            lambda conn: conn.execute(
                "UPDATE tr_credit_balance SET pause_epoch = pause_epoch + 2 WHERE workspace_id = %s",
                (ws.id,),
            )
        )
    else:
        assert db is not None
        for (workspace_id, _), row in db.typed["tr_credit_balance"].items():
            if workspace_id == ws.id:
                row["pause_epoch"] = epoch + 2
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(
            workspace_id=ws.id,
            key_hash=key.hash,
            model_id="m",
            provider="p",
            usage_type=usage,
            estimated_microdollars=100,
            credit_reservation_id=reservation.id if reservation else None,
            idempotency_key="epoch",
            expected_pause_epoch=epoch,
        )
    with pytest.raises(BillingPausedError):
        store.get_gateway_authorization_by_idempotency_key(ws.id, key.hash, "epoch")


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_legacy_partial_release_allocates_recovery_in_payment_order(backend: str) -> None:
    from trusted_router.storage_legacy_trust import BillingPausedError
    from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance
    from trusted_router.types import UsageType

    store = _legacy_store(backend)
    ws = store.create_workspace("owner", "partial-release", trial_credit_microdollars=0)
    now = datetime.now(UTC)
    # Insertion order deliberately differs from the canonical payment order.
    for name, age in [("second", 1), ("first", 2)]:
        store.credit_workspace_typed_direct(
            ws.id,
            50,
            name,
            provenance=CreditProvenance(
                "checkout", "stripe", f"pi_{name}", now - timedelta(days=age)
            ),
            payment_amount_microdollars=50,
            currency="USD",
        )
    _raw, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000
    )
    reservations = []
    for amount in [40, 60]:
        store.reserve_key_limit(key.hash, amount, usage_type=UsageType.CREDITS)
        reservations.append(
            store.reserve(ws.id, key.hash, amount, idempotency_key=f"release-{amount}")
        )
    for name, target in [("second", 40), ("first", 30)]:
        store.record_adverse_trust_event(
            AdverseTrustEvent(
                event_id=f"refund-{name}",
                provider="stripe",
                kind="refund",
                adverse_ref=f"re_{name}",
                original_payment_ref=f"pi_{name}",
                amount_micro=target,
                provider_subtype="refund",
                lifecycle_status="succeeded",
                occurred_at=now,
                provider_ordering_watermark="1",
                payload="{}",
            )
        )

    def facts() -> dict[str, tuple[int, int, int]]:
        if backend == "memory":
            return {
                name: (
                    int(store.trust_events[(ws.id, name)].recovery_target),
                    int(store.trust_events[(ws.id, name)].recovered_micro),
                    int(store.trust_events[(ws.id, name)].unrecovered_micro),
                )
                for name in ["first", "second"]
            }
        rows = store._run_transaction(
            lambda conn: conn.execute(
                "SELECT event_id, recovery_target, recovered_micro, unrecovered_micro FROM tr_trust_event "
                "WHERE workspace_id = %s AND kind = 'payment'",
                (ws.id,),
            ).fetchall()
        )
        return {name: (target, recovered, debt) for name, target, recovered, debt in rows}

    for index, reservation in enumerate(reservations):
        with pytest.raises(BillingPausedError):
            store.create_gateway_authorization(
                workspace_id=ws.id,
                key_hash=key.hash,
                model_id="m",
                provider="p",
                usage_type=UsageType.CREDITS,
                estimated_microdollars=reservation.amount_microdollars,
                credit_reservation_id=reservation.id,
                idempotency_key=reservation.idempotency_key,
            )
        expected = {"first": (30, 30, 0), "second": (40, 10, 30) if index == 0 else (40, 40, 0)}
        assert facts() == expected
        assert store.get_workspace(ws.id).billing_paused is (index == 0)
