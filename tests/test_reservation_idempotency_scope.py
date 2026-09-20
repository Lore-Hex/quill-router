"""Reservation retries preserve caller ownership on both legacy billing stores."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from trusted_router.config import Settings
from trusted_router.routes.internal.gateway import _authorize_gateway_sync_impl
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_legacy_trust import BillingPausedError, reject_postgres_reservation
from trusted_router.storage_models import ApiKey, Reservation
from trusted_router.storage_postgres import (
    _RESERVATION_IDEMPOTENCY_KIND,
    _RESERVATION_KIND,
    _gateway_idempotency_id,
    _reservation_idempotency_id,
)
from trusted_router.typed_balance import LiveCreditSummary, live_credit_summary


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Iterator[Any]:
    backend = InMemoryStore() if request.param == "memory" else postgres_store_on(sqlite_postgres_conn())
    backend.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=False)
    configure_store(backend)
    try:
        yield backend
    finally:
        configure_store(InMemoryStore())


@pytest.fixture
def pg_store() -> Iterator[Any]:
    backend = postgres_store_on(sqlite_postgres_conn())
    backend.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=False)
    configure_store(backend)
    try:
        yield backend
    finally:
        configure_store(InMemoryStore())


def _key(store: Any, name: str, credits: int = 5_000_000, *, workspace_id: str | None = None) -> ApiKey:
    if workspace_id is None:
        workspace_id = store.create_workspace(name, name, trial_credit_microdollars=credits).id
    return store.create_api_key(workspace_id=workspace_id, name=name, creator_user_id=name)[1]


def _balance(store: Any, key: ApiKey) -> LiveCreditSummary:
    result = live_credit_summary(key.workspace_id, store=store)
    assert result is not None
    return result


def _authorize(store: Any, key: ApiKey, idem: str = "shared", tokens: int = 100) -> Any:
    response = _authorize_gateway_sync_impl(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        GatewayAuthorizeRequest(
            api_key_hash=key.hash, idempotency_key=idem,
            model="anthropic/claude-haiku-4.5",
            estimated_input_tokens=tokens, max_output_tokens=tokens,
        ),
        Settings(environment="test", internal_gateway_token=None),
        {"boot_auth": None, "boot_verified": False},
    )
    result = store.get_gateway_authorization(response["data"]["authorization_id"])
    assert result is not None and result.credit_reservation_id is not None
    return result


def _reserve(store: Any, key: ApiKey, amount: int = 100, idem: str | None = "shared") -> Reservation:
    return store.reserve(key.workspace_id, key.hash, amount, idempotency_key=idem)


def _pointer(store: Any, index_id: str) -> dict[str, Any] | None:
    return store._run_transaction(
        lambda conn: store._read_entity_tx(conn, _RESERVATION_IDEMPOTENCY_KIND, index_id, dict)
    )


def _write_pointer(store: Any, index_id: str, value: dict[str, Any]) -> None:
    store._run_transaction(
        lambda conn: store._write_entity_tx(conn, _RESERVATION_IDEMPOTENCY_KIND, index_id, value)
    )


def _reservation(store: Any, reservation_id: str) -> Reservation:
    if isinstance(store, InMemoryStore):
        return store.api_keys.reservations[reservation_id]
    result = store._run_transaction(
        lambda conn: store._read_entity_tx(conn, _RESERVATION_KIND, reservation_id, Reservation)
    )
    assert result is not None
    return result


def _legacy_reservation(store: Any, key: ApiKey) -> Reservation:
    original = _reserve(store, key, idem=None)
    original.idempotency_key = "shared"
    store._run_transaction(
        lambda conn: store._write_entity_tx(conn, _RESERVATION_KIND, original.id, original)
    )
    _write_pointer(store, "shared", asdict(original))
    return original


def _pause(store: Any, key: ApiKey, paused: bool = True) -> None:
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=True)
    if isinstance(store, InMemoryStore):
        store.workspaces[key.workspace_id].billing_pause_causes = ["abuse"] if paused else []
    else:
        store._run_transaction(lambda conn: conn.execute(
            "UPDATE tr_credit_balance SET billing_pause_causes = %s WHERE workspace_id = %s",
            ('["abuse"]' if paused else "[]", key.workspace_id),
        ))


def test_gateway_reservation_cannot_use_another_workspaces_credit(store: Any) -> None:
    a, b = _key(store, "a"), _key(store, "b", 0)
    auth = _authorize(store, a)
    before = _balance(store, a)
    for idem in ("shared", "fresh"):
        with pytest.raises(HTTPException) as exc:
            _authorize(store, b, idem)
        assert exc.value.status_code == 402
        assert store.get_gateway_authorization_by_idempotency_key(b.workspace_id, b.hash, idem) is None
    assert _balance(store, a) == before
    assert before["reserved"] == auth.estimated_microdollars > 0
    assert _balance(store, b)["reserved"] == 0
    if isinstance(store, InMemoryStore):
        assert list(store.api_keys.gateway_authorizations) == [auth.id]
    else:
        rows = store._run_transaction(lambda conn: conn.execute(
            "SELECT id FROM tr_entities WHERE kind = %s", ("gateway_authorization",),
        ).fetchall())
        assert rows == [(auth.id,)]


def test_gateway_reservations_settle_only_their_own_workspace(store: Any) -> None:
    a, b = _key(store, "a"), _key(store, "b")
    auth_a, auth_b = _authorize(store, a), _authorize(store, b, tokens=200)
    assert auth_a.credit_reservation_id != auth_b.credit_reservation_id
    assert auth_a.estimated_microdollars != auth_b.estimated_microdollars
    for key, auth in ((a, auth_a), (b, auth_b)):
        assert _balance(store, key)["reserved"] == auth.estimated_microdollars
        reservation = _reservation(store, auth.credit_reservation_id)
        assert (reservation.workspace_id, reservation.key_hash) == (key.workspace_id, key.hash)
        assert _reserve(store, key).id == reservation.id
    for key, auth, actual in ((a, auth_a, 31), (b, auth_b, 73)):
        assert store.finalize_gateway_authorization(
            auth.id, success=True, actual_microdollars=actual, selected_usage_type="Credits",
        )
        assert _balance(store, key)["total_usage"] == actual
        assert _balance(store, key)["reserved"] == 0


@pytest.mark.parametrize("idem", ["shared", "", "a#b:c\x00d"])
def test_reservation_scope_includes_api_key_and_retries_hold_once(store: Any, idem: str) -> None:
    a = _key(store, "a")
    b = _key(store, "b", workspace_id=a.workspace_id)
    first, second = _reserve(store, a, 100, idem), _reserve(store, b, 200, idem)
    assert first.id != second.id
    assert _reserve(store, a, 999, idem).id == first.id
    assert _reserve(store, b, 999, idem).id == second.id
    assert _balance(store, a)["reserved"] == 300


def test_reservation_without_idempotency_key_holds_each_request(store: Any) -> None:
    key = _key(store, "a")
    first, second = _reserve(store, key, idem=None), _reserve(store, key, idem=None)
    assert first.id != second.id
    assert _balance(store, key)["reserved"] == 200


@pytest.mark.parametrize("paused", [False, True])
def test_postgres_legacy_owned_reservation_is_adopted(pg_store: Any, paused: bool) -> None:
    key = _key(pg_store, "a")
    original = _legacy_reservation(pg_store, key)
    legacy = asdict(original)
    if paused:
        _pause(pg_store, key)
        with pytest.raises(BillingPausedError):
            _reserve(pg_store, key)
        assert _balance(pg_store, key)["reserved"] == 0
        _pause(pg_store, key, False)
        with pytest.raises(BillingPausedError):
            _reserve(pg_store, key)
    else:
        assert _reserve(pg_store, key, 999).id == original.id
        assert _reserve(pg_store, key, 999).id == original.id
        assert _balance(pg_store, key)["reserved"] == 100
    scoped = _pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared"))
    assert scoped is not None
    if paused:
        assert scoped.get("reason") == "billing_paused"
    else:
        assert scoped["id"] == original.id
    assert _pointer(pg_store, "shared") == legacy


@pytest.mark.parametrize("owner", ["workspace", "key", "missing"])
@pytest.mark.parametrize("credits", [0, 5_000_000])
def test_postgres_foreign_legacy_reservation_is_ignored(pg_store: Any, owner: str, credits: int) -> None:
    a = _key(pg_store, "a")
    b = _key(pg_store, "b", credits, workspace_id=a.workspace_id if owner == "key" else None)
    original = _legacy_reservation(pg_store, a)
    legacy = asdict(original)
    if owner == "missing":
        legacy.update(id="missing", workspace_id=b.workspace_id, key_hash=b.hash)
    _write_pointer(pg_store, "shared", legacy)
    before = _balance(pg_store, a)
    if not credits and owner != "key":
        with pytest.raises(HTTPException) as exc:
            _authorize(pg_store, b)
        assert exc.value.status_code == 402
        assert pg_store.get_gateway_authorization_by_idempotency_key(b.workspace_id, b.hash, "shared") is None
    else:
        own = _authorize(pg_store, b)
        assert own.credit_reservation_id != original.id
        assert _reserve(pg_store, b).id == own.credit_reservation_id
    if owner != "key":
        assert _balance(pg_store, a) == before
    assert asdict(_reservation(pg_store, original.id)) == asdict(original)
    assert _pointer(pg_store, "shared") == legacy


def test_postgres_scoped_reservation_takes_precedence_over_legacy(pg_store: Any) -> None:
    key = _key(pg_store, "a")
    legacy = _legacy_reservation(pg_store, key)
    scoped = _reserve(pg_store, key, idem=None)
    _write_pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared"), asdict(scoped))
    assert _reserve(pg_store, key).id == scoped.id
    assert _pointer(pg_store, "shared") == asdict(legacy)
    assert _balance(pg_store, key)["reserved"] == 200


@pytest.mark.parametrize("owned", [False, True])
def test_postgres_legacy_terminal_checks_referenced_reservation(pg_store: Any, owned: bool) -> None:
    a = _key(pg_store, "a")
    b = a if owned else _key(pg_store, "b")
    original = _legacy_reservation(pg_store, a)
    pg_store.refund(original.id)
    terminal = {"reason": "billing_paused", "reservation_id": original.id}
    _write_pointer(pg_store, "shared", terminal)
    _pause(pg_store, b, False)
    if owned:
        with pytest.raises(BillingPausedError):
            _reserve(pg_store, b)
        assert _balance(pg_store, b)["reserved"] == 0
    else:
        assert _reserve(pg_store, b).id != original.id
        assert _balance(pg_store, b)["reserved"] == 100
    assert _pointer(pg_store, "shared") == terminal


@pytest.mark.parametrize("owner", ["workspace", "key"])
@pytest.mark.parametrize("paused", [False, True])
def test_reservation_scoped_pointer_checks_ownership(store: Any, owner: str, paused: bool) -> None:
    a = _key(store, "a")
    b = _key(store, "b", workspace_id=a.workspace_id if owner == "key" else None)
    original = _reserve(store, a)
    if isinstance(store, InMemoryStore):
        store.api_keys.reservation_id_by_idempotency_key[(b.workspace_id, b.hash, "shared")] = original.id
    else:
        _write_pointer(store, _reservation_idempotency_id(b.workspace_id, b.hash, "shared"), asdict(original))
    before = _balance(store, a)
    if paused:
        _pause(store, b)
        with pytest.raises(BillingPausedError):
            _reserve(store, b)
        assert _balance(store, a) == before
    else:
        own = _reserve(store, b)
        assert own.id != original.id
        assert (own.workspace_id, own.key_hash) == (b.workspace_id, b.hash)
        assert _reserve(store, b).id == own.id
    assert not _reservation(store, original.id).settled


def test_paused_reservation_cannot_refund_another_workspace(store: Any) -> None:
    a, b = _key(store, "a"), _key(store, "b")
    original = _reserve(store, a)
    before = _balance(store, a)
    _pause(store, b)
    with pytest.raises(BillingPausedError):
        _reserve(store, b)
    _pause(store, b, False)
    with pytest.raises(BillingPausedError):
        _reserve(store, b)
    assert _balance(store, a) == before
    assert not _reservation(store, original.id).settled
    assert _reserve(store, a).id == original.id


def test_paused_reservation_releases_owned_hold_once(store: Any) -> None:
    key = _key(store, "a")
    _reserve(store, key)
    sibling = _reserve(store, key, 70, "sibling")
    _pause(store, key)
    with pytest.raises(BillingPausedError):
        _reserve(store, key, 999)
    assert _balance(store, key)["reserved"] == 70
    _pause(store, key, False)
    with pytest.raises(BillingPausedError):
        _reserve(store, key, 999)
    assert _balance(store, key)["reserved"] == 70
    assert _reserve(store, key, 70, "sibling").id == sibling.id


def test_postgres_paused_reservation_preserves_foreign_legacy_pointer(pg_store: Any) -> None:
    a, b = _key(pg_store, "a"), _key(pg_store, "b")
    original = _legacy_reservation(pg_store, a)
    legacy = asdict(original)
    _write_pointer(pg_store, "shared", legacy)
    _pause(pg_store, b)
    with pytest.raises(BillingPausedError):
        _reserve(pg_store, b)
    assert _balance(pg_store, a)["reserved"] == 100
    assert _reservation(pg_store, original.id) == original
    assert _pointer(pg_store, "shared") == legacy


@pytest.mark.parametrize("idem", ["shared", ""])
def test_postgres_reject_reservation_writes_only_scoped_terminal(pg_store: Any, idem: str) -> None:
    key = _key(pg_store, "a")
    original = _reserve(pg_store, key, idem=idem)
    assert _pointer(pg_store, idem) is None
    pg_store._run_transaction(lambda conn: reject_postgres_reservation(conn, pg_store, original.id))
    assert _pointer(pg_store, idem) is None
    scoped = _pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, idem))
    assert scoped == {"reason": "billing_paused", "reservation_id": original.id}
    assert _balance(pg_store, key)["reserved"] == 0
    _pause(pg_store, key, False)
    with pytest.raises(BillingPausedError):
        _reserve(pg_store, key, idem=idem)


@pytest.mark.parametrize("separator", ["#", ":", "\x00", '\",\"'])
def test_reservation_idempotency_hash_has_distinct_domain_and_field_boundaries(separator: str) -> None:
    values = [
        ("w", "k", f"i{separator}tail"),
        ("w", f"k{separator}i", "tail"),
        (f"w{separator}k", "i", "tail"),
    ]
    ids = [_reservation_idempotency_id(*value) for value in values]
    assert len(set(ids)) == len(values)
    assert set(ids).isdisjoint(_gateway_idempotency_id(*value) for value in values)
    assert ids == [_reservation_idempotency_id(*value) for value in values]
