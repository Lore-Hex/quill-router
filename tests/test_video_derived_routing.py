"""Derived capability constraints never change video identity or duplicate money."""
from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.routing import video_route_endpoint_candidates
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_gcp import SpannerStore
from trusted_router.storage_models import UserProvidedModel

HEADER = "X-Quill-Video-Allowed-Providers"
MODEL = "bytedance/seedance-2.5"
URL = "/v1/internal/gateway/authorize"


def payload(key, **changes):
    return {
        "api_key_lookup_hash": lookup_hash_api_key(key), "model": MODEL,
        "route_type": "videos", "estimated_input_tokens": 0, "max_tokens": 300_000,
        "idempotency_key": "derived-video", "request_fingerprint": "a" * 64,
        **changes,
    }


def request(header=None):
    return Request({"type": "http", "method": "POST", "path": URL,
                    "headers": [] if header is None else [(HEADER.lower().encode(), header.encode())]})


def no_reservation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("rejected routing must not reserve funds or key headroom")
    monkeypatch.setattr(InMemoryStore, "reserve", forbidden)
    monkeypatch.setattr(InMemoryStore, "reserve_key_limit", forbidden)


@pytest.mark.parametrize("value", [
    "", " ", ",", "byteplus,", ",byteplus", "byteplus,,fal", "byteplus,byteplus",
    "BytePlus", "google-ai", "gemini", "unknown-provider", "byteplus;fal",
    "byteplus/fal", "byte plus", "byteplus\n", "x" * 4097,
    ",".join(["byteplus"] * 65),
])
def test_invalid_header_rejected_before_money(client, inference_key, monkeypatch, value):
    no_reservation(monkeypatch)
    response = client.post(URL, json=payload(inference_key), headers={HEADER: value})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "bad_request"


def test_repeated_header_rejected(client, inference_key, monkeypatch):
    no_reservation(monkeypatch)
    response = client.post(URL, json=payload(inference_key),
                           headers=[(HEADER, "byteplus"), (HEADER, "fal")])
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("route_type", ["images", "chat.completions", None])
def test_header_rejected_on_nonvideo(client, inference_key, monkeypatch, route_type):
    no_reservation(monkeypatch)
    response = client.post(URL, json=payload(inference_key, route_type=route_type),
                           headers={HEADER: "byteplus"})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "bad_request"


@pytest.mark.parametrize("fallbacks", [True, False])
@pytest.mark.parametrize("allowed", ["byteplus", "venice"])
def test_primary_fallback_and_frozen_routes_obey_header(client, inference_key, fallbacks, allowed):
    excluded = "venice" if allowed == "byteplus" else "byteplus"
    body = payload(inference_key, provider={"order": [excluded, allowed], "allow_fallbacks": fallbacks})
    baseline = client.post(URL, json={**body, "idempotency_key": "unconstrained"})
    assert baseline.status_code == 200, baseline.text
    assert baseline.json()["data"]["provider"] == excluded
    response = client.post(URL, json=body, headers={HEADER: f" \t{allowed}\t "})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["provider"] == allowed
    assert {c["provider"] for c in data["route_candidates"]} == {allowed}
    auth = STORE.get_gateway_authorization(data["authorization_id"])
    assert {MODEL_ENDPOINTS[e].provider for e in auth.candidate_endpoint_ids} == {allowed}
    frozen = json.loads(auth.video_pricing_snapshot)["candidates"]
    assert {MODEL_ENDPOINTS[c["endpoint_id"]].provider for c in frozen} == {allowed}
    unconstrained = STORE.get_gateway_authorization(baseline.json()["data"]["authorization_id"])
    assert auth.idempotency_fingerprint == unconstrained.idempotency_fingerprint


@pytest.mark.parametrize("provider", [
    {"only": ["venice"]}, {"ignore": ["byteplus"]},
    {"order": ["venice"], "allow_fallbacks": False},
    {"only": ["venice"], "data_collection": "deny"},
])
def test_empty_intersection_reserves_nothing(client, inference_key, monkeypatch, provider):
    no_reservation(monkeypatch)
    response = client.post(URL, json=payload(inference_key, provider=provider), headers={HEADER: "byteplus"})
    assert response.status_code == 400, response.text


def test_missing_capable_route_reserves_nothing(client, inference_key, monkeypatch):
    no_reservation(monkeypatch)
    response = client.post(URL, json=payload(inference_key), headers={HEADER: "fal"})
    assert response.status_code == 400, response.text


