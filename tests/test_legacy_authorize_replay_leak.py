"""Deterministic legacy authorize races, using real memory and Postgres money code."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_errors import StoreConflict
from trusted_router.storage_models import Reservation
from trusted_router.typed_balance import live_credit_summary


@pytest.fixture(params=["memory", "postgres"])
def backend(request: Any) -> Any:
    conn = None
    if request.param == "memory":
        store: Any = InMemoryStore()
    else:
        conn = sqlite_postgres_conn()
        store = postgres_store_on(conn)
    configure_store(store)
    user = store.ensure_user("replay@example.com", trial_credit_microdollars=0)
    workspace = store.create_workspace(user.id, "replay", trial_credit_microdollars=0)
    store.credit_workspace_once(workspace.id, 10_000_000, "seed")
    _, key = store.create_api_key(
        workspace_id=workspace.id,
        name="replay",
        creator_user_id=user.id,
        limit_microdollars=5_000_000,
    )
    yield store, conn, workspace.id, key.hash
    configure_store(InMemoryStore())
    if conn is not None:
        conn._raw.close()


@pytest.fixture(params=["before_reserve", "before_create"])
def race(backend: Any, request: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    store, conn, workspace_id, key_hash = backend

    def run(*, mismatch: bool = False, last_credits: bool = False) -> Any:
        body = _body(key_hash)
        if last_credits:
            probe = _authorize(body.model_copy(update={"idempotency_key": "estimate-probe"}))
            probe_authorization = store.get_gateway_authorization(probe["authorization_id"])
            assert probe_authorization is not None
            estimate = probe_authorization.estimated_microdollars
            assert estimate > 0
            assert store.finalize_gateway_authorization(
                probe["authorization_id"], success=False, actual_microdollars=0,
                selected_usage_type=probe_authorization.usage_type,
            )
            balance = live_credit_summary(workspace_id, store=store)
            assert balance is not None
            assert store.debit_workspace_guarded(
                workspace_id, balance["total_credits"] - estimate, "leave-one-estimate", kind="test"
            ) == "accepted"
            balance = live_credit_summary(workspace_id, store=store)
            assert balance is not None
            assert balance["total_credits"] == estimate
            assert _holds(backend) == (0, 0)

        method = "reserve" if request.param == "before_reserve" else "create_gateway_authorization"
        original = getattr(type(store), method)
        winner: dict[str, Any] = {}
        entered = False

        def complete_a(self: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal entered
            if not entered:
                entered = True
                # B has passed the empty pre-probe. A completes before B's
                # credit hold, or after both B holds but before B's insert.
                winner.update(_authorize(body))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(type(store), method, complete_a)
        loser: Any
        try:
            loser = _authorize(
                # Change the fingerprint without changing the shared credit estimate.
                body.model_copy(update={"tags": {"request": "different"}}) if mismatch else body
            )
        except HTTPException as exc:
            loser = exc
        assert winner and entered
        authorization = store.get_gateway_authorization(winner["authorization_id"])
        assert authorization is not None
        return store, conn, workspace_id, key_hash, winner, loser, authorization

    return run


@pytest.fixture(params=["before_reserve", "after_reserve"])
def deferred_race(backend: Any, request: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    store, conn, workspace_id, key_hash = backend
    settings = Settings(environment="test", federation_deferred_settlement_enabled=True)
    # Simulate eligibility of a federated key without replacing the local fixture's identity.
    monkeypatch.setattr(
        gateway, "_deferred_settlement_applies",
        lambda settings, api_key: settings.federation_deferred_settlement_enabled,
    )

    def run(
        *, foreign_field: str | None = None,
        on_stage: Callable[[str, Reservation | None], None] = lambda stage, reservation: None,
    ) -> Any:
        balance = live_credit_summary(workspace_id, store=store)
        assert balance is not None
        assert store.debit_workspace_guarded(
            workspace_id, balance["total_credits"], "empty-local-credit", kind="test"
        ) == "accepted"
        original = type(store).reserve
        original_create = type(store).create_gateway_authorization
        body = _body(key_hash)
        winner: dict[str, Any] = {}
        entered = False

        def complete_deferred_a(self: Any, *args: Any, **kwargs: Any) -> Reservation:
            nonlocal entered
            if entered:
                return original(self, *args, **kwargs)
            entered = True
            reservation: Reservation | None = None

            def reserve_b() -> None:
                nonlocal reservation
                assert store.claim_credit_transfer(
                    transfer_id="credit-arrives", workspace_id=workspace_id,
                    amount_microdollars=10_000_000, source="home", accept=True,
                ) == "accepted"
                on_stage("before_reserve", None)
                before = _holds(backend)[1]
                reservation = original(self, *args, **kwargs)
                assert _holds(backend)[1] == before + reservation.amount_microdollars
                assert reservation.amount_microdollars > 0
                on_stage("before_create", reservation)

            def create_a(self: Any, *a: Any, **kw: Any) -> Any:
                if kw["key_hash"] == key_hash and kw["settlement"] == "deferred_home":
                    assert kw["credit_reservation_id"] is None
                    assert _holds(backend)[1] == 0
                    if request.param == "after_reserve":
                        reserve_b()
                return original_create(self, *a, **kw)

            # A fails its credit reserve before the transfer. Its deferred create
            # lands either before or after B acquires a local credit reservation.
            monkeypatch.setattr(type(store), "create_gateway_authorization", create_a)
            winner.update(_authorize(body, settings))
            authorization = store.get_gateway_authorization(winner["authorization_id"])
            assert authorization is not None
            assert authorization.settlement == "deferred_home"
            assert authorization.credit_reservation_id is None
            if request.param == "before_reserve":
                reserve_b()
            assert reservation is not None
            if foreign_field is not None:
                # Return foreign ownership metadata as if reserve found another
                # tenant's keyed reservation; the replay must not refund it.
                reservation = replace(reservation, **{foreign_field: "foreign-owner"})
            return reservation

        monkeypatch.setattr(type(store), "reserve", complete_deferred_a)
        loser = _authorize(body, settings)
        on_stage("after_reclaim", None)
        authorization = store.get_gateway_authorization(winner["authorization_id"])
        assert authorization is not None
        assert loser["idempotent_replay"] is True
        assert loser["authorization_id"] == winner["authorization_id"]
        assert loser["credit_reservation_id"] is None
        return store, conn, workspace_id, key_hash, winner, loser, authorization

    return run


def _body(key_hash: str) -> GatewayAuthorizeRequest:
    return GatewayAuthorizeRequest(
        api_key_hash=key_hash,
        model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100,
        max_output_tokens=100,
        idempotency_key="racing-request",
    )


def _authorize(
    body: GatewayAuthorizeRequest, settings: Settings | None = None
) -> dict[str, Any]:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.request_id = "replay-test-request"
    return gateway._authorize_gateway_sync_impl(
        req, body, settings or Settings(environment="test"),
        {"boot_auth": None, "boot_verified": False},
    )["data"]


def _holds(result: Any) -> tuple[int, int]:
    store, conn, workspace_id, key_hash, *_ = result
    if conn is None:
        reserved = store.get_key_by_hash(key_hash).reserved_microdollars
    else:
        reserved = conn.execute(
            "SELECT reserved FROM tr_key_limit WHERE key_hash = %s AND shard = 0",
            (key_hash,),
        ).fetchone()[0]
    balance = live_credit_summary(workspace_id, store=store)
    assert balance is not None
    return reserved, balance["reserved"]


def _assert_winner_holds(result: Any) -> None:
    authorization = result[-1]
    assert authorization.key_reserved_microdollars > 0
    assert authorization.estimated_microdollars > 0
    assert _holds(result) == (
        authorization.key_reserved_microdollars, authorization.estimated_microdollars,
    )


def test_concurrent_replay_keeps_one_authorization(race: Any) -> None:
    result = race()
    store, conn, workspace_id, key_hash, winner, loser, authorization = result
    _assert_winner_holds(result)
    assert not isinstance(loser, HTTPException)
    if conn is None:
        assert len(store.api_keys.gateway_authorizations) == 1
    else:
        assert conn.count_entities("gateway_authorization") == 1
    indexed = store.get_gateway_authorization_by_idempotency_key(
        workspace_id, key_hash, "racing-request"
    )
    assert indexed.id == authorization.id == winner["authorization_id"] == loser["authorization_id"]


def test_concurrent_replay_refunds_loser_key_hold(race: Any) -> None:
    _assert_winner_holds(race())


def test_concurrent_replay_preserves_winner_credit_hold(race: Any) -> None:
    result = race()
    store, conn, *_, authorization = result
    _assert_winner_holds(result)
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None
    if conn is None:
        assert not store.api_keys.reservations[reservation_id].settled
    else:
        assert conn.has_entity("reservation", reservation_id)
        assert not conn.has_entity("reservation_finalization", reservation_id)


def test_concurrent_deferred_replay_refunds_orphan_credit(deferred_race: Any) -> None:
    result = deferred_race()
    store, _, _, key_hash, *_, authorization = result
    assert _holds(result) == (authorization.key_reserved_microdollars, 0)
    assert store.finalize_gateway_authorization(
        authorization.id, success=True,
        actual_microdollars=authorization.estimated_microdollars,
        selected_usage_type=authorization.usage_type,
    )
    assert _holds(result) == (0, 0)
    retry = _authorize(_body(key_hash))
    assert retry["idempotent_replay"] is True
    assert retry["authorization_id"] == authorization.id
    assert _holds(result) == (0, 0)


@pytest.mark.parametrize("foreign_field", ["workspace_id", "key_hash"])
def test_concurrent_replay_preserves_foreign_credit(
    deferred_race: Any, backend: Any, monkeypatch: pytest.MonkeyPatch, foreign_field: str,
) -> None:
    store, *_ = backend
    original = type(store).refund
    refunded: list[str] = []

    def track_refund(self: Any, reservation_id: str) -> Any:
        refunded.append(reservation_id)
        return original(self, reservation_id)

    monkeypatch.setattr(type(store), "refund", track_refund)
    result = deferred_race(foreign_field=foreign_field)
    authorization = result[-1]
    assert refunded == []
    assert _holds(result) == (authorization.estimated_microdollars,) * 2


@pytest.mark.parametrize("other_workspace", [False, True])
@pytest.mark.parametrize("contender_stage", ["before_reserve", "before_create", "after_reclaim"])
def test_deferred_reclaim_cannot_release_another_callers_reservation(
    deferred_race: Any, backend: Any, monkeypatch: pytest.MonkeyPatch,
    other_workspace: bool, contender_stage: str,
) -> None:
    store, conn, workspace_id, key_hash = backend
    owner = store.get_key_by_hash(key_hash).creator_user_id
    contender_workspace = workspace_id
    if other_workspace:
        contender_workspace = store.create_workspace(
            owner, "contender", trial_credit_microdollars=0,
        ).id
        store.credit_workspace_once(contender_workspace, 10_000_000, "contender-seed")
    _, contender_key = store.create_api_key(
        workspace_id=contender_workspace, name="contender", creator_user_id=owner,
        limit_microdollars=5_000_000,
    )
    contender_backend = store, conn, contender_workspace, contender_key.hash
    contender: Any = None
    orphan: Reservation | None = None
    original_reserve = type(store).reserve
    original_refund = type(store).refund
    reservations: list[Reservation] = []
    refunds: list[str] = []

    def record_reserve(self: Any, *args: Any, **kwargs: Any) -> Reservation:
        reservation = original_reserve(self, *args, **kwargs)
        if reservation.key_hash == contender_key.hash:
            reservations.append(reservation)
        return reservation

    def record_refund(self: Any, reservation_id: str) -> Any:
        refunds.append(reservation_id)
        before = _holds(contender_backend)
        result = original_refund(self, reservation_id)
        if contender is not None:
            # Shared workspaces lose only B's escrow; C's key hold stays intact.
            assert orphan is not None
            assert before == (
                contender.key_reserved_microdollars,
                contender.estimated_microdollars + (
                    0 if other_workspace else orphan.amount_microdollars
                ),
            )
            assert _holds(contender_backend) == (
                contender.key_reserved_microdollars, contender.estimated_microdollars,
            )
        return result

    def at_stage(stage: str, reservation: Reservation | None) -> None:
        nonlocal contender, orphan
        if reservation is not None:
            orphan = reservation
        if stage == contender_stage:
            response = _authorize(_body(contender_key.hash))
            assert response["idempotent_replay"] is False
            contender = store.get_gateway_authorization(response["authorization_id"])
            assert contender is not None and not contender.settled
            assert contender.settlement == "local"
            assert len(reservations) == 1
            own_reservation = reservations[0]
            assert own_reservation.workspace_id == contender_workspace
            assert own_reservation.key_hash == contender_key.hash
            assert contender.credit_reservation_id == own_reservation.id
            assert own_reservation.amount_microdollars == contender.estimated_microdollars > 0
            expected_credit = contender.estimated_microdollars
            if stage == "before_create" and not other_workspace:
                assert orphan is not None
                expected_credit += orphan.amount_microdollars
            assert _holds(contender_backend) == (contender.key_reserved_microdollars, expected_credit)

    monkeypatch.setattr(type(store), "reserve", record_reserve)
    monkeypatch.setattr(type(store), "refund", record_refund)
    result = deferred_race(on_stage=at_stage)
    winner = result[-1]
    assert contender is not None and orphan is not None
    assert contender.credit_reservation_id != orphan.id
    assert refunds == [orphan.id]
    assert _holds(contender_backend) == (
        contender.key_reserved_microdollars, contender.estimated_microdollars,
    )
    assert _holds(backend) == (
        winner.key_reserved_microdollars,
        0 if other_workspace else contender.estimated_microdollars,
    )
    for authorization in (winner, contender):
        assert store.finalize_gateway_authorization(
            authorization.id, success=True, actual_microdollars=authorization.estimated_microdollars,
            selected_usage_type=authorization.usage_type,
        )
    assert _holds(backend) == _holds(contender_backend) == (0, 0)


def test_concurrent_replay_returns_stored_response(
    race: Any, backend: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, *_ = backend
    original_routes = gateway.chat_route_endpoint_candidates
    original_create = type(store).create_gateway_authorization
    routes: list[Any] = []
    attempts: list[dict[str, Any]] = []

    def drifting_routes(*args: Any, **kwargs: Any) -> Any:
        candidates = (
            original_routes({"model": "openai/gpt-4.1-mini"}, Settings(environment="test"))
            if not routes else original_routes(*args, **kwargs)
        )
        routes.append(candidates)
        return candidates

    def record_create(self: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs)
        return original_create(self, **kwargs)

    monkeypatch.setattr(gateway, "chat_route_endpoint_candidates", drifting_routes)
    monkeypatch.setattr(
        gateway, "_endpoint_cost_microdollars",
        lambda *args, **kwargs: 2_000 if len(routes) == 1 else 1_000,
    )
    monkeypatch.setattr(type(store), "create_gateway_authorization", record_create)
    *_, winner, loser, authorization = race()
    assert len(routes) == len(attempts) == 2
    loser_attempt = next(a for a in attempts if a["authorization_id"] != authorization.id)
    assert loser_attempt["estimated_microdollars"] != authorization.estimated_microdollars
    assert loser_attempt["candidate_endpoint_ids"] != authorization.candidate_endpoint_ids
    for field in ("model_id", "provider", "endpoint_id"):
        assert loser_attempt[field] != getattr(authorization, field)
    assert not isinstance(loser, HTTPException)
    assert loser["idempotent_replay"] is True
    expected = {
        "authorization_id": authorization.id,
        "estimated_cost_microdollars": authorization.estimated_microdollars,
        "model": authorization.model_id,
        "provider": authorization.provider,
        "endpoint_id": authorization.endpoint_id,
        "credit_reservation_id": authorization.credit_reservation_id,
    }
    for field, value in expected.items():
        assert loser[field] == winner[field] == value


def test_concurrent_replay_settlement_leaves_no_holds(race: Any) -> None:
    result = race()
    store, *_, authorization = result
    assert store.finalize_gateway_authorization(
        authorization.id,
        success=True,
        actual_microdollars=authorization.estimated_microdollars,
        selected_usage_type=authorization.usage_type,
    )
    assert _holds(result) == (0, 0)
    balance = live_credit_summary(authorization.workspace_id, store=store)
    assert balance is not None
    assert balance["total_usage"] == authorization.estimated_microdollars


def test_concurrent_fingerprint_mismatch_keeps_only_winner_holds(race: Any) -> None:
    result = race(mismatch=True)
    *_, loser, authorization = result
    assert isinstance(loser, HTTPException)
    assert loser.status_code == 409
    assert _holds(result) == (authorization.estimated_microdollars,) * 2


def test_create_conflict_retry_adopts_credit_reservation(
    backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, workspace_id, key_hash = backend
    original = type(store).create_gateway_authorization
    reservation_ids: list[str] = []

    def conflict_once(self: Any, *args: Any, **kwargs: Any) -> Any:
        reservation_ids.append(kwargs["credit_reservation_id"])
        if len(reservation_ids) == 1:
            raise StoreConflict("injected create conflict after credit reserve")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(store), "create_gateway_authorization", conflict_once)
    body = _body(key_hash)
    with pytest.raises(HTTPException) as failed:
        _authorize(body)
    assert failed.value.status_code == 503
    key_reserved, credit_reserved = _holds(backend)
    assert key_reserved == 0
    assert credit_reserved > 0
    assert store.get_gateway_authorization_by_idempotency_key(
        workspace_id, key_hash, body.idempotency_key
    ) is None

    retry = _authorize(body)
    authorization = store.get_gateway_authorization(retry["authorization_id"])
    assert authorization is not None
    assert store.finalize_gateway_authorization(
        authorization.id,
        success=True,
        actual_microdollars=authorization.estimated_microdollars,
        selected_usage_type=authorization.usage_type,
    )
    assert _holds(backend) == (0, 0)
    assert reservation_ids == [authorization.credit_reservation_id] * 2


def test_failed_create_reservation_reclaimed_once_when_retry_replays_deferred_winner(
    backend: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, workspace_id, key_hash = backend
    body = _body(key_hash)
    unrelated_response = _authorize(body.model_copy(update={"idempotency_key": "unrelated"}))
    unrelated = store.get_gateway_authorization(unrelated_response["authorization_id"])
    assert unrelated is not None
    baseline = _holds(backend)[1]
    assert baseline == unrelated.estimated_microdollars > 0
    balance = live_credit_summary(workspace_id, store=store)
    assert balance is not None
    assert store.debit_workspace_guarded(
        workspace_id, balance["total_credits"] - balance["total_usage"] - baseline,
        "leave-only-unrelated-escrow", kind="test",
    ) == "accepted"
    settings = Settings(environment="test", federation_deferred_settlement_enabled=True)
    monkeypatch.setattr(gateway, "_deferred_settlement_applies", lambda settings, api_key: True)
    original_create = type(store).create_gateway_authorization
    original_reserve = type(store).reserve
    original_refund = type(store).refund
    reservations: list[Reservation] = []
    refunds: list[str] = []
    credit_samples = [baseline]
    winner_args: dict[str, Any] = {}
    phase = "winner"

    def record_reserve(self: Any, *args: Any, **kwargs: Any) -> Reservation:
        reservation = original_reserve(self, *args, **kwargs)
        reservations.append(reservation)
        credit_samples.append(_holds(backend)[1])
        return reservation

    def record_refund(self: Any, reservation_id: str) -> Any:
        refunds.append(reservation_id)
        credit_samples.append(_holds(backend)[1])
        result = original_refund(self, reservation_id)
        credit_samples.append(_holds(backend)[1])
        return result

    def staged_create(self: Any, **kwargs: Any) -> Any:
        nonlocal phase
        if phase == "first_b":
            raise StoreConflict("B's first create rolled back")
        if phase == "retry_b":
            # A's deferred reserve predates the transfer, but its create lands
            # only after B's failed create and B's retry of the same reservation.
            original_create(self, **winner_args)
            return original_create(self, **kwargs)
        assert phase == "winner"
        winner_args.update(kwargs)
        assert kwargs["settlement"] == "deferred_home"
        assert kwargs["credit_reservation_id"] is None
        assert store.claim_credit_transfer(
            transfer_id="credit-arrives", workspace_id=workspace_id,
            amount_microdollars=10_000_000, source="home", accept=True,
        ) == "accepted"
        phase = "first_b"
        with pytest.raises(HTTPException) as failed:
            _authorize(body, settings)
        assert failed.value.status_code == 503
        assert len(reservations) == 1
        assert _holds(backend) == (
            unrelated.key_reserved_microdollars + kwargs["key_reserved_microdollars"],
            baseline + reservations[0].amount_microdollars,
        )
        phase = "retry_b"
        replay = _authorize(body, settings)
        assert replay["idempotent_replay"] is True
        assert replay["authorization_id"] == kwargs["authorization_id"]
        assert replay["credit_reservation_id"] is None
        assert len(reservations) == 2 and reservations[0].id == reservations[1].id
        assert refunds == [reservations[0].id]
        expected_holds = (
            unrelated.key_reserved_microdollars + kwargs["key_reserved_microdollars"], baseline,
        )
        assert _holds(backend) == expected_holds
        phase = "replayed"
        assert _authorize(body, settings)["authorization_id"] == kwargs["authorization_id"]
        assert len(reservations) == 2
        assert refunds == [reservations[0].id]
        assert _holds(backend) == expected_holds
        assert min(credit_samples) >= baseline
        return store.get_gateway_authorization(kwargs["authorization_id"])

    monkeypatch.setattr(type(store), "reserve", record_reserve)
    monkeypatch.setattr(type(store), "refund", record_refund)
    monkeypatch.setattr(type(store), "create_gateway_authorization", staged_create)
    response = _authorize(body, settings)
    winner = store.get_gateway_authorization(response["authorization_id"])
    assert winner is not None
    for authorization in (winner, unrelated):
        assert store.finalize_gateway_authorization(
            authorization.id, success=True, actual_microdollars=authorization.estimated_microdollars,
            selected_usage_type=authorization.usage_type,
        )
    assert _holds(backend) == (0, 0)


def test_concurrent_replay_with_only_one_estimate_of_credit(race: Any) -> None:
    result = race(last_credits=True)
    *_, winner, loser, authorization = result
    assert not isinstance(loser, HTTPException)
    assert loser["idempotent_replay"] is True
    assert loser["authorization_id"] == winner["authorization_id"]
    assert _holds(result) == (authorization.estimated_microdollars,) * 2


@pytest.mark.parametrize("mismatch", [False, True])
@pytest.mark.parametrize("error_type", [None, StoreConflict, RuntimeError])
def test_replay_key_refund_called_once_and_preserves_response(
    race: Any, backend: Any, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, mismatch: bool, error_type: type[Exception] | None,
) -> None:
    store, _, workspace_id, key_hash = backend
    calls: list[tuple[str, int, Any]] = []
    original = type(store).refund_key_limit
    sleeps: list[float] = []
    monkeypatch.setattr(gateway.time, "sleep", sleeps.append)

    def fail_refund(
        self: Any, refunded_key: str, amount: int, *, usage_type: Any
    ) -> None:
        calls.append((refunded_key, amount, usage_type))
        if error_type is not None:
            raise error_type(f"private request content: racing-request {key_hash}\nsecret")
        original(self, refunded_key, amount, usage_type=usage_type)

    monkeypatch.setattr(type(store), "refund_key_limit", fail_refund)
    with caplog.at_level(logging.ERROR, logger=gateway.logger.name):
        result = race(mismatch=mismatch)
    *_, winner, loser, authorization = result
    if mismatch:
        assert isinstance(loser, HTTPException)
        assert loser.status_code == 409
    else:
        assert not isinstance(loser, HTTPException)
        assert loser["idempotent_replay"] is True
        assert loser["authorization_id"] == winner["authorization_id"]
    estimate = authorization.estimated_microdollars
    assert calls == [(key_hash, authorization.key_reserved_microdollars, authorization.usage_type)]
    assert sleeps == []
    assert _holds(result) == (estimate * (2 if error_type else 1), estimate)
    records = [record for record in caplog.records if record.name == gateway.logger.name]
    if error_type is None:
        assert records == []
        return
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    outcome = "rolled_back" if error_type is StoreConflict else "unknown"
    assert records[0].getMessage() == (
        f"billing.replay_key_refund_failed workspace_id={workspace_id} "
        f"request_id=replay-test-request key_hash={key_hash[:12]} "
        f"reserved_microdollars={estimate} error_class={error_type.__name__} outcome={outcome}"
    )
    assert records[0].exc_info is None


@pytest.mark.parametrize("error_type", [None, StoreConflict, RuntimeError])
def test_replay_credit_refund_called_once_and_preserves_response(
    deferred_race: Any, backend: Any, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, error_type: type[Exception] | None,
) -> None:
    store, _, workspace_id, key_hash = backend
    original = type(store).refund
    calls: list[str] = []
    sleeps: list[float] = []
    monkeypatch.setattr(gateway.time, "sleep", sleeps.append)

    def fail_refund(self: Any, reservation_id: str) -> Any:
        calls.append(reservation_id)
        if error_type is not None:
            raise error_type(f"private request content: racing-request {key_hash}\nsecret")
        return original(self, reservation_id)

    monkeypatch.setattr(type(store), "refund", fail_refund)
    with caplog.at_level(logging.ERROR, logger=gateway.logger.name):
        result = deferred_race()
    authorization = result[-1]
    assert len(calls) == 1
    assert sleeps == []
    records = [record for record in caplog.records if record.name == gateway.logger.name]
    succeeded = error_type is None
    if succeeded:
        assert records == []
    else:
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert error_type is not None
        outcome = "rolled_back" if error_type is StoreConflict else "unknown"
        assert records[0].getMessage() == (
            f"billing.replay_credit_refund_failed workspace_id={workspace_id} "
            f"request_id=replay-test-request key_hash={key_hash[:12]} "
            f"reserved_microdollars={authorization.estimated_microdollars} "
            f"error_class={error_type.__name__} outcome={outcome}"
        )
        assert records[0].exc_info is None
    assert _holds(result) == (
        authorization.key_reserved_microdollars,
        0 if succeeded else authorization.estimated_microdollars,
    )


def test_replay_uncapped_key_skips_refund(
    race: Any, backend: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, _, key_hash = backend
    assert store.update_key(key_hash, {"limit_microdollars": None}) is not None
    calls: list[Any] = []

    def track_refund(self: Any, *args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(type(store), "refund_key_limit", track_refund)
    result = race()
    *_, loser, authorization = result
    assert loser["idempotent_replay"] is True
    assert authorization.key_reserved_microdollars == 0
    assert calls == []
    assert _holds(result) == (0, authorization.estimated_microdollars)


def test_replay_after_cap_removed_preserves_unrelated_hold(race: Any, backend: Any) -> None:
    store, _, _, key_hash = backend
    unrelated = _authorize(_body(key_hash).model_copy(update={"idempotency_key": "unrelated"}))
    unrelated_authorization = store.get_gateway_authorization(unrelated["authorization_id"])
    assert unrelated_authorization is not None
    held = unrelated_authorization.key_reserved_microdollars
    assert _holds(backend) == (held, held)
    assert held > 0
    assert store.update_key(key_hash, {"limit_microdollars": None}) is not None
    assert _holds(backend) == (held, held)

    result = race()
    *_, loser, authorization = result
    assert loser["idempotent_replay"] is True
    assert authorization.key_reserved_microdollars == 0
    assert _holds(result) == (held, held + authorization.estimated_microdollars)


def test_replay_refunds_recorded_key_hold_instead_of_estimate(
    race: Any, backend: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, *_ = backend
    original = type(store).reserve_key_limit

    def reserve_smaller_hold(self: Any, key_hash: str, amount: int, **kwargs: Any) -> Any:
        # The reserve result is authoritative even when it differs from the quote.
        return original(self, key_hash, amount // 2, **kwargs)

    monkeypatch.setattr(type(store), "reserve_key_limit", reserve_smaller_hold)
    result = race()
    *_, loser, authorization = result
    assert loser["idempotent_replay"] is True
    assert 0 < authorization.key_reserved_microdollars < authorization.estimated_microdollars
    assert _holds(result) == (
        authorization.key_reserved_microdollars, authorization.estimated_microdollars,
    )


# This proves only credit cleanup; the tombstone path still leaves a key hold.
# That is a pre-existing STORE bug outside the scope of this change.
def test_pause_epoch_after_both_reserves_releases_shared_credit(
    backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, conn, workspace_id, key_hash = backend
    settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=True)
    store.trust_settings = settings
    body = _body(key_hash)
    original = type(store).create_gateway_authorization
    attempts: list[dict[str, Any]] = []
    rejections: list[HTTPException] = []

    def pause_before_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs)
        if len(attempts) == 1:
            # The outer request has reserved; the inner request also reserves
            # before either reaches the real create transaction.
            with pytest.raises(HTTPException) as inner:
                _authorize(body, settings)
            rejections.append(inner.value)
        else:
            assert len(attempts) == 2
            assert all(attempt["credit_reservation_id"] for attempt in attempts)
            assert _holds(backend)[1] > 0
            if conn is None:
                store.credit_trust_shards[(workspace_id, 0)]["pause_epoch"] += 1
            else:
                conn.execute(
                    "UPDATE tr_credit_balance SET pause_epoch = pause_epoch + 1 "
                    "WHERE workspace_id = %s", (workspace_id,),
                )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(store), "create_gateway_authorization", pause_before_create)
    with pytest.raises(HTTPException) as outer:
        _authorize(body, settings)
    rejections.append(outer.value)
    assert len(attempts) == len(rejections) == 2
    assert all(exc.status_code == 403 and "billing_paused" in str(exc.detail) for exc in rejections)
    assert _holds(backend)[1] == 0
    if conn is None:
        assert not store.api_keys.gateway_authorizations
    else:
        assert conn.count_entities("gateway_authorization") == 0
