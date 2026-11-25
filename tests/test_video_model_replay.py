"""Catalog replay remains compatible; retired creator requests never reopen work."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.fixtures.generate_gateway_authorizations import (
    authorize,
    money_snapshot,
    seed_model,
)
from tests.test_service_surfaces import _app
from tests.test_video_derived_routing import URL, no_reservation
from tests.test_video_replay_regressions import LOOKUP
from trusted_router.partner_billing import (
    PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX,
    PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE,
)
from trusted_router.routes.internal import gateway, video_jobs
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import (
    ApiKey,
    CreditAccount,
    CreditMoney,
    GatewayAuthorization,
    UserProvidedModel,
    Workspace,
)

RECORDS = json.loads((Path(__file__).parent / "fixtures/gateway_authorizations_main.json").read_text())


@pytest.fixture(scope="module")
def client():
    return TestClient(_app("internal", internal_gateway_token=None, user_models_dispatch_enabled=True))


def load_record(kind):
    """Install recorded bytes, without calling today's writer or fingerprint helper."""
    record = copy.deepcopy(RECORDS["authorizations"][kind])
    store = STORE.in_memory_target
    key = ApiKey(**record["key"])
    workspace = Workspace(**record["workspace"])
    auth = GatewayAuthorization(**record["authorization"])
    store.workspaces[workspace.id] = workspace
    store.credits[workspace.id] = CreditAccount(**record["credit_account"])
    store.credit_money[workspace.id] = CreditMoney(**record["credit_money"])
    store.api_keys.keys[key.hash] = key
    store.api_keys.key_ids_by_lookup_hash[key.lookup_hash] = key.hash
    store.api_keys.gateway_authorizations[auth.id] = auth
    index = store.api_keys._gateway_authorization_idempotency_index_key(workspace.id, key.hash, auth.idempotency_key)
    store.api_keys.gateway_authorization_id_by_idempotency_key[index] = auth.id
    if record["user_model"]:
        model = UserProvidedModel(**record["user_model"])
        store.user_model_store.models[model.id] = model
    return record, store, key, auth


@pytest.mark.parametrize("kind", ["catalog", "user", "chat"])
def test_literal_main_authorizations(client, monkeypatch, kind):
    record, store, key, auth = load_record(kind)
    frozen = copy.deepcopy(auth)
    before = money_snapshot(store, None, key)
    no_reservation(monkeypatch)
    for retry in record["retries"]:
        response = client.post(URL, json=retry["body"])
        expected_status = 410 if kind == "user" else retry["current_status"]
        assert response.status_code == expected_status, response.text
        if response.status_code == 200:
            data = response.json()["data"]
            assert data["authorization_id"] == auth.id
            assert data["idempotent_replay"] is True
            assert data.get("invocation_nonce") is None
            assert data["credit_reservation_id"] == auth.credit_reservation_id
            assert data["region"] == auth.region
        if kind == "catalog":
            lookup_body = {k: v for k, v in retry["body"].items() if k != "additional_cost_reservation_microdollars"}
            lookup = client.post(LOOKUP, json=lookup_body)
            assert lookup.status_code == retry["current_status"], lookup.text
            if lookup.status_code == 200:
                assert lookup.json()["data"]["authorization"]["authorization_id"] == auth.id
        assert store.get_gateway_authorization(auth.id) == frozen
        assert money_snapshot(store, None, key) == before


@pytest.mark.parametrize("field", ["custom_model_id", "user_provided_model_id"])
def test_catalog_spelling_cannot_replay_historical_creator_hold(client, monkeypatch, field):
    record, store, key, auth = load_record("catalog")
    setattr(auth, field, "tr-custom-model/historical")
    frozen = copy.deepcopy(auth)
    before = money_snapshot(store, None, key)
    no_reservation(monkeypatch)
    response = client.post(URL, json=record["request"])
    assert response.status_code == 410, response.text
    assert store.get_gateway_authorization(auth.id) == frozen
    assert money_snapshot(store, None, key) == before


@pytest.mark.parametrize("backend", ["memory", "typed", "legacy"])
@pytest.mark.parametrize("reverse", [False, True])
def test_retired_wrapper_cannot_reuse_catalog_authorization(backend, reverse):
    store, database, key, _, wrapper = seed_model("custom", backend=backend)
    base = {**wrapper, "model": "bytedance/seedance-2.5"}
    first = authorize(base) if reverse else None
    before = money_snapshot(store, database, key)
    with pytest.raises(HTTPException) as error:
        authorize(wrapper)
    assert error.value.status_code == 410
    assert money_snapshot(store, database, key) == before
    if first is None:
        first = authorize(base)
    before = money_snapshot(store, database, key)
    retry = authorize(base)
    assert retry["authorization_id"] == first["authorization_id"]
    assert retry["idempotent_replay"] is True
    assert money_snapshot(store, database, key) == before


@pytest.mark.parametrize("backend", ["memory", "typed", "legacy"])
@pytest.mark.parametrize("kind", ["custom", "user"])
@pytest.mark.parametrize("field", [
    "custom_model_revision", "custom_model_id", "user_provided_model_id", "user_provided_model_revision",
])
def test_explicit_creator_identity_rejected_even_with_catalog_model(backend, kind, field):
    store, database, key, model, body = seed_model(kind, backend=backend)
    body["model"] = "bytedance/seedance-2.5"
    body[field] = model.revision if field.endswith("revision") else model.id
    before = money_snapshot(store, database, key)
    with pytest.raises(HTTPException) as error:
        authorize(body)
    assert error.value.status_code == 410
    assert money_snapshot(store, database, key) == before