def test_resolver_filters_before_first_choice():
    candidates = video_route_endpoint_candidates(
        {"model": MODEL, "provider": {"order": ["venice", "byteplus"], "allow_fallbacks": False}},
        Settings(environment="test"), allowed_providers=frozenset({"byteplus"}),
    )
    assert len(candidates) == 1 and candidates[0][1].provider == "byteplus"


@pytest.mark.parametrize("first_header", [None, "byteplus"])
@pytest.mark.parametrize("drift", ["capability", "removed"])
def test_header_rollout_replay_before_live_filters(client, inference_key, monkeypatch, first_header, drift):
    body = payload(inference_key, provider={"only": ["byteplus"]})
    first = client.post(URL, json=body, headers={} if first_header is None else {HEADER: first_header})
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    auth = copy.deepcopy(STORE.get_gateway_authorization(data["authorization_id"]))
    before = copy.deepcopy(STORE.get_credit_account(auth.workspace_id))
    if drift == "capability":
        monkeypatch.setitem(MODELS, MODEL, replace(MODELS[MODEL], supports_video=False))
    else:
        for endpoint_id, endpoint in tuple(MODEL_ENDPOINTS.items()):
            if endpoint.model_id == MODEL:
                monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
        monkeypatch.delitem(MODELS, MODEL)
    no_reservation(monkeypatch)
    for headers in ({HEADER: "venice"}, {HEADER: "fal,byteplus"}, {}):
        replay = client.post(URL, json={**body, "region": "europe-west4"}, headers=headers)
        assert replay.status_code == 200, replay.text
        recovered = replay.json()["data"]
        assert recovered["idempotent_replay"] is True
        for field in ("authorization_id", "credit_reservation_id", "endpoint_id", "region"):
            assert recovered[field] == data[field]
        assert {c["endpoint_id"] for c in recovered["route_candidates"]} == set(auth.candidate_endpoint_ids)
        assert STORE.get_gateway_authorization(auth.id) == auth
        assert STORE.get_credit_account(auth.workspace_id) == before
    # Prompt/seed changes arrive as a changed enclave HMAC; original policy is
    # still identity even if the new capability header would exclude everything.
    for change in ({"request_fingerprint": "b" * 64}, {"request_fingerprint": "c" * 64},
                   {"provider": {"only": ["venice"]}}):
        conflict = client.post(URL, json={**body, **change}, headers={HEADER: "fal"})
        assert conflict.status_code == 409, conflict.text
    missing = client.post("/v1/internal/gateway/video/jobs/missing-video-job/lookup",
                          json={"api_key_lookup_hash": body["api_key_lookup_hash"]})
    assert missing.status_code == 404


def test_legacy_hash_is_unchanged_and_resolution_is_video_only():
    body = {"route_type": "videos", "request_fingerprint": "a" * 64,
            "provider": {"only": ["byteplus"]}}
    expected = hashlib.sha256(json.dumps(
        {**body, "workspace_id": "ws", "key_hash": "key"}, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    def fingerprint(b):
        return gateway._gateway_authorize_fingerprint(workspace_id="ws", key_hash="key", body=b)
    assert fingerprint(body) == expected
    assert fingerprint({**body, "video_resolution": "1080p"}) == expected
    assert fingerprint({**body, "video_resolution": "720p", "request_fingerprint": "b" * 64}) != expected
    assert fingerprint({"route_type": "images"}) != fingerprint({"route_type": "images", "video_resolution": "720p"})


@pytest.mark.parametrize("changed_identity", [False, True])
def test_concurrent_first_requests_keep_one_frozen_hold(monkeypatch, changed_identity):
    store, database, key = _seed_typed_gateway_store()
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model=MODEL, route_type="videos", max_output_tokens=300_000,
        estimated_input_tokens=0, idempotency_key="race", request_fingerprint="a" * 64,
    )
    settings = Settings(environment="test")
    barrier = Barrier(2)
    transaction_lock = Lock()
    real_atomic = SpannerStore.authorize_gateway_typed
    real_lookup = SpannerStore.get_typed_authorization_by_idempotency
    lookup_lock = Lock()
    lookup_count = 0
    snapshots = []

    def miss(self, *args):
        nonlocal lookup_count
        with lookup_lock:
            lookup_count += 1
            first_lookup = lookup_count <= 2
        if first_lookup:
            barrier.wait(timeout=20)
            return None
        return real_lookup(self, *args)

    def serialized_transaction(self, *args, **kwargs):
        # The fake DB has no isolation; serialize only its transaction, after
        # both real requests have missed the early indexed lookup.
        with transaction_lock:
            result = real_atomic(self, *args, **kwargs)
            snapshots.append(copy.deepcopy(database.typed))
            return result

    monkeypatch.setattr(SpannerStore, "get_typed_authorization_by_idempotency", miss)
    monkeypatch.setattr(SpannerStore, "authorize_gateway_typed", serialized_transaction)

    def run(header, fingerprint):
        try:
            return gateway._authorize_gateway_sync(
                request(header), body.model_copy(update={"request_fingerprint": fingerprint}), settings,
            )["data"]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(run, "byteplus", "a" * 64),
                 pool.submit(run, "venice", ("b" if changed_identity else "a") * 64)]
        results = [call.result(timeout=30) for call in calls]
    successes = [r for r in results if isinstance(r, dict)]
    winner = next(r for r in successes if not r["idempotent_replay"])
    if changed_identity:
        assert len(successes) == 1 and 409 in results
    else:
        assert len(successes) == 2
        assert sum(r["idempotent_replay"] for r in successes) == 1
        for response in successes:
            assert response["authorization_id"] == winner["authorization_id"]
            assert response["credit_reservation_id"] == winner["credit_reservation_id"]
            assert response["route_candidates"] == winner["route_candidates"]
    assert len(snapshots) == 2 and snapshots[0] == snapshots[1]
    assert list(database.reservations) == [winner["credit_reservation_id"]]
    auth = store.get_gateway_authorization(winner["authorization_id"])
    assert {MODEL_ENDPOINTS[e].provider for e in auth.candidate_endpoint_ids} == {winner["provider"]}


