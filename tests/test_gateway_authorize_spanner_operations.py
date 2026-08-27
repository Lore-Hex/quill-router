from __future__ import annotations

import dataclasses

import pytest
from starlette.requests import Request

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import CreditAccount, Workspace, configure_store
from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _seed_typed_gateway_store() -> tuple[SpannerBigtableStore, object, object]:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="ws-rpc", name="RPC", owner_user_id="user-rpc")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id,
        "shard": 0,
        "total_credits": 50_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id,
        name="rpc-key",
        creator_user_id=workspace.owner_user_id,
        limit_microdollars=50_000_000,
    )
    configure_store(store)
    return store, database, key


def _body(key_hash: str, *, idempotency_key: str = "rpc-idem") -> GatewayAuthorizeRequest:
    return GatewayAuthorizeRequest(
        api_key_hash=key_hash,
        idempotency_key=idempotency_key,
        model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100,
        max_output_tokens=100,
    )


def test_typed_authorize_route_does_not_call_legacy_idempotency_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, key = _seed_typed_gateway_store()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("typed authorize must never probe the legacy entity index")

    monkeypatch.setattr(
        SpannerBigtableStore,
        "get_gateway_authorization_by_idempotency_key",
        forbidden,
    )

    response = gateway._authorize_gateway_sync(
        _request(), _body(key.hash), Settings(environment="test")
    )

    assert response["data"]["authorization_id"]
    assert response["data"]["idempotent_replay"] is False


def test_typed_authorize_route_does_not_call_typed_pretransaction_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, key = _seed_typed_gateway_store()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("typed fresh authorize must rely on its in-transaction probe")

    monkeypatch.setattr(
        SpannerBigtableStore,
        "get_typed_authorization_by_idempotency",
        forbidden,
    )

    response = gateway._authorize_gateway_sync(
        _request(), _body(key.hash), Settings(environment="test")
    )

    assert response["data"]["authorization_id"]
    assert response["data"]["idempotent_replay"] is False


def test_typed_authorize_replay_and_mismatch_still_come_from_transaction() -> None:
    _store, _database, key = _seed_typed_gateway_store()
    settings = Settings(environment="test")
    first = gateway._authorize_gateway_sync(_request(), _body(key.hash), settings)

    replay = gateway._authorize_gateway_sync(_request(), _body(key.hash), settings)

    assert replay["data"]["authorization_id"] == first["data"]["authorization_id"]
    assert replay["data"]["credit_reservation_id"] == first["data"]["credit_reservation_id"]
    assert replay["data"]["idempotent_replay"] is True

    changed = _body(key.hash)
    changed.max_output_tokens += 1
    with pytest.raises(Exception) as raised:
        gateway._authorize_gateway_sync(_request(), changed, settings)
    assert getattr(raised.value, "status_code", None) == 409


def test_typed_accepted_authorization_is_returned_without_post_commit_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key = _seed_typed_gateway_store()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("accepted authorize already has the exact inserted record")

    monkeypatch.setattr(SpannerBigtableStore, "get_gateway_authorization", forbidden)
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=key.workspace_id,
        key_hash=key.hash,
        estimate=1_000_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="m",
        provider="anthropic",
        requested_model_id="m",
        candidate_model_ids=["m"],
        region="us",
        endpoint_id="e",
        candidate_endpoint_ids=["e"],
        idempotency_key="direct-idem",
        idempotency_fingerprint="direct-fingerprint",
    )

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None


def test_typed_accepted_authorization_matches_persisted_record() -> None:
    store, _database, key = _seed_typed_gateway_store()
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=key.workspace_id,
        key_hash=key.hash,
        estimate=1_000_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="m",
        provider="anthropic",
        requested_model_id="m",
        candidate_model_ids=["m"],
        region="us",
        endpoint_id="e",
        candidate_endpoint_ids=["e"],
        idempotency_key="direct-equivalence",
        idempotency_fingerprint="direct-equivalence-fingerprint",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None

    persisted = store.get_gateway_authorization(authorization.id)

    assert persisted is not None
    assert dataclasses.asdict(authorization) == dataclasses.asdict(persisted)


def test_fresh_typed_gateway_authorize_has_exact_sequential_spanner_operation_count() -> None:
    _store, database, key = _seed_typed_gateway_store()
    gateway._BROADCAST_EMPTY_CACHE.clear()
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    before = (
        database.snapshot_execute_sql_calls,
        database.transaction_execute_sql_calls,
        database.transaction_execute_update_calls,
    )

    response = gateway._authorize_gateway_sync(
        _request(), _body(key.hash), Settings(environment="test")
    )

    after = (
        database.snapshot_execute_sql_calls,
        database.transaction_execute_sql_calls,
        database.transaction_execute_update_calls,
    )
    operation_count = sum(end - start for start, end in zip(before, after, strict=True))
    assert response["data"]["authorization_id"]
    # Representative steady-state fresh request: the workspace's observed-empty
    # broadcast cache is warm, while this idempotency key and authorization are new.
    assert operation_count == 10


def test_broadcast_empty_results_are_cached_until_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, key = _seed_typed_gateway_store()
    gateway._BROADCAST_EMPTY_CACHE.clear()
    now = [100.0]
    calls = 0

    def list_empty(_self: object, workspace_id: str) -> list[object]:
        nonlocal calls
        assert workspace_id == key.workspace_id
        calls += 1
        return []

    monkeypatch.setattr(gateway.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(SpannerBigtableStore, "list_broadcast_destinations", list_empty)

    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    assert calls == 1

    now[0] += gateway._BROADCAST_EMPTY_CACHE_TTL_SECONDS + 0.001
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    assert calls == 2


def test_positive_broadcast_results_are_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, key = _seed_typed_gateway_store()
    gateway._BROADCAST_EMPTY_CACHE.clear()
    destination = object()
    calls = 0

    def list_positive(_self: object, workspace_id: str) -> list[object]:
        nonlocal calls
        assert workspace_id == key.workspace_id
        calls += 1
        return [destination]

    monkeypatch.setattr(SpannerBigtableStore, "list_broadcast_destinations", list_positive)

    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == [destination]
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == [destination]
    assert calls == 2
    assert key.workspace_id not in gateway._BROADCAST_EMPTY_CACHE


def test_broadcast_empty_cache_evicts_oldest_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, _key = _seed_typed_gateway_store()
    gateway._BROADCAST_EMPTY_CACHE.clear()
    monkeypatch.setattr(gateway, "_BROADCAST_EMPTY_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(
        SpannerBigtableStore,
        "list_broadcast_destinations",
        lambda _self, _workspace_id: [],
    )

    for workspace_id in ("ws-1", "ws-2", "ws-3"):
        assert gateway._broadcast_destinations_for_authorize(workspace_id) == []

    assert list(gateway._BROADCAST_EMPTY_CACHE) == ["ws-2", "ws-3"]