@pytest.mark.parametrize("kind,drift", [
    ("custom", "disabled"), ("user", "disabled"),
    ("user", "inactive"), ("user", "off-clock"),
    ("user", "dispatch-disabled"),
])
@pytest.mark.parametrize("resolution", [None, "1080p"])
def test_retirement_errors_before_replay(client, monkeypatch, kind, drift, resolution):
    store, _, key, model, body = seed_model(kind)
    if resolution:
        body["video_resolution"] = resolution
    before = money_snapshot(store, None, key)
    if drift == "disabled":
        model.enabled = False
    elif drift == "inactive":
        model.status = "inactive"
    elif drift == "off-clock":
        model.online = False
    else:
        monkeypatch.setattr(client.app.state.settings, "user_models_dispatch_enabled", False)

    def forbidden(*args, **kwargs):
        pytest.fail("unavailable model must fail before replay or fingerprinting")

    monkeypatch.setattr(InMemoryStore, "get_gateway_authorization_by_idempotency_key", forbidden)
    monkeypatch.setattr(gateway, "_gateway_authorize_fingerprint", forbidden)
    no_reservation(monkeypatch)
    response = client.post(URL, json=body)
    assert response.status_code == 410, response.text
    assert response.json()["error"]["type"] == "model_not_supported"
    assert money_snapshot(store, None, key) == before


@pytest.mark.parametrize("kind", ["custom", "user"])
@pytest.mark.parametrize("backend", ["memory", "typed", "legacy"])
def test_creator_video_derived_fields_cannot_bypass_retirement(backend, kind):
    store, database, key, _, body = seed_model(kind, backend=backend)
    before = money_snapshot(store, database, key)
    with pytest.raises(HTTPException) as error:
        authorize({**body, "max_tokens": 400_000, "video_resolution": "1080p",
                   "additional_cost_reservation_microdollars": 1_200_000, "region": "europe-west4"})
    assert error.value.status_code == 410
    assert money_snapshot(store, database, key) == before


@pytest.mark.parametrize("kind", ["custom", "user"])
@pytest.mark.parametrize("backend", ["memory", "typed", "legacy"])
def test_creator_requests_reject_before_live_resolution_or_replay(monkeypatch, kind, backend):
    for resolution in (None, "1080p"):
        with monkeypatch.context() as patch:
            store, database, key, _, body = seed_model(kind, backend=backend)
            if resolution:
                body["video_resolution"] = resolution
            before = money_snapshot(store, database, key)
            lookup = ("get_gateway_authorization_by_idempotency_key" if backend == "memory"
                      else "get_typed_authorization_by_idempotency")
            hooks = [
                (type(store), "get_custom_model" if kind == "custom" else "get_user_model"),
                (gateway, "_gateway_authorize_fingerprint"),
                (type(store), lookup),
            ]
            for target, name in hooks:
                def forbidden(*args, _name=name, **kwargs):
                    pytest.fail(f"retired request reached {_name}")
                patch.setattr(target, name, forbidden)
            with pytest.raises(HTTPException) as error:
                authorize(body)
            assert error.value.status_code == 410
            assert money_snapshot(store, database, key) == before


@pytest.mark.parametrize("model", ["tr-custom-model/missing", "tr-user-model/missing"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_replay_lookup_rejects_creator_ids_before_all_store_reads(monkeypatch, model, authenticated):
    client = TestClient(_app("internal", internal_gateway_token="test-secret"))  # noqa: S106

    class NoReads:
        def __getattr__(self, name):
            pytest.fail(f"creator replay lookup accessed store: {name}")

    monkeypatch.setattr(gateway, "STORE", NoReads())
    monkeypatch.setattr(video_jobs, "STORE", NoReads())
    body = {"model": model, "route_type": "videos", "api_key_lookup_hash": "missing",
            "idempotency_key": "missing", "request_fingerprint": "a" * 64}
    headers = {"x-trustedrouter-internal-token": "test-secret"} if authenticated else {}
    response = client.post(LOOKUP, json=body, headers=headers)
    assert response.status_code == (400 if authenticated else 401), response.text
    if authenticated:
        assert response.json()["error"]["type"] == "bad_request"


NON_VIDEO_ROUTES = [None, "chat.completions", "responses", "messages", "embeddings", "images",
                    "decide", "batch.native.openai", gateway.POLYPHEMUS_SELECT_ROUTE_TYPE,
                    PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE,
                    PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX + "chat"]


@pytest.mark.parametrize("route", NON_VIDEO_ROUTES)
@pytest.mark.parametrize("case", ["tags", "batch-key", "idempotency"])
def test_nonvideo_main_error_precedence(client, route, case):
    _, _, _, _, body = seed_model("user")
    body.update(route_type=route, model="tr-custom-model/missing")
    expected = (410, "model_not_supported")
    headers = {}
    if case == "tags":
        body.update(tags={"trustedrouter:invalid": "reserved"}, http_referer="invalid-url")
        expected = (400, "invalid_tags")
    elif case == "batch-key":
        body["idempotency_key"] = "tr-native-batch:bad-route"
    else:
        body.pop("idempotency_key")
        headers = {"idempotency-key": "x" * 257}
    actual = client.post(URL, json=body, headers=headers)
    assert (actual.status_code, actual.json()["error"]["type"]) == expected, actual.text
