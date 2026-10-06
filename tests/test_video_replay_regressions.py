"""Real authorize/lookup regressions for the round-two recovery contract."""
from __future__ import annotations

import copy
from unittest.mock import Mock

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.test_service_surfaces import _app
from tests.test_video_derived_routing import MODEL, URL, payload, request
from tests.test_video_execution_replay import main_fingerprint, resolution_only_fingerprint
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway, video_jobs
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import STORE, InMemoryStore, configure_store

LOOKUP = "/v1/internal/gateway/video/replay-lookup"


@pytest.mark.parametrize("version", ["new", "main", "resolution-only"])
@pytest.mark.parametrize("region", [None, "", "not-configured", "us-central1"])
@pytest.mark.parametrize("rollout", [False, True])
def test_real_authorization_region_replays(client, inference_key, monkeypatch, version, region, rollout):
    body = payload(inference_key, max_tokens=1, max_output_tokens=1, region=region,
                   additional_cost_reservation_microdollars=900_000,
                   provider={"only": ["venice"]})
    with monkeypatch.context() as writer:
        if version != "new":
            writer.setattr(gateway, "_gateway_authorize_fingerprint", {
                "main": main_fingerprint, "resolution-only": resolution_only_fingerprint,
            }[version])
        first = client.post(URL, json=body)
    assert first.status_code == 200, first.text
    auth = copy.deepcopy(STORE.get_gateway_authorization(first.json()["data"]["authorization_id"]))
    account = STORE.credit_money_snapshot(auth.workspace_id)
    key = copy.deepcopy(STORE.get_key_by_hash(auth.key_hash))
    retry = dict(body)
    if rollout:
        retry.update(max_tokens=400_000, max_output_tokens=400_000, video_resolution="1080p")
        # New hashes discard supplied execution locality altogether. Old
        # hashes can recover supplied, frozen execution and omitted forms only.
        if version == "new" or region in (None, "us-central1"):
            retry["region"] = "europe-west4"
    replay = client.post(URL, json=retry)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["idempotent_replay"] is True
    assert replay.json()["data"]["authorization_id"] == auth.id
    retry.pop("additional_cost_reservation_microdollars")
    lookup = client.post(LOOKUP, json=retry)
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["data"]["authorization"]["authorization_id"] == auth.id
    for change in ({"request_fingerprint": "b" * 64}, {"provider": {"only": ["byteplus"]}},
                   {"estimated_input_tokens": 99}, {"tags": {"different": "identity"}}):
        for endpoint in (URL, LOOKUP):
            conflict = client.post(endpoint, json={**retry, **change})
            assert conflict.status_code == 409, conflict.text
    assert STORE.get_gateway_authorization(auth.id) == auth
    assert STORE.credit_money_snapshot(auth.workspace_id) == account
    assert STORE.get_key_by_hash(auth.key_hash).reserved_microdollars == key.reserved_microdollars


@pytest.mark.parametrize("metadata", [{"http_referer": "https://example.com"}, {"app_categories": []}])
def test_authorize_then_lookup_normalizes_attribution(client, inference_key, metadata):
    body = payload(inference_key, **metadata)
    first = client.post(URL, json=body)
    assert first.status_code == 200, first.text
    lookup = client.post(LOOKUP, json=body)
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["data"]["authorization"]["authorization_id"] == first.json()["data"]["authorization_id"]


@pytest.mark.parametrize("surface", ["deployed", "route"])
@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"},
                                     {"x-trustedrouter-internal-token": "wrong"}])
def test_lookup_http_auth_before_any_read(monkeypatch, headers, surface):
    client = TestClient(_app("internal", internal_gateway_token="test-secret"))  # noqa: S106
    if surface == "route":
        app = FastAPI()
        router = APIRouter()
        video_jobs.register(router)
        app.include_router(router)
        app.state.settings = Settings(environment="test", internal_gateway_token="test-secret")  # noqa: S106
        client = TestClient(app)
    key_read = Mock(side_effect=AssertionError("unauthenticated key read"))
    auth_read = Mock(side_effect=AssertionError("unauthenticated authorization read"))
    monkeypatch.setattr(gateway, "_api_key_for_gateway_lookup", key_read)
    monkeypatch.setattr(InMemoryStore, "get_gateway_authorization_by_idempotency_key", auth_read)
    body = payload("unused")
    response = client.post("/internal/gateway/video/replay-lookup", json=body, headers=headers)
    assert response.status_code == 401, response.text
    key_read.assert_not_called()
    auth_read.assert_not_called()


@pytest.mark.parametrize("changed_identity", [False, True])
@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_legacy_concurrent_authorize(monkeypatch, changed_identity, backend):
    """The winner commits during the loser's routing, after its early miss."""
    store = STORE.in_memory_target
    if backend == "postgres":
        from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
        conn = sqlite_postgres_conn()
        store = postgres_store_on(conn)
        configure_store(store)
    workspace = store.create_workspace("user", "race", trial_credit_microdollars=10_000_000)
    _, key = store.create_api_key(workspace_id=workspace.id, name="race", creator_user_id="user",
                                 limit_microdollars=10_000_000)
    body = GatewayAuthorizeRequest(api_key_hash=key.hash, model=MODEL, route_type="videos",
                                   idempotency_key="legacy-race", request_fingerprint="a" * 64,
                                   max_tokens=300_000, additional_cost_reservation_microdollars=900_000)
    settings = Settings(environment="test")
    original_route = gateway.video_route_endpoint_candidates
    winner = None
    holds_after_winner = None
    hold_calls = []
    for method in ("reserve_key_limit", "reserve", "create_gateway_authorization"):
        original = getattr(type(store), method)

        def tracked(self, *args, _method=method, _original=original, **kwargs):
            hold_calls.append(_method)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(type(store), method, tracked)

    def route(*args, **kwargs):
        nonlocal winner, holds_after_winner
        # Restore before the nested call so only the loser pauses in routing.
        monkeypatch.setattr(gateway, "video_route_endpoint_candidates", original_route)
        winner = gateway._authorize_gateway_sync(request("venice"), body, settings)["data"]
        holds_after_winner = list(hold_calls)
        return original_route(*args, **kwargs)

    monkeypatch.setattr(gateway, "video_route_endpoint_candidates", route)
    loser_body = body.model_copy(update={"request_fingerprint": ("b" if changed_identity else "a") * 64})
    if changed_identity:
        with pytest.raises(HTTPException) as error:
            gateway._authorize_gateway_sync(request("byteplus"), loser_body, settings)
        assert error.value.status_code == 409
    else:
        replay = gateway._authorize_gateway_sync(request("byteplus"), loser_body, settings)["data"]
        assert replay["idempotent_replay"] is True
        assert replay["route_candidates"] == winner["route_candidates"]
        assert replay["authorization_id"] == winner["authorization_id"]
    assert winner is not None and winner["idempotent_replay"] is False
    assert holds_after_winner == ["reserve_key_limit", "reserve", "create_gateway_authorization"]
    assert hold_calls == holds_after_winner  # The loser never takes either hold.
    auth = store.get_gateway_authorization(winner["authorization_id"])
    if backend == "postgres":
        reserved = conn.execute("SELECT reserved FROM tr_key_limit WHERE key_hash = %s", (key.hash,)).fetchone()[0]
        assert reserved == auth.key_reserved_microdollars
        assert conn.balance(workspace.id)[2] == auth.estimated_microdollars
    else:
        assert key.reserved_microdollars == auth.key_reserved_microdollars
        assert store.credit_money_snapshot(workspace.id)[2] == auth.estimated_microdollars
