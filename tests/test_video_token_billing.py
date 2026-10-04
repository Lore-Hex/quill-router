"""Token-billed media must preserve the fixed-quote ledger contract."""

from dataclasses import replace

import pytest

from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage import STORE

MODEL = "bytedance/seedance-2.5"
ENDPOINT = f"{MODEL}@venice/prepaid"


@pytest.fixture
def token_endpoint(monkeypatch):
    # Isolate the accounting contract from provider rollout/catalog state.
    monkeypatch.setitem(MODEL_ENDPOINTS, ENDPOINT, replace(
        MODEL_ENDPOINTS[ENDPOINT], completion_price_microdollars_per_million_tokens=10_700_000,
        published_completion_price_microdollars_per_million_tokens=10_700_000,
    ))


def authorize(client, key, quote=0):
    response = client.post("/v1/internal/gateway/authorize", json={
        "api_key_lookup_hash": lookup_hash_api_key(key), "model": MODEL,
        "estimated_input_tokens": 0, "max_output_tokens": 80_000,
        "route_type": "videos", "additional_cost_reservation_microdollars": quote,
        "idempotency_key": "token-video", "request_fingerprint": "b" * 64,
        "provider": {"only": ["venice"]},
    })
    assert response.status_code == 200, response.text
    assert response.json()["data"]["candidate_cost_reporting"] is False
    return response.json()["data"]["authorization_id"]


def settle(client, authorization_id, tokens=38_830, **extra):
    return client.post("/v1/internal/gateway/settle", json={
        "authorization_id": authorization_id, "actual_input_tokens": 0,
        "actual_output_tokens": tokens, "route_type": "videos",
        "selected_endpoint": ENDPOINT, "selected_model": MODEL,
        "finish_reason": "completed", **extra,
    })


@pytest.mark.parametrize("fixed_quote,hold", [(0, 856_000), (900_000, 900_000)])
def test_video_token_hold_settles_actual_usage_once(client, inference_key, token_endpoint, fixed_quote, hold):
    auth_id = authorize(client, inference_key, fixed_quote)
    auth = STORE.get_gateway_authorization(auth_id)
    assert auth.estimated_microdollars == hold
    assert auth.video_pricing_snapshot
    assert auth.pricing_snapshot is None  # Never enroll async video in Stage D.
    for _ in range(2):
        response = settle(client, auth_id)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["cost_microdollars"] == 415_481
    auth = STORE.get_gateway_authorization(auth_id)
    assert auth.settled
    assert auth.finalized_output_tokens == 38_830
    assert auth.finalized_cost_microdollars == 415_481
    assert len(STORE.generation_store.generations) == 1


@pytest.mark.parametrize("new_rate", [0, 1_000_000, 100_000_000])
def test_price_refresh_cannot_reprice_a_video(client, inference_key, token_endpoint, monkeypatch, new_rate):
    auth_id = authorize(client, inference_key)
    snapshot = STORE.get_gateway_authorization(auth_id).video_pricing_snapshot
    monkeypatch.setitem(MODEL_ENDPOINTS, ENDPOINT, replace(
        MODEL_ENDPOINTS[ENDPOINT], completion_price_microdollars_per_million_tokens=new_rate,
    ))
    assert authorize(client, inference_key) == auth_id
    assert STORE.get_gateway_authorization(auth_id).video_pricing_snapshot == snapshot
    response = settle(client, auth_id)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_microdollars"] == 415_481


def test_video_token_ceiling_not_inflated_by_fixed_provider_hold(client, inference_key, token_endpoint):
    auth_id = authorize(client, inference_key, 900_000)
    response = settle(client, auth_id, 80_001)
    assert response.status_code == 400, response.text
    assert not STORE.get_gateway_authorization(auth_id).settled


def test_video_refund_needs_no_usage_or_snapshot(client, inference_key, token_endpoint):
    auth_id = authorize(client, inference_key)
    STORE.get_gateway_authorization(auth_id).video_pricing_snapshot = None
    response = client.post("/v1/internal/gateway/refund", json={"authorization_id": auth_id})
    assert response.status_code == 200, response.text
    assert STORE.get_gateway_authorization(auth_id).finalized_cost_microdollars == 0


def test_video_cannot_disguise_settlement_as_chat(client, inference_key, token_endpoint):
    auth_id = authorize(client, inference_key)
    response = settle(client, auth_id, route_type="chat_completions")
    assert response.status_code == 400, response.text
    assert not STORE.get_gateway_authorization(auth_id).settled


@pytest.mark.parametrize("tokens,extra", [
    (0, {}), (80_001, {}), (100, {"additional_cost_microdollars": 1}),
    (100, {"actual_input_tokens": 1}),
])
def test_invalid_token_video_usage_does_not_mutate_credits(client, inference_key, token_endpoint, tokens, extra):
    auth_id = authorize(client, inference_key)
    response = settle(client, auth_id, tokens, **extra)
    assert response.status_code == 400, response.text
    assert not STORE.get_gateway_authorization(auth_id).settled
    assert not STORE.generation_store.generations


def test_video_token_job_round_trip_and_authoritative_public_cost(client, inference_key, token_endpoint):
    auth_id = authorize(client, inference_key)
    job = {"job_id": "job-tokens", "authorization_id": auth_id, "model": MODEL,
           "provider": "venice", "endpoint_id": ENDPOINT, "provider_model": "native-model",
           "quoted_microdollars": 0, "output_token_limit": 80_000}
    for limit, status in [(0, 400), (80_001, 400), (80_000, 200)]:
        response = client.post("/v1/internal/gateway/video/jobs/prepare", json={**job, "output_token_limit": limit})
        assert response.status_code == status, response.text
    queued = client.post("/v1/internal/gateway/video/jobs/job-tokens/queued", json={
        "provider_job_id": "native-job", "quoted_microdollars": 0,
    })
    assert queued.status_code == 200, queued.text
    assert queued.json()["data"]["output_token_limit"] == 80_000
    assert settle(client, auth_id).status_code == 200
    updated = client.post("/v1/internal/gateway/video/jobs/job-tokens/update", json={"status": "completed"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["settled_microdollars"] == 415_481
    assert updated.json()["data"]["output_tokens"] == 38_830
