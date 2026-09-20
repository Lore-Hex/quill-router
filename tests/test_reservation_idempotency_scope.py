"""Reservation retries preserve caller ownership on both legacy billing stores."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Event
from typing import Any
from uuid import uuid4

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
    _GATEWAY_AUTHORIZATION_KIND,
    _GATEWAY_IDEMPOTENCY_KIND,
    _RESERVATION_FINALIZATION_KIND,
    _RESERVATION_IDEMPOTENCY_KIND,
    _RESERVATION_IDEMPOTENCY_SCOPED_KIND,
    _RESERVATION_KIND,
    _gateway_idempotency_id,
    _reservation_idempotency_id,
)
from trusted_router.typed_balance import LiveCreditSummary, live_credit_summary


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Iterator[Any]:
    backend = InMemoryStore() if request.param == "memory" else postgres_store_on(sqlite_postgres_conn(check_same_thread=False))
    backend.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=False)
    configure_store(backend)
    try:
        yield backend
    finally:
        configure_store(InMemoryStore())


@pytest.fixture
def pg_store() -> Iterator[Any]:
    backend = postgres_store_on(sqlite_postgres_conn(check_same_thread=False))
    backend.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=False)
    configure_store(backend)
    try:
        yield backend
    finally:
        configure_store(InMemoryStore())


def _key(
    store: Any, name: str, credits: int = 5_000_000, *,
    workspace_id: str | None = None, capped: bool = False,
) -> ApiKey:
    if workspace_id is None:
        workspace_id = store.create_workspace(name, name, trial_credit_microdollars=credits).id
    return store.create_api_key(
        workspace_id=workspace_id, name=name, creator_user_id=name,
        limit_microdollars=5_000_000 if capped else None,
    )[1]


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


def _pointer(store: Any, index_id: str, *, scoped: bool = False) -> dict[str, Any] | None:
    kind = _RESERVATION_IDEMPOTENCY_SCOPED_KIND if scoped else _RESERVATION_IDEMPOTENCY_KIND
    return store._run_transaction(
        lambda conn: store._read_entity_tx(conn, kind, index_id, dict)
    )


def _write_pointer(store: Any, index_id: str, value: dict[str, Any], *, scoped: bool = False) -> None:
    kind = _RESERVATION_IDEMPOTENCY_SCOPED_KIND if scoped else _RESERVATION_IDEMPOTENCY_KIND
    store._run_transaction(
        lambda conn: store._write_entity_tx(conn, kind, index_id, value)
    )


def _finalized(store: Any, reservation_id: str) -> bool:
    if isinstance(store, InMemoryStore):
        return store.api_keys.reservations[reservation_id].settled
    return store._run_transaction(lambda conn: conn.has_entity(_RESERVATION_FINALIZATION_KIND, reservation_id))


def _key_reserved(store: Any, key: ApiKey) -> int:
    if isinstance(store, InMemoryStore):
        return store.get_key_by_hash(key.hash).reserved_microdollars
    return store._run_transaction(lambda conn: conn.execute(
        "SELECT reserved FROM tr_key_limit WHERE key_hash = %s AND shard = 0", (key.hash,),
    ).fetchone()[0])


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


def _legacy_row(store: Any, idem: str = "shared") -> Any:
    return store._run_transaction(lambda conn: conn.execute(
        "SELECT body, updated_at FROM tr_entities WHERE kind = %s AND id = %s",
        (_RESERVATION_IDEMPOTENCY_KIND, idem),
    ).fetchone())


def _old_worker_reserve(
    store: Any, workspace_id: str, key_hash: str, amount_microdollars: int, *,
    idempotency_key: str | None = None,
) -> Reservation:
    """OLD worker: unarmed reserve transaction copied from origin/main.

    Keep the bare insert-once and Reservation decoding used by deployed workers.
    These rollout tests exercise the unarmed transaction on the same connection.
    """
    assert not store.trust_settings.spend_lease_trust_eligibility_enabled
    reservation = Reservation(
        id=str(uuid4()), workspace_id=workspace_id, key_hash=key_hash,
        amount_microdollars=int(amount_microdollars), idempotency_key=idempotency_key,
    )

    def reserve_credit(conn: Any) -> Reservation:
        if idempotency_key is not None:
            won = store._insert_entity_once_tx(
                conn, _RESERVATION_IDEMPOTENCY_KIND, idempotency_key, reservation,
            )
            if not won:
                existing = store._read_entity_tx(
                    conn, _RESERVATION_IDEMPOTENCY_KIND, idempotency_key, Reservation,
                )
                if existing is None:
                    raise RuntimeError("reservation idempotency row disappeared after conflict")
                return existing
        inserted = store._insert_entity_once_tx(
            conn, _RESERVATION_KIND, reservation.id, reservation,
        )
        if not inserted:
            raise RuntimeError("reservation id collision")
        cursor = conn.execute(
            "UPDATE tr_credit_balance "
            "SET reserved = reserved + %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE workspace_id = %s AND shard = 0 "
            "AND total_credits - total_usage - reserved >= %s",
            (reservation.amount_microdollars, workspace_id, reservation.amount_microdollars),
        )
        if cursor.rowcount != 1:
            raise ValueError("insufficient credits")
        return reservation

    return store._run_transaction(reserve_credit)


def _pause(store: Any, key: ApiKey, paused: bool = True) -> None:
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=True)
    if isinstance(store, InMemoryStore):
        store.workspaces[key.workspace_id].billing_paused = paused
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
    conn = pg_store._run_transaction(lambda conn: conn)
    conn.statements.clear()
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
    assert all(sql.startswith("SELECT") for sql, params in conn.statements if _RESERVATION_IDEMPOTENCY_KIND in params)
    scoped = _pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared"), scoped=True)
    assert scoped is not None
    if paused:
        assert scoped.get("reason") == "billing_paused"
    else:
        assert scoped["id"] == original.id
    assert _pointer(pg_store, "shared") == legacy
    assert _pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared")) is None


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
    _write_pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared"), asdict(scoped), scoped=True)
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
        _write_pointer(store, _reservation_idempotency_id(b.workspace_id, b.hash, "shared"), asdict(original), scoped=True)
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
    assert not _finalized(store, original.id)
    assert _balance(store, a)["reserved"] == (200 if owner == "key" and not paused else 100)


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
    assert not _finalized(store, original.id)
    assert _balance(store, a)["reserved"] == 100
    assert _reserve(store, a).id == original.id


def test_paused_reservation_releases_owned_hold_once(store: Any) -> None:
    key = _key(store, "a", capped=True)
    original = _reserve(store, key)
    sibling = _reserve(store, key, 70, "sibling")
    assert store.reserve_key_limit(key.hash, 70, usage_type="Credits").reserved_microdollars == 70
    before = _key_reserved(store, key)
    _pause(store, key)
    assert store.reserve_key_limit(key.hash, 999, usage_type="Credits").reserved_microdollars == 999
    with pytest.raises(BillingPausedError):
        _reserve(store, key, 999)
    assert _key_reserved(store, key) == before
    assert _balance(store, key)["reserved"] == 70
    assert _finalized(store, original.id)
    assert not _finalized(store, sibling.id)
    _pause(store, key, False)
    assert store.reserve_key_limit(key.hash, 999, usage_type="Credits").reserved_microdollars == 999
    with pytest.raises(BillingPausedError):
        _reserve(store, key, 999)
    assert _key_reserved(store, key) == before
    assert _balance(store, key)["reserved"] == 70
    assert _reserve(store, key, 70, "sibling").id == sibling.id


@pytest.mark.parametrize("new_first", [False, True])
@pytest.mark.parametrize("idem", ["shared", ""])
def test_postgres_mixed_version_reservation_holds_once(pg_store: Any, new_first: bool, idem: str) -> None:
    key = _key(pg_store, "a")
    calls = [pg_store.reserve, lambda *args, **kwargs: _old_worker_reserve(pg_store, *args, **kwargs)]
    if not new_first:
        calls.reverse()
    first = calls[0](key.workspace_id, key.hash, 100, idempotency_key=idem)
    second = calls[1](key.workspace_id, key.hash, 100, idempotency_key=idem)
    assert first.id == second.id
    assert _balance(pg_store, key)["reserved"] == 100
    assert _pointer(pg_store, idem) == asdict(first)
    assert _pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, idem), scoped=True) == asdict(first)


def test_postgres_mixed_version_gateway_interleaving_settles_one_hold(
    pg_store: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _key(pg_store, "a")
    _reserve(pg_store, key, 70, "sibling")
    before = _balance(pg_store, key)["reserved"]
    new_reserved, old_authorized = Event(), Event()
    new_reserve = type(pg_store).reserve
    reservations: list[Reservation] = []

    def mixed_reserve(self: Any, *args: Any, **kwargs: Any) -> Reservation:
        if new_reserved.is_set():
            return _old_worker_reserve(self, *args, **kwargs)
        result = new_reserve(self, *args, **kwargs)
        reservations.append(result)
        new_reserved.set()
        assert old_authorized.wait(15), "old worker did not finish authorization"
        return result

    monkeypatch.setattr(type(pg_store), "reserve", mixed_reserve)
    # Requests overlap, but the old worker runs after the new reserve commits.
    # Events keep the harness's single connection out of simultaneous transactions.
    with ThreadPoolExecutor(max_workers=1) as executor:
        new_attempt = executor.submit(_authorize, pg_store, key)
        try:
            assert new_reserved.wait(15), "new worker did not commit its reservation"
            old_result = _authorize(pg_store, key)
        finally:
            old_authorized.set()
        new_result = new_attempt.result(timeout=15)
    assert new_result.id == old_result.id
    assert new_result.credit_reservation_id == reservations[0].id
    assert pg_store._run_transaction(lambda conn: conn.count_entities(_GATEWAY_AUTHORIZATION_KIND)) == 1
    assert _balance(pg_store, key)["reserved"] == before + new_result.estimated_microdollars
    assert pg_store.finalize_gateway_authorization(
        new_result.id, success=True, actual_microdollars=31, selected_usage_type="Credits",
    )
    assert _balance(pg_store, key)["reserved"] == before
    assert _finalized(pg_store, reservations[0].id)


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("owner", ["own", "foreign"])
def test_postgres_fresh_takeover_preserves_existing_legacy_row(pg_store: Any, terminal: bool, owner: str) -> None:
    key = _key(pg_store, "a")
    legacy_key = key if owner == "own" else _key(pg_store, "b")
    original = _legacy_reservation(pg_store, legacy_key)
    before = _legacy_row(pg_store)
    index_id = _reservation_idempotency_id(key.workspace_id, key.hash, "shared")
    _write_pointer(pg_store, index_id, {"reason": "billing_paused"} if terminal else {"id": "missing"}, scoped=True)
    fresh = _reserve(pg_store, key)
    assert fresh.id != original.id
    assert _pointer(pg_store, index_id, scoped=True) == asdict(fresh)
    assert _legacy_row(pg_store) == before
    assert _balance(pg_store, key)["reserved"] == (200 if owner == "own" else 100)


@pytest.mark.parametrize("state", ["fresh", "terminal", "missing"])
def test_postgres_insufficient_credit_rolls_back_both_pointers(pg_store: Any, state: str) -> None:
    key = _key(pg_store, "a", 0)
    if state == "terminal":
        _pause(pg_store, key)
        with pytest.raises(BillingPausedError):
            _reserve(pg_store, key)
        pg_store.trust_settings.spend_lease_trust_eligibility_enabled = False
    elif state == "missing":
        _write_pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared"),
                       {"id": "missing"}, scoped=True)
    before = _billing_state(pg_store)
    with pytest.raises(ValueError, match="^insufficient credits$"):
        _reserve(pg_store, key)
    assert _billing_state(pg_store) == before
    assert _pointer(pg_store, "shared") is None
    assert _balance(pg_store, key)["reserved"] == 0


def _assert_pause_tombstones(store: Any, key: ApiKey, idem: str) -> None:
    if isinstance(store, InMemoryStore):
        assert (key.workspace_id, key.hash, idem) in store._paused_authorizations
    else:
        pointer = _pointer(store, _reservation_idempotency_id(key.workspace_id, key.hash, idem), scoped=True)
        assert pointer is not None and pointer["reason"] == "billing_paused"
        gateway = store._run_transaction(lambda conn: store._read_entity_tx(
            conn, _GATEWAY_IDEMPOTENCY_KIND, _gateway_idempotency_id(key.workspace_id, key.hash, idem), dict,
        ))
        assert gateway == {"reason": "billing_paused"}


@pytest.mark.parametrize("gateway_retry", [False, True])
@pytest.mark.parametrize("with_reservation", [False, True])
def test_disarmed_takeover_rearms_before_authorization(
    store: Any, gateway_retry: bool, with_reservation: bool,
) -> None:
    key = _key(store, "a")
    _pause(store, key, False)
    first = _reserve(store, key, idem="auth") if with_reservation else None
    _pause(store, key)
    with pytest.raises(BillingPausedError):
        if first is not None:
            _create_authorization(store, key, first.id)
        else:
            _reserve(store, key, idem="auth")
    _assert_pause_tombstones(store, key, "auth")
    assert _balance(store, key)["reserved"] == 0
    if first is not None:
        assert _finalized(store, first.id)
    store.trust_settings.spend_lease_trust_eligibility_enabled = False
    second = _reserve(store, key, idem="auth")
    assert first is None or first.id != second.id
    assert _balance(store, key)["reserved"] == 100
    assert store.get_gateway_authorization_by_idempotency_key(key.workspace_id, key.hash, "auth") is None
    _pause(store, key, False)
    auth = _authorize(store, key, "auth") if gateway_retry else _create_authorization(store, key, second.id)
    assert auth.credit_reservation_id == second.id
    assert _balance(store, key)["reserved"] == 100
    assert not _finalized(store, second.id)
    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=31, selected_usage_type="Credits",
    )
    assert _balance(store, key)["reserved"] == 0
    assert _finalized(store, second.id)


@pytest.mark.parametrize("with_reason", [False, True])
def test_terminal_takeover_preserves_gateway_authorization_pointer(store: Any, with_reason: bool) -> None:
    key = _key(store, "a")
    first = _reserve(store, key, idem="auth")
    auth = _create_authorization(store, key, first.id)
    if isinstance(store, InMemoryStore):
        store._paused_authorizations.add((key.workspace_id, key.hash, "auth"))
        before = dict(store.api_keys.gateway_authorization_id_by_idempotency_key)
    else:
        _write_pointer(store, _reservation_idempotency_id(key.workspace_id, key.hash, "auth"),
                       {"reason": "billing_paused"}, scoped=True)
        gateway_id = _gateway_idempotency_id(key.workspace_id, key.hash, "auth")
        pointer = {"authorization_id": auth.id}
        if with_reason:
            pointer["reason"] = "billing_paused"
        store._run_transaction(lambda conn: store._write_entity_tx(conn, _GATEWAY_IDEMPOTENCY_KIND, gateway_id, pointer))
        before = store._run_transaction(lambda conn: conn.execute(
            "SELECT body, updated_at FROM tr_entities WHERE kind = %s AND id = %s",
            (_GATEWAY_IDEMPOTENCY_KIND, gateway_id),
        ).fetchone())
    second = _reserve(store, key, idem="auth")
    assert second.id != first.id
    if isinstance(store, InMemoryStore):
        assert store.api_keys.gateway_authorization_id_by_idempotency_key == before
    else:
        assert store._run_transaction(lambda conn: conn.execute(
            "SELECT body, updated_at FROM tr_entities WHERE kind = %s AND id = %s",
            (_GATEWAY_IDEMPOTENCY_KIND, gateway_id),
        ).fetchone()) == before
    assert _create_authorization(store, key, second.id).id == auth.id
    assert _balance(store, key)["reserved"] == 200


@pytest.mark.parametrize("with_reservation", [False, True])
def test_failed_disarmed_takeover_preserves_terminal_decision(store: Any, with_reservation: bool) -> None:
    key = _key(store, "a", 170)
    sibling = _reserve(store, key, 70, "sibling")
    first = _reserve(store, key, idem="auth") if with_reservation else None
    _pause(store, key)
    with pytest.raises(BillingPausedError):
        if first is not None:
            _create_authorization(store, key, first.id)
        else:
            _reserve(store, key, idem="auth")
    _assert_pause_tombstones(store, key, "auth")
    assert _balance(store, key)["reserved"] == 70
    store.trust_settings.spend_lease_trust_eligibility_enabled = False
    before = _billing_state(store)
    with pytest.raises(ValueError, match="^insufficient credits$"):
        _reserve(store, key, 200, "auth")
    assert _billing_state(store) == before
    _pause(store, key, False)
    with pytest.raises(BillingPausedError):
        _reserve(store, key, 100, "auth")
    _assert_pause_tombstones(store, key, "auth")
    assert _balance(store, key)["reserved"] == 70
    assert not _finalized(store, sibling.id)
    if first is not None:
        assert _finalized(store, first.id)


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
    legacy = _legacy_row(pg_store, idem)
    assert _pointer(pg_store, idem) == asdict(original)
    pg_store._run_transaction(lambda conn: reject_postgres_reservation(conn, pg_store, original.id))
    assert _legacy_row(pg_store, idem) == legacy
    scoped = _pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, idem), scoped=True)
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


@pytest.mark.parametrize(("fields", "expected"), [
    (("w", "k", "i"), "residem_89e3af4800f55ee298233f00f2399ebe7c340ceea6a2458412527ba0d49cb1bd"),
    (("workspace", "key", ""), "residem_119c26e1689a37e9d434f841459362c56120c7bdaf20ed2525de06f67fda5a2d"),
    (("w#x", "k\x00z", "a:b"), "residem_9e8031c5d741912d2bb69735a71384a173f82ea027d765aaff46a5be733374e2"),
])
def test_reservation_idempotency_hash_fixed_vectors(fields: tuple[str, str, str], expected: str) -> None:
    assert _reservation_idempotency_id(*fields) == expected


@pytest.mark.parametrize("exhausted", [False, True])
def test_gateway_client_key_and_scoped_pointer_have_separate_namespaces(store: Any, exhausted: bool) -> None:
    key = _key(store, "a")
    first = _authorize(store, key)
    original = asdict(_reservation(store, first.credit_reservation_id))
    if exhausted:
        balance = _balance(store, key)
        _reserve(store, key, balance["total_credits"] - balance["reserved"], "remainder")
    before = _balance(store, key)
    idem = _reservation_idempotency_id(key.workspace_id, key.hash, "shared")
    if exhausted:
        with pytest.raises(HTTPException) as exc:
            _authorize(store, key, idem)
        assert exc.value.status_code == 402
        assert store.get_gateway_authorization_by_idempotency_key(key.workspace_id, key.hash, idem) is None
        assert _balance(store, key) == before
        if not isinstance(store, InMemoryStore):
            assert _pointer(store, _reservation_idempotency_id(key.workspace_id, key.hash, idem), scoped=True) is None
    else:
        second = _authorize(store, key, idem)
        assert first.id != second.id
        assert first.credit_reservation_id != second.credit_reservation_id
        assert _balance(store, key)["reserved"] == before["reserved"] + second.estimated_microdollars
    assert asdict(_reservation(store, first.credit_reservation_id)) == original
    assert not _finalized(store, first.credit_reservation_id)
    assert _reserve(store, key).id == first.credit_reservation_id


@pytest.mark.parametrize("legacy_type", ["reservation", "missing", "tombstone"])
def test_postgres_legacy_scoped_string_is_neither_adopted_nor_overwritten(pg_store: Any, legacy_type: str) -> None:
    key = _key(pg_store, "a")
    original = _reserve(pg_store, key, idem=None)
    legacy = asdict(original)
    if legacy_type == "missing":
        legacy["id"] = "missing"
    elif legacy_type == "tombstone":
        legacy = {"reason": "billing_paused"}
    index_id = _reservation_idempotency_id(key.workspace_id, key.hash, "shared")
    _write_pointer(pg_store, index_id, legacy)
    _pause(pg_store, key, False)
    own = _reserve(pg_store, key)
    assert own.id != original.id
    assert _reserve(pg_store, key).id == own.id
    assert _balance(pg_store, key)["reserved"] == 200
    assert not _finalized(pg_store, original.id)
    assert _pointer(pg_store, index_id) == legacy
    assert _pointer(pg_store, index_id, scoped=True) == asdict(own)


def test_disarmed_reservation_takes_over_pause_tombstone_once(store: Any) -> None:
    key = _key(store, "a")
    _pause(store, key)
    with pytest.raises(BillingPausedError):
        _reserve(store, key)
    assert _balance(store, key)["reserved"] == 0
    store.trust_settings.spend_lease_trust_eligibility_enabled = False
    first, second = _reserve(store, key), _reserve(store, key)
    assert first.id == second.id
    assert _balance(store, key)["reserved"] == 100
    if isinstance(store, InMemoryStore):
        assert list(store.api_keys.reservations) == [first.id]
        assert store.api_keys.reservation_id_by_idempotency_key[(key.workspace_id, key.hash, "shared")] == first.id
    else:
        assert store._run_transaction(lambda conn: conn.count_entities(_RESERVATION_KIND)) == 1
        assert _pointer(store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared"), scoped=True) == asdict(first)
    _pause(store, key)
    with pytest.raises(BillingPausedError):
        _reserve(store, key)
    assert _balance(store, key)["reserved"] == 0
    assert _finalized(store, first.id)


@pytest.mark.parametrize("state", ["fresh", "legacy", "paused", "terminal", "foreign"])
def test_postgres_reservation_pause_lock_precedes_all_pointer_access(pg_store: Any, state: str) -> None:
    key = _key(pg_store, "a")
    if state == "legacy":
        _legacy_reservation(pg_store, key)
    if state == "terminal":
        _write_pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared"),
                       {"reason": "billing_paused"}, scoped=True)
    if state == "foreign":
        other = _reserve(pg_store, _key(pg_store, "b"))
        _write_pointer(pg_store, _reservation_idempotency_id(key.workspace_id, key.hash, "shared"),
                       asdict(other), scoped=True)
    _pause(pg_store, key, state == "paused")
    conn = pg_store._run_transaction(lambda conn: conn)
    conn.statements.clear()
    if state in {"paused", "terminal"}:
        with pytest.raises(BillingPausedError):
            _reserve(pg_store, key)
    else:
        _reserve(pg_store, key)
    pause_reads = [i for i, (sql, _) in enumerate(conn.statements)
                   if sql.startswith("SELECT billing_pause_causes, pause_epoch FROM tr_credit_balance")]
    pointer_access = [i for i, (_, params) in enumerate(conn.statements)
                      if _RESERVATION_IDEMPOTENCY_KIND in params or _RESERVATION_IDEMPOTENCY_SCOPED_KIND in params]
    assert len(pause_reads) == 1
    assert pointer_access and all(pause_reads[0] < i for i in pointer_access)
    legacy_writes = [sql for sql, params in conn.statements
                     if _RESERVATION_IDEMPOTENCY_KIND in params and not sql.startswith("SELECT")]
    assert len(legacy_writes) == (1 if state in {"fresh", "foreign"} else 0)
    assert all("ON CONFLICT (kind, id) DO NOTHING" in sql for sql in legacy_writes)


def _billing_state(store: Any) -> Any:
    if isinstance(store, InMemoryStore):
        return deepcopy((
            store.credit_money, store.api_keys.reservations,
            store.api_keys.reservation_id_by_idempotency_key,
            store.api_keys.gateway_authorizations,
            store.api_keys.gateway_authorization_id_by_idempotency_key,
            store.api_keys.deferred_outstanding, store._paused_authorizations,
            [store.list_keys(ws) for ws in store.workspaces],
        ))
    return store._run_transaction(lambda conn: (
        conn.execute("SELECT * FROM tr_entities ORDER BY kind, id").fetchall(),
        conn.execute("SELECT * FROM tr_credit_balance ORDER BY workspace_id, shard").fetchall(),
        conn.execute("SELECT * FROM tr_key_limit ORDER BY key_hash, shard").fetchall(),
        conn.execute("SELECT * FROM tr_deferred_outstanding ORDER BY workspace_id").fetchall(),
    ))


def _create_authorization(store: Any, key: ApiKey, reservation_id: str | None, **kwargs: Any) -> Any:
    return store.create_gateway_authorization(
        workspace_id=key.workspace_id, key_hash=key.hash, model_id="m", provider="p",
        usage_type="Credits", estimated_microdollars=100, credit_reservation_id=reservation_id,
        idempotency_key="auth", **kwargs,
    )


@pytest.mark.parametrize("owner", ["workspace", "key", "both"])
@pytest.mark.parametrize("state", ["disarmed", "armed", "paused"])
def test_authorization_refuses_foreign_reservation_before_any_mutation(store: Any, owner: str, state: str) -> None:
    a = _key(store, "a", capped=True)
    b = _key(store, "b", workspace_id=a.workspace_id if owner == "key" else None, capped=True)
    caller = replace(b, hash=a.hash) if owner == "workspace" else b
    foreign = _reserve(store, a)
    _reserve(store, b, 70, "caller")
    assert store.reserve_key_limit(caller.hash, 100, usage_type="Credits").reserved_microdollars == 100
    if state != "disarmed":
        _pause(store, b, state == "paused")
    before = _billing_state(store)
    if not isinstance(store, InMemoryStore):
        conn = store._run_transaction(lambda conn: conn)
        conn.statements.clear()
    with pytest.raises(ValueError, match="^credit reservation belongs to another caller$"):
        _create_authorization(store, caller, foreign.id, deferred_cap_microdollars=1000)
    if not isinstance(store, InMemoryStore):
        assert all(sql.startswith("SELECT") for sql, _ in conn.statements)
        reservation_reads = [sql for sql, params in conn.statements if params == (_RESERVATION_KIND, foreign.id)]
        assert reservation_reads and all("FOR UPDATE" not in sql for sql in reservation_reads)
    assert _billing_state(store) == before
    assert not _finalized(store, foreign.id)


@pytest.mark.parametrize("reservation_type", ["own", "missing", "none"])
@pytest.mark.parametrize("armed", [False, True])
def test_authorization_accepts_own_or_absent_reservation_and_preserves_replay(
    store: Any, reservation_type: str, armed: bool,
) -> None:
    key = _key(store, "a")
    other = _key(store, "b")
    foreign = _reserve(store, other)
    if armed:
        _pause(store, key, False)
    reservation_id = _reserve(store, key).id if reservation_type == "own" else "missing" if reservation_type == "missing" else None
    auth = _create_authorization(store, key, reservation_id, deferred_cap_microdollars=1000)
    assert auth.credit_reservation_id == reservation_id
    if armed:
        _pause(store, key)
    before = _billing_state(store)
    assert _create_authorization(store, key, foreign.id, deferred_cap_microdollars=0).id == auth.id
    assert _billing_state(store) == before


def test_memory_authorization_empty_key_replay_precedes_ownership_guard() -> None:
    store = InMemoryStore()
    key = _key(store, "a")
    own = _reserve(store, key)
    foreign = _reserve(store, _key(store, "b"))
    args: dict[str, Any] = dict(
        workspace_id=key.workspace_id, key_hash=key.hash, model_id="m", provider="p",
        usage_type="Credits", estimated_microdollars=100, idempotency_key="",
    )
    auth = store.create_gateway_authorization(**args, credit_reservation_id=own.id)
    for armed in (False, True):
        store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=armed)
        before = _billing_state(store)
        assert store.create_gateway_authorization(**args, credit_reservation_id=foreign.id).id == auth.id
        assert _billing_state(store) == before


@pytest.mark.parametrize("retry_before_rearm", [False, True])
def test_disarmed_reservation_replaces_refunded_terminal_once(store: Any, retry_before_rearm: bool) -> None:
    key = _key(store, "a", capped=True)
    _pause(store, key, False)
    assert store.reserve_key_limit(key.hash, 70, usage_type="Credits").reserved_microdollars == 70
    sibling = _reserve(store, key, 70, "sibling")
    before = _balance(store, key)
    key_before = _key_reserved(store, key)
    assert store.reserve_key_limit(key.hash, 100, usage_type="Credits").reserved_microdollars == 100
    first = _reserve(store, key, idem="auth")
    assert not _finalized(store, first.id)
    assert _balance(store, key)["reserved"] == before["reserved"] + 100
    assert _key_reserved(store, key) == key_before + 100

    if isinstance(store, InMemoryStore):
        store.credit_trust_shards[(key.workspace_id, 0)]["pause_epoch"] += 1
    else:
        store._run_transaction(lambda conn: conn.execute(
            "UPDATE tr_credit_balance SET pause_epoch = pause_epoch + 1 WHERE workspace_id = %s",
            (key.workspace_id,),
        ))
    with pytest.raises(BillingPausedError):
        _create_authorization(store, key, first.id, key_reserved_microdollars=100)
    assert _finalized(store, first.id)
    assert _balance(store, key) == before
    assert _key_reserved(store, key) == key_before
    refunded = asdict(_reservation(store, first.id))

    def assert_refund_unchanged() -> None:
        assert asdict(_reservation(store, first.id)) == refunded
        assert _finalized(store, first.id)
        if not isinstance(store, InMemoryStore):
            marker = store._run_transaction(lambda conn: store._read_entity_tx(
                conn, _RESERVATION_FINALIZATION_KIND, first.id, dict,
            ))
            assert marker == {"actual_microdollars": 0, "operation": "billing_paused"}

    assert_refund_unchanged()
    store.trust_settings.spend_lease_trust_eligibility_enabled = False
    assert store.reserve_key_limit(key.hash, 100, usage_type="Credits").reserved_microdollars == 100
    second = _reserve(store, key, idem="auth")
    assert second.id != first.id
    assert not _finalized(store, second.id)
    assert _balance(store, key)["reserved"] == before["reserved"] + 100
    assert _key_reserved(store, key) == key_before + 100
    assert_refund_unchanged()
    auth = _create_authorization(store, key, second.id, key_reserved_microdollars=100)
    assert auth.credit_reservation_id == second.id
    if retry_before_rearm:
        assert _reserve(store, key, idem="auth").id == second.id
    assert _balance(store, key)["reserved"] == before["reserved"] + 100
    assert _key_reserved(store, key) == key_before + 100
    assert_refund_unchanged()

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=31, selected_usage_type="Credits",
    )
    assert _finalized(store, second.id)
    settled = _balance(store, key)
    assert settled["reserved"] == before["reserved"]
    assert settled["total_usage"] == before["total_usage"] + 31
    assert settled["total_credits"] == before["total_credits"]
    assert _key_reserved(store, key) == key_before
    assert_refund_unchanged()

    store.trust_settings.spend_lease_trust_eligibility_enabled = True
    assert _reserve(store, key, idem="auth").id == second.id
    assert _balance(store, key) == settled
    assert _key_reserved(store, key) == key_before
    assert_refund_unchanged()
    assert not _finalized(store, sibling.id)
