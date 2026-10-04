from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.test_gateway_authorize_spanner_operations import _request, _seed_typed_gateway_store
from tests.test_video_generation_control_plane import _authorize_video
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import STORE
from trusted_router.storage_gcp import SpannerStore


@pytest.mark.parametrize("changed", [
    {"provider": {"region": "us"}},
    {"provider": {"min_privacy": "confidential"}},
    {"request_fingerprint": "b" * 64},
    {"request_fingerprint": None},
    {"route_type": "images"},
    {"route_type": "chat"},
    {"tags": {"team": "different"}},
])
def test_cross_region_compatibility_never_relaxes_customer_fields(changed) -> None:
    body = {"route_type": "videos", "request_fingerprint": "a" * 64, "region": "us-central1"}
    authorization = SimpleNamespace(
        workspace_id="workspace", key_hash="key", idempotency_key="retry",
        region="us-central1", idempotency_fingerprint=gateway._gateway_authorize_fingerprint(
            workspace_id="workspace", key_hash="key", body=body, idempotency_key="retry",
        ),
    )
    retry = {**body, "region": "europe-west4"}
    matches = gateway._video_cross_region_replay_matches
    assert matches(authorization, "workspace", "key", retry, "retry")
    assert not matches(authorization, "workspace", "key", {**retry, **changed}, "retry")
    assert not matches(authorization, "other-workspace", "key", retry, "retry")
    assert not matches(authorization, "workspace", "other-key", retry, "retry")
    assert not matches(authorization, "workspace", "key", retry, "other-retry")
    assert not matches(None, "workspace", "key", retry, "retry")


@pytest.mark.parametrize("first_region", [None, "us-central1"])
def test_video_cross_region_replay_keeps_original_hold(
    client: TestClient, inference_key: str, first_region: str | None,
) -> None:
    first = _authorize_video(client, inference_key, region=first_region)
    auth = STORE.get_gateway_authorization(str(first["authorization_id"]))
    assert auth is not None
    account_before = copy.deepcopy(STORE.get_credit_account(auth.workspace_id))
    replay = _authorize_video(client, inference_key, region="europe-west4", quote=900_000)
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["credit_reservation_id"] == first["credit_reservation_id"]
    assert replay["idempotent_replay"] is True
    assert STORE.get_credit_account(auth.workspace_id) == account_before


@pytest.mark.parametrize("first_region", [None, "us-central1"])
def test_typed_video_cross_region_replay_preserves_all_money_rows(
    monkeypatch: pytest.MonkeyPatch, first_region: str | None,
) -> None:
    store, database, key = _seed_typed_gateway_store()
    settings = Settings(environment="test")
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model="minimax/hailuo-3", route_type="videos",
        estimated_input_tokens=0, max_output_tokens=1,
        additional_cost_reservation_microdollars=850_500,
        idempotency_key="video-region-retry", request_fingerprint="a" * 64,
        region=first_region,
    )
    real_lookup = SpannerStore.get_typed_authorization_by_idempotency
    lookups = []

    def lookup(self, *args):
        lookups.append(args)
        return real_lookup(self, *args)

    monkeypatch.setattr(SpannerStore, "get_typed_authorization_by_idempotency", lookup)
    first = gateway._authorize_gateway_sync(_request(), body, settings)["data"]
    assert not lookups  # No new happy-path RPCs.
    before = copy.deepcopy(database.typed)
    changed = body.model_copy(update={"region": "europe-west4"})
    replay = gateway._authorize_gateway_sync(_request(), changed, settings)["data"]
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["credit_reservation_id"] == first["credit_reservation_id"]
    assert replay["idempotent_replay"] is True
    assert len(lookups) == 1
    assert database.typed == before
    for update in (
        {"request_fingerprint": "b" * 64},
        {"provider": {"only": [first["provider"]]}},
        {"max_output_tokens": 2},
        {"request_fingerprint": None},
    ):
        with pytest.raises(HTTPException) as error:
            gateway._authorize_gateway_sync(_request(), changed.model_copy(update=update), settings)
        assert error.value.status_code == 409
        assert database.typed == before
    assert store.get_gateway_authorization(first["authorization_id"]) is not None


@pytest.mark.parametrize("refund", [False, True])
def test_finalized_video_replay_returns_existing_job_only(
    client: TestClient, inference_key: str, refund: bool,
) -> None:
    auth = _authorize_video(client, inference_key)
    body = {
        "job_id": "job-finalized-replay", "authorization_id": auth["authorization_id"],
        "model": "minimax/hailuo-3", "provider": auth["provider"],
        "endpoint_id": auth["endpoint_id"], "provider_model": "MiniMax-H3",
        "quoted_microdollars": 850_500,
    }
    first = client.post("/v1/internal/gateway/video/jobs/prepare", json=body)
    assert first.status_code == 200, first.text
    final = client.post(
        "/v1/internal/gateway/refund" if refund else "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"], "route_type": "videos",
            "actual_input_tokens": 0, "actual_output_tokens": 0,
            "selected_endpoint": auth["endpoint_id"],
            **({"status_code": 502, "error_type": "provider_error"} if refund else
               {"additional_cost_microdollars": 850_500}),
        },
    )
    assert final.status_code == 200, final.text
    authorization = STORE.get_gateway_authorization(str(auth["authorization_id"]))
    assert authorization is not None and authorization.settled
    before = copy.deepcopy(STORE.get_credit_account(authorization.workspace_id))
    replay = client.post("/v1/internal/gateway/video/jobs/prepare", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["id"] == body["job_id"]
    assert replay.json()["data"]["created"] is False
    for update in ({"job_id": "job-must-not-create"}, {"model": "bytedance/seedance-2.5"}):
        rejected = client.post("/v1/internal/gateway/video/jobs/prepare", json={**body, **update})
        assert rejected.status_code == 409, rejected.text
    assert STORE.get_video_job("job-must-not-create") is None
    assert STORE.get_credit_account(authorization.workspace_id) == before


@pytest.mark.parametrize("strict", [False, True])
def test_cross_region_video_replay_survives_consumed_daily_budget(strict: bool) -> None:
    store, database, key = _seed_typed_gateway_store()
    store.update_key(key.hash, {
        "limit_daily_microdollars": 1_000_000, "budget_strict": strict,
    })
    settings = Settings(environment="test")
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model="minimax/hailuo-3", route_type="videos",
        estimated_input_tokens=0, max_output_tokens=1,
        additional_cost_reservation_microdollars=850_500,
        idempotency_key="video-window-retry", request_fingerprint="a" * 64,
        region="us-central1",
    )
    first = gateway._authorize_gateway_sync(_request(), body, settings)["data"]
    assert store.typed_finalize_gateway_authorization(
        first["authorization_id"], success=True,
        actual_microdollars=850_500, selected_usage_type="Credits",
    )
    before = copy.deepcopy(database.typed)
    retry = body.model_copy(update={"region": "europe-west4"})
    replay = gateway._authorize_gateway_sync(_request(), retry, settings)["data"]
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["idempotent_replay"] is True
    assert database.typed == before
    with pytest.raises(HTTPException) as error:
        gateway._authorize_gateway_sync(
            _request(), retry.model_copy(update={"idempotency_key": "new-video"}), settings,
        )
    assert error.value.status_code == 429
    assert database.typed == before