def test_header_bounds_are_independent_of_member_validation():
    known = sorted(PROVIDERS)[:65]
    assert len(known) == 65
    assert len(gateway._video_allowed_providers_header(request(",".join(known[:64])), "videos")) == 64
    for header in (",".join(known), " " * 4096 + "byteplus"):
        with pytest.raises(HTTPException) as error:
            gateway._video_allowed_providers_header(request(header), "videos")
        assert error.value.status_code == 400


@pytest.mark.parametrize("first_resolution", [None, "720p"])
def test_typed_resolution_and_header_rollout_preserve_money(first_resolution):
    store, database, key = _seed_typed_gateway_store()
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model=MODEL, route_type="videos", max_output_tokens=300_000,
        estimated_input_tokens=0, idempotency_key="old-enclave", request_fingerprint="a" * 64,
        video_resolution=first_resolution, provider={"only": ["byteplus"]},
    )
    settings = Settings(environment="test")
    first = gateway._authorize_gateway_sync(request(), body, settings)["data"]
    auth = copy.deepcopy(store.get_gateway_authorization(first["authorization_id"]))
    before = copy.deepcopy(database.typed)
    retry = body.model_copy(update={"video_resolution": "1080p", "region": "europe-west4"})
    replay = gateway._authorize_gateway_sync(request("venice"), retry, settings)["data"]
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["credit_reservation_id"] == first["credit_reservation_id"]
    assert replay["idempotent_replay"] is True
    assert replay.get("video_tariff_resolution") == first_resolution
    assert store.get_gateway_authorization(auth.id) == auth
    assert database.typed == before
    with pytest.raises(HTTPException) as error:
        gateway._authorize_gateway_sync(
            request("venice"), retry.model_copy(update={"request_fingerprint": "b" * 64}), settings,
        )
    assert error.value.status_code == 409
    assert database.typed == before


def test_owner_dispatch_cannot_bypass_video_constraints(monkeypatch):
    _store, database, key = _seed_typed_gateway_store()
    owner_model = UserProvidedModel(
        id="tr-user-model/owner-demo", owner_user_id="owner", owner_workspace_id="ws-rpc",
        name="Demo", kind="machine", status="active",
    )
    monkeypatch.setattr(SpannerStore, "get_user_model", lambda *args: owner_model)
    monkeypatch.setattr(gateway, "user_model_is_on_the_clock", lambda *args: True)
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model=owner_model.id, route_type="videos",
        max_output_tokens=1, idempotency_key="owner-video", request_fingerprint="a" * 64,
    )
    before = copy.deepcopy(database.typed)
    with pytest.raises(HTTPException) as error:
        gateway._authorize_gateway_sync(
            request("byteplus"), body, Settings(environment="test", user_models_dispatch_enabled=True),
        )
    assert error.value.status_code == 400
    assert "catalog video routes" in error.value.detail["error"]["message"]
    assert database.typed == before
