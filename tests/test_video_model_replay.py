"""Literal main compatibility fixtures and main-equivalent creator-model paths."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.generate_gateway_authorizations import (
    authorize,
    money_snapshot,
    observe_inconsistent_identity,
    observe_race,
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
        assert response.status_code == retry["current_status"], response.text
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


@pytest.mark.parametrize("backend", ["memory", "typed", "legacy"])
@pytest.mark.parametrize("reverse", [False, True])
def test_wrapper_base_race_equals_recorded_main(monkeypatch, backend, reverse):
    # Main treats the prepared bodies as equal in both directions. In typed
    # modes this exercises actual transaction REPLAY after the early miss.
    assert observe_race(monkeypatch, backend, reverse) == RECORDS["observations"][f"race/{backend}/{reverse}"]


@pytest.mark.parametrize("backend", ["memory", "typed", "legacy"])
@pytest.mark.parametrize("kind", ["custom", "user"])
@pytest.mark.parametrize("field", ["custom_model_revision", "custom_model_id"])
def test_inconsistent_explicit_identity_equals_recorded_main(backend, kind, field):
    assert observe_inconsistent_identity(kind, field, backend) == RECORDS["observations"][f"identity/{backend}/{kind}/{field}"]


@pytest.mark.parametrize("kind,drift,status", [
    ("custom", "disabled", 404), ("user", "disabled", 404),
    ("user", "inactive", 404), ("user", "off-clock", 503),
    ("user", "dispatch-disabled", 404),
])
@pytest.mark.parametrize("resolution", [None, "1080p"])
def test_unavailable_model_errors_before_replay(client, monkeypatch, kind, drift, status, resolution):
    store, _, key, model, body = seed_model(kind)
    if resolution:
        body["video_resolution"] = resolution
    first = authorize(body)
    auth = copy.deepcopy(store.get_gateway_authorization(first["authorization_id"]))
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
    assert response.status_code == status, response.text
    assert response.json()["error"]["type"] == ("model_off_the_clock" if status == 503 else "not_found")
    assert store.get_gateway_authorization(auth.id) == auth
    assert money_snapshot(store, None, key) == before


@pytest.mark.parametrize("kind", ["custom", "user"])
@pytest.mark.parametrize("backend", ["memory", "typed", "legacy"])
def test_creator_video_derived_fields_still_replay(backend, kind):
    store, database, key, _, body = seed_model(kind, backend=backend)
    first = authorize(body)
    before = money_snapshot(store, database, key)
    retry = authorize({**body, "max_tokens": 400_000, "video_resolution": "1080p",
                       "additional_cost_reservation_microdollars": 1_200_000, "region": "europe-west4"})
    assert retry["idempotent_replay"] is True
    assert retry["authorization_id"] == first["authorization_id"]
    assert money_snapshot(store, database, key) == before


@pytest.mark.parametrize("kind", ["custom", "user"])
@pytest.mark.parametrize("backend", ["memory", "typed", "legacy"])
@pytest.mark.parametrize("resolution", [None, "1080p"])
def test_creator_retry_uses_only_main_late_replay(monkeypatch, kind, backend, resolution):
    store, _, _, _, body = seed_model(kind, backend=backend)
    if resolution:
        body["video_resolution"] = resolution
    first = authorize(body)
    events = []
    route_name = "video_route_endpoint_candidates" if kind == "custom" else "_user_model_gateway_candidate"
    original_route = getattr(gateway, route_name)

    def route(*args, **kwargs):
        events.append("route")
        return original_route(*args, **kwargs)

    monkeypatch.setattr(gateway, route_name, route)
    method = "get_gateway_authorization_by_idempotency_key" if backend == "memory" else "authorize_gateway_typed"
    original_replay = getattr(type(store), method)

    def late_replay(self, *args, **kwargs):
        assert events == ["route"]
        events.append("late-replay")
        return original_replay(self, *args, **kwargs)

    monkeypatch.setattr(type(store), method, late_replay)
    if backend != "memory":
        def forbidden(*args, **kwargs):
            pytest.fail("identical creator retry must go directly through typed admission")
        monkeypatch.setattr(type(store), "get_typed_authorization_by_idempotency", forbidden)
    retry = authorize(body)
    assert events == ["route", "late-replay"]
    assert retry["idempotent_replay"] is True
    assert retry["authorization_id"] == first["authorization_id"]


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
    actual = client.post(URL, json=body, headers=headers)
    assert (actual.status_code, actual.json()["error"]["type"]) == expected, actual.text
