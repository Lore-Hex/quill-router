"""Enclave execution estimates may drift; logical identity and money may not."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from tests.test_video_derived_routing import MODEL, URL, no_reservation, payload, request
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import STORE
from trusted_router.storage_gcp import SpannerStore

TOKEN_FIELDS = ("max_tokens", "max_output_tokens", "max_completion_tokens")


def main_fingerprint(*, workspace_id, key_hash, body, idempotency_key=None):
    # Frozen independently from origin/main's real fingerprint implementation
    # (702bbf78). Do not call the production helper: that would hide regressions.
    material = {k: v for k, v in body.items() if k not in {
        "api_key_hash", "api_key_lookup_hash", "idempotency_key", "spend_lease_admission",
    }}
    material.update(workspace_id=workspace_id, key_hash=key_hash)
    return hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def resolution_only_fingerprint(**kwargs):
    body = dict(kwargs.pop("body"))
    body.pop("video_resolution", None)
    return main_fingerprint(body=body, **kwargs)


@pytest.mark.parametrize("version", ["new", "main", "resolution-only"])
@pytest.mark.parametrize("first_resolution", [None, "1080p"])
@pytest.mark.parametrize("fields", [TOKEN_FIELDS[:1], TOKEN_FIELDS[:2], TOKEN_FIELDS[1:2],
                                    TOKEN_FIELDS[2:], TOKEN_FIELDS])
def test_typed_execution_rollout_preserves_authorization_and_all_money(
    monkeypatch, version, first_resolution, fields,
):
    store, database, key = _seed_typed_gateway_store()
    settings = Settings(environment="test")
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model=MODEL, route_type="videos", estimated_input_tokens=0,
        idempotency_key="four-second-1080p", request_fingerprint="a" * 64,
        video_resolution=first_resolution, additional_cost_reservation_microdollars=900_000,
        provider={"order": ["venice", "byteplus"]}, **dict.fromkeys(fields, 1),
    )
    # Simulate the actual old writer, including main's resolution-bearing hash.
    with monkeypatch.context() as old:
        if version != "new":
            old.setattr(gateway, "_gateway_authorize_fingerprint", {
                "main": main_fingerprint, "resolution-only": resolution_only_fingerprint,
            }[version])
        first = gateway._authorize_gateway_sync(request("venice"), body, settings)["data"]
    assert first["provider"] == "venice"
    authorization = copy.deepcopy(store.get_gateway_authorization(first["authorization_id"]))
    assert json.loads(authorization.video_pricing_snapshot)["output_token_limit"] == 1
    before = copy.deepcopy(database.typed)

    def forbidden(*args, **kwargs):
        pytest.fail("replay must not enter the reservation transaction")
    monkeypatch.setattr(SpannerStore, "authorize_gateway_typed", forbidden)
    retry = body.model_copy(update={
        **dict.fromkeys(TOKEN_FIELDS, 400_000), "video_resolution": "1080p",
        "additional_cost_reservation_microdollars": 1_200_000, "region": "europe-west4",
    })
    replay = gateway._authorize_gateway_sync(request("byteplus"), retry, settings)["data"]
    assert replay["idempotent_replay"] is True
    for field in ("authorization_id", "credit_reservation_id", "route_candidates", "region"):
        assert replay[field] == first[field]
    assert replay.get("video_tariff_resolution") == first_resolution
    assert store.get_gateway_authorization(authorization.id) == authorization
    assert database.typed == before
    for change in (
        {"request_fingerprint": "b" * 64}, {"request_fingerprint": None},
        {"model": "minimax/hailuo-3"}, {"provider": {"only": ["byteplus"]}},
    ):
        with pytest.raises(HTTPException) as error:
            gateway._authorize_gateway_sync(request(), retry.model_copy(update=change), settings)
        assert error.value.status_code == 409
        assert database.typed == before
    fingerprint_body = retry.model_dump(exclude_none=True)
    fingerprint_body.pop("inference_receipt")  # Authorize omits the false default.
    assert gateway._video_cross_region_replay_matches(
        authorization, authorization.workspace_id, key.hash, fingerprint_body, body.idempotency_key,
    )
    for workspace, key_hash, idem in (
        ("other-workspace", key.hash, body.idempotency_key),
        (authorization.workspace_id, "other-key", body.idempotency_key),
        (authorization.workspace_id, key.hash, "other-idempotency-key"),
    ):
        assert not gateway._video_cross_region_replay_matches(
            authorization, workspace, key_hash, fingerprint_body, idem,
        )


@pytest.mark.parametrize("legacy", [False, True])
def test_memory_execution_rollout_replays_without_reservation(client, inference_key, monkeypatch, legacy):
    body = payload(inference_key, max_tokens=1, max_output_tokens=1,
                   provider={"order": ["venice", "byteplus"]},
                   additional_cost_reservation_microdollars=900_000)
    with monkeypatch.context() as old:
        if legacy:
            old.setattr(gateway, "_gateway_authorize_fingerprint", main_fingerprint)
        first = client.post(URL, json=body, headers={"X-Quill-Video-Allowed-Providers": "venice"})
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    auth = copy.deepcopy(STORE.get_gateway_authorization(data["authorization_id"]))
    account = copy.deepcopy(STORE.get_credit_account(auth.workspace_id))
    no_reservation(monkeypatch)
    retry = client.post(URL, json={**body, "max_tokens": 400_000, "max_output_tokens": 400_000,
                                  "video_resolution": "1080p"})
    assert retry.status_code == 200, retry.text
    replay = retry.json()["data"]
    assert replay["idempotent_replay"] is True
    assert replay["authorization_id"] == auth.id
    assert replay["credit_reservation_id"] == auth.credit_reservation_id
    assert STORE.get_gateway_authorization(auth.id) == auth
    assert STORE.get_credit_account(auth.workspace_id) == account


@pytest.mark.parametrize("field", (*TOKEN_FIELDS, "video_resolution", "additional_cost_reservation_microdollars"))
def test_execution_fields_excluded_only_for_videos(field):
    for route in ("videos", "images", "chat.completions"):
        body = {"route_type": route, "request_fingerprint": "a" * 64, "model": MODEL}
        def fingerprint(b):
            return gateway._gateway_authorize_fingerprint(workspace_id="ws", key_hash="key", body=b)
        changed = {**body, field: "1080p" if field == "video_resolution" else 400_000}
        assert (fingerprint(body) == fingerprint(changed)) == (route == "videos")


def test_legacy_compatibility_after_transactional_lookup_miss(monkeypatch):
    store, database, key = _seed_typed_gateway_store()
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model=MODEL, route_type="videos", max_tokens=1, max_output_tokens=1,
        additional_cost_reservation_microdollars=900_000, estimated_input_tokens=0,
        idempotency_key="rolling-writer-race", request_fingerprint="a" * 64,
    )
    settings = Settings(environment="test")
    with monkeypatch.context() as old:
        old.setattr(gateway, "_gateway_authorize_fingerprint", main_fingerprint)
        first = gateway._authorize_gateway_sync(request("venice"), body, settings)["data"]
    before = copy.deepcopy(database.typed)
    real_lookup = SpannerStore.get_typed_authorization_by_idempotency
    calls = 0
    def miss_once(self, *args):
        nonlocal calls
        calls += 1
        return None if calls == 1 else real_lookup(self, *args)
    monkeypatch.setattr(SpannerStore, "get_typed_authorization_by_idempotency", miss_once)
    retry = body.model_copy(update={"max_tokens": 400_000, "max_output_tokens": 400_000,
                                    "video_resolution": "1080p"})
    replay = gateway._authorize_gateway_sync(request("byteplus"), retry, settings)["data"]
    assert calls == 2
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["credit_reservation_id"] == first["credit_reservation_id"]
    assert replay["idempotent_replay"] is True
    assert database.typed == before
    assert store.get_gateway_authorization(first["authorization_id"]) is not None


def test_legacy_without_snapshot_uses_fixed_quote_sentinel():
    body = {"route_type": "videos", "model": MODEL, "max_tokens": 1, "max_output_tokens": 1,
            "request_fingerprint": "a" * 64}
    auth = SimpleNamespace(workspace_id="ws", key_hash="key", idempotency_key="retry", region="us",
                           idempotency_fingerprint=main_fingerprint(workspace_id="ws", key_hash="key", body=body))
    retry = {**body, "max_tokens": 400_000, "max_output_tokens": 400_000, "video_resolution": "1080p"}
    assert gateway._video_cross_region_replay_matches(auth, "ws", "key", retry, "retry")
    assert not gateway._video_cross_region_replay_matches(
        auth, "ws", "key", {**retry, "request_fingerprint": "b" * 64}, "retry",
    )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("limit", [1, 123_456])
def test_execution_replay_scope_and_frozen_limit(legacy, limit):
    body = {"route_type": "videos", "model": MODEL, **dict.fromkeys(TOKEN_FIELDS, limit),
            "video_resolution": "720p", "request_fingerprint": "a" * 64,
            "provider": {"only": ["venice"]}, "region": "us-central1"}
    fingerprint = main_fingerprint if legacy else gateway._gateway_authorize_fingerprint
    auth = SimpleNamespace(
        workspace_id="ws", key_hash="key", idempotency_key="retry", region="us-central1",
        video_pricing_snapshot=json.dumps({"output_token_limit": limit, "video_tariff_resolution": "720p"}),
        idempotency_fingerprint=fingerprint(workspace_id="ws", key_hash="key", body=body),
    )
    retry = {**body, **dict.fromkeys(TOKEN_FIELDS, 400_000), "video_resolution": "1080p",
             "region": "europe-west4"}
    matches = gateway._video_cross_region_replay_matches
    assert matches(auth, "ws", "key", retry, "retry")
    for workspace, key_hash, idem in (("other", "key", "retry"), ("ws", "other", "retry"),
                                      ("ws", "key", "other")):
        assert not matches(auth, workspace, key_hash, retry, idem)
