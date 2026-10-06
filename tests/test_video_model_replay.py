"""Differential replay/validation tests using main's actual authorization writer."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_service_surfaces import _app
from tests.test_video_derived_routing import MODEL, URL, no_reservation
from tests.test_video_replay_regressions import LOOKUP
from trusted_router.partner_billing import (
    PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX,
    PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE,
)
from trusted_router.routes.internal import gateway
from trusted_router.storage import STORE, InMemoryStore


@pytest.fixture(scope="module")
def client():
    # Exercise the deployed HTTP surface without rebuilding every public route
    # for each differential case. Store state and settings patches reset per test.
    return TestClient(_app("internal", internal_gateway_token=None))


@pytest.fixture
def main_writer():
    # Keep the writer independent of current preparation and hashing. The
    # snapshot is verbatim source, pinned in its header; no git checkout needed.
    path = Path(__file__).parent / "fixtures/gateway_authorize_main.py.txt"
    namespace = dict(vars(gateway))
    exec(compile(path.read_text(), str(path), "exec"), namespace)  # noqa: S102 - frozen test oracle
    return namespace["_authorize_gateway_sync_impl"]


def seed_model(kind):
    workspace = STORE.create_workspace("owner", "legacy-video", trial_credit_microdollars=10_000_000)
    _, key = STORE.create_api_key(workspace_id=workspace.id, name="legacy", creator_user_id="owner",
                                 limit_microdollars=10_000_000)
    common = dict(owner_user_id="owner", owner_workspace_id=workspace.id, name="Legacy video")
    if kind == "user":
        model = STORE.create_user_model(**common, kind="machine", endpoint_url="https://owner.example/v1",
                                       prompt_price_microdollars_per_million_tokens=100,
                                       completion_price_microdollars_per_million_tokens=200)
        STORE.set_user_model_online(model.id, owner_user_id="owner", online=True)
    else:
        # Existing wrappers can have video bases in storage; the actual main
        # authorizer below must accept this route (no mocked routing or hashes).
        model = STORE.create_custom_model(**common, base_model_id=MODEL, hidden_prompt="policy")
    return model, dict(api_key_lookup_hash=key.lookup_hash, model=model.id, route_type="videos",
                      idempotency_key="main-model-video", request_fingerprint="a" * 64,
                      max_tokens=1, additional_cost_reservation_microdollars=900_000)


@pytest.mark.parametrize("kind", ["user", "custom"])
@pytest.mark.parametrize("endpoint", [URL, LOOKUP])
@pytest.mark.parametrize("drift", ["none", "disabled", "inactive", "off-clock", "dispatch-disabled", "provider-removed"])
def test_main_model_video_replay(client, monkeypatch, main_writer, kind, endpoint, drift):
    model, body = seed_model(kind)
    if drift == "provider-removed":
        body["provider"] = {"order": ["venice"], "allow_fallbacks": False}
    monkeypatch.setattr(client.app.state.settings, "user_models_dispatch_enabled", True)
    with monkeypatch.context() as old:
        old.setattr(gateway, "_authorize_gateway_sync_impl", main_writer)
        first = client.post(URL, json=body)
        assert first.status_code == 200, first.text
        # Establish that main itself replays the identical request.
        again = client.post(URL, json=body)
        assert again.status_code == 200, again.text
        assert again.json()["data"]["idempotent_replay"] is True
    auth = copy.deepcopy(STORE.get_gateway_authorization(first.json()["data"]["authorization_id"]))
    assert (auth.user_provided_model_id if kind == "user" else auth.custom_model_id) == model.id
    account = copy.deepcopy(STORE.get_credit_account(auth.workspace_id))
    key = copy.deepcopy(STORE.get_key_by_hash(auth.key_hash))
    if drift == "disabled":
        model.enabled = False
    elif drift == "inactive":
        model.status = "inactive"
    elif drift == "off-clock":
        model.online = False
    elif drift == "dispatch-disabled":
        monkeypatch.setattr(client.app.state.settings, "user_models_dispatch_enabled", False)

    if drift == "provider-removed":
        monkeypatch.delitem(gateway.PROVIDERS, "venice")

    def forbidden(*args, **kwargs):
        pytest.fail("replay read live model state or routing")

    no_reservation(monkeypatch)
    for method in ("get_user_model", "get_custom_model"):
        monkeypatch.setattr(InMemoryStore, method, forbidden)
    monkeypatch.setattr(gateway, "user_model_is_on_the_clock", forbidden)
    monkeypatch.setattr(gateway, "video_route_endpoint_candidates", forbidden)
    monkeypatch.setattr(gateway, "provider_route_preferences", forbidden)
    for retry in (body, {**body, "max_tokens": 400_000, "video_resolution": "1080p",
                         "region": "europe-west4"}):
        if endpoint == LOOKUP:
            retry = {k: v for k, v in retry.items() if k != "additional_cost_reservation_microdollars"}
        response = client.post(endpoint, json=retry)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        if endpoint == LOOKUP:
            data = data["authorization"]
        assert data["authorization_id"] == auth.id
        assert data["idempotent_replay"] is True
        assert data.get("invocation_nonce") is None
    assert STORE.get_gateway_authorization(auth.id) == auth
    assert STORE.get_credit_account(auth.workspace_id) == account
    assert STORE.get_key_by_hash(auth.key_hash).reserved_microdollars == key.reserved_microdollars


@pytest.mark.parametrize("kind", ["user", "custom"])
@pytest.mark.parametrize("endpoint", [URL, LOOKUP])
@pytest.mark.parametrize("change", ["model", "prepared-model", "custom_model_id", "revision", "policy", "byok", "hmac"])
def test_main_model_video_identity_conflicts(client, monkeypatch, main_writer, kind, endpoint, change):
    model, body = seed_model(kind)
    if change == "prepared-model":
        body.pop("max_tokens")
    monkeypatch.setattr(client.app.state.settings, "user_models_dispatch_enabled", True)
    with monkeypatch.context() as old:
        old.setattr(gateway, "_authorize_gateway_sync_impl", main_writer)
        first = client.post(URL, json=body)
    assert first.status_code == 200, first.text
    auth = copy.deepcopy(STORE.get_gateway_authorization(first.json()["data"]["authorization_id"]))
    account = copy.deepcopy(STORE.get_credit_account(auth.workspace_id))
    key = copy.deepcopy(STORE.get_key_by_hash(auth.key_hash))
    changes = {
        "model": {"model": MODEL},
        "prepared-model": {"model": MODEL, "custom_model_id": model.id,
                           "custom_model_revision": model.revision, "provider": {"usage": "credits"}},
        "custom_model_id": {"custom_model_id": "tr-custom-model/different"},
        "revision": {"custom_model_revision": model.revision + 1},
        "policy": {"provider": {"only": ["byteplus"]}},
        "byok": {"provider": {"usage": "byok"}},
        "hmac": {"request_fingerprint": "b" * 64},
    }
    no_reservation(monkeypatch)
    if endpoint == LOOKUP:
        body.pop("additional_cost_reservation_microdollars")
    response = client.post(endpoint, json={**body, **changes[change]})
    assert response.status_code == 409, response.text
    assert STORE.get_gateway_authorization(auth.id) == auth
    assert STORE.get_credit_account(auth.workspace_id) == account
    assert STORE.get_key_by_hash(auth.key_hash).reserved_microdollars == key.reserved_microdollars


NON_VIDEO_ROUTES = [None, "chat.completions", "responses", "messages", "embeddings", "images",
                    "decide", "batch.native.openai", gateway.POLYPHEMUS_SELECT_ROUTE_TYPE,
                    PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE,
                    PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX + "chat"]


@pytest.mark.parametrize("route", NON_VIDEO_ROUTES)
@pytest.mark.parametrize("case", ["tags", "batch-key", "idempotency"])
def test_nonvideo_main_error_precedence(client, monkeypatch, main_writer, route, case):
    _, body = seed_model("user")
    body.update(route_type=route, model="tr-custom-model/missing")
    expected = (404, "not_found")
    headers = {}
    if case == "tags":
        body.update(tags={"trustedrouter:invalid": "reserved"}, http_referer="invalid-url")
        expected = (400, "invalid_tags")
    elif case == "batch-key":
        body["idempotency_key"] = "tr-native-batch:bad-route"
    else:
        body.pop("idempotency_key")
        headers = {"idempotency-key": "x" * 257}
    with monkeypatch.context() as old:
        old.setattr(gateway, "_authorize_gateway_sync_impl", main_writer)
        baseline = client.post(URL, json=body, headers=headers)
    actual = client.post(URL, json=body, headers=headers)
    assert (baseline.status_code, baseline.json()["error"]["type"]) == expected, baseline.text
    assert (actual.status_code, actual.json()["error"]["type"]) == expected, actual.text
