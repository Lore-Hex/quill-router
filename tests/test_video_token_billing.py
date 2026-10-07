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


def resolution_authorize(client, key, **overrides):
    payload = {
        "api_key_lookup_hash": lookup_hash_api_key(key), "model": MODEL,
        "estimated_input_tokens": 0, "max_tokens": 300_000,
        "route_type": "videos", "video_resolution": "1080p",
        "idempotency_key": "resolution-video", "request_fingerprint": "c" * 64,
        "provider": {"only": ["byteplus"]}, **overrides,
    }
    if payload["video_resolution"] is None:
        payload.pop("video_resolution")
    return client.post("/v1/internal/gateway/authorize", json=payload)


@pytest.mark.parametrize("resolution,status", [("720p", 200), ("1080p", 400)])
def test_resolution_price_ceiling(client, inference_key, resolution, status):
    response = resolution_authorize(
        client, inference_key, video_resolution=resolution,
        provider={"only": ["byteplus"], "max_price": {"completion": 12}},
    )
    assert response.status_code == status, response.text
    if status == 200:
        auth = STORE.get_gateway_authorization(response.json()["data"]["authorization_id"])
        assert auth.estimated_microdollars == 3_386_550


@pytest.mark.parametrize("resolution,provider", [("720p", "byteplus"), ("1080p", "venice")])
def test_resolution_price_sort_before_no_fallback_selection(client, inference_key, monkeypatch, resolution, provider):
    # Put another provider between BytePlus's two tariffs, so resolution must
    # change the winner before allow_fallbacks=false selects a single route.
    monkeypatch.setitem(MODEL_ENDPOINTS, ENDPOINT, replace(
        MODEL_ENDPOINTS[ENDPOINT], completion_price_microdollars_per_million_tokens=12_000_000,
        output_token_price_per_m_by_resolution={"720p": 12_000_000, "1080p": 12_000_000},
    ))
    response = resolution_authorize(
        client, inference_key, video_resolution=resolution,
        provider={"only": ["byteplus", "venice"], "sort": "price", "allow_fallbacks": False},
    )
    assert response.status_code == 200, response.text
    auth = STORE.get_gateway_authorization(response.json()["data"]["authorization_id"])
    assert auth.candidate_endpoint_ids == [f"{MODEL}@{provider}/prepaid"]


def test_resolution_replay_survives_removed_tariff(client, inference_key, monkeypatch):
    import copy

    first = resolution_authorize(client, inference_key)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    auth = STORE.get_gateway_authorization(data["authorization_id"])
    snapshot = auth.video_pricing_snapshot
    money_before = copy.deepcopy(STORE.credit_money[auth.workspace_id])
    reservations_before = copy.deepcopy(STORE.api_keys.reservations)
    assert money_before.reserved_microdollars == 3_703_050
    endpoint_id = f"{MODEL}@byteplus/prepaid"
    original = MODEL_ENDPOINTS[endpoint_id]
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint_id, replace(
        original, output_token_price_per_m_by_resolution={"720p": 11_288_500},
    ))
    replay = resolution_authorize(client, inference_key)
    assert replay.status_code == 200, replay.text
    replay_data = replay.json()["data"]
    assert replay_data["authorization_id"] == auth.id
    assert replay_data["credit_reservation_id"] == data["credit_reservation_id"]
    assert replay_data["video_tariff_resolution"] == "1080p"
    assert replay_data["idempotent_replay"] is True
    assert STORE.get_gateway_authorization(auth.id).video_pricing_snapshot == snapshot
    conflict = resolution_authorize(client, inference_key, request_fingerprint="d" * 64)
    assert conflict.status_code == 409, conflict.text
    assert STORE.credit_money[auth.workspace_id] == money_before
    assert STORE.api_keys.reservations == reservations_before


@pytest.mark.parametrize("resolution,tokens,rate,charge", [
    ("1080p", 243_000, 12_343_500, 2_999_471),  # $2.843100 plus 5.5%, rounded up.
    ("720p", 108_000, 11_288_500, 1_219_158),
    ("480p", 48_000, 11_288_500, 541_848),
])
def test_resolution_authorize_settle_differential(client, inference_key, monkeypatch, resolution, tokens, rate, charge):
    import json

    endpoint_id = f"{MODEL}@byteplus/prepaid"
    original = MODEL_ENDPOINTS[endpoint_id]
    response = resolution_authorize(client, inference_key, video_resolution=resolution)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["video_tariff_resolution"] == resolution
    assert data["candidate_cost_reporting"] is False
    auth = STORE.get_gateway_authorization(data["authorization_id"])
    assert auth.estimated_microdollars == rate * 300_000 // 1_000_000
    money = STORE.credit_money[auth.workspace_id]
    assert money.reserved_microdollars == auth.estimated_microdollars
    initial_usage = money.total_usage_microdollars
    frozen_snapshot = auth.video_pricing_snapshot
    snapshot = json.loads(frozen_snapshot)
    candidate = snapshot["candidates"][0]
    assert candidate["rates"]["output_micro_per_million"] == rate
    assert candidate["tiers"][0]["rates"]["output_micro_per_million"] == rate
    assert snapshot["video_tariff_resolution"] == resolution
    assert MODEL_ENDPOINTS[endpoint_id] is original
    # Change both live headline and tier/table rates. Settlement must use the
    # authorization's frozen tariff, including after an idempotent retry.
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint_id, replace(
        original, completion_price_microdollars_per_million_tokens=1,
        price_tiers=(), output_token_price_per_m_by_resolution={resolution: 1},
    ))
    replay = resolution_authorize(client, inference_key, video_resolution=resolution)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["authorization_id"] == auth.id
    assert replay.json()["data"]["video_tariff_resolution"] == resolution
    assert STORE.get_gateway_authorization(auth.id).video_pricing_snapshot == frozen_snapshot
    for _ in range(2):
        settled = settle(client, auth.id, tokens, selected_endpoint=endpoint_id)
        assert settled.status_code == 200, settled.text
        assert settled.json()["data"]["cost_microdollars"] == charge
    assert STORE.credit_money[auth.workspace_id].total_usage_microdollars == initial_usage + charge
    assert STORE.credit_money[auth.workspace_id].reserved_microdollars == 0
    assert len(STORE.generation_store.generations) == 1


@pytest.mark.parametrize("model,expected_rate", [("2.5", 12_343_500), ("2.0", 8_123_500)])
def test_both_full_seedance_models_authorize_1080p(client, inference_key, model, expected_rate):
    import json

    response = resolution_authorize(client, inference_key, model=f"bytedance/seedance-{model}")
    assert response.status_code == 200, response.text
    auth = STORE.get_gateway_authorization(response.json()["data"]["authorization_id"])
    assert json.loads(auth.video_pricing_snapshot)["candidates"][0]["rates"]["output_micro_per_million"] == expected_rate


def test_fast_1080p_excluded_but_fixed_quote_fallback_remains(client, inference_key):
    model = "bytedance/seedance-2.0-fast"
    rejected = resolution_authorize(client, inference_key, model=model)
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["type"] == "provider_not_supported"
    fallback = resolution_authorize(client, inference_key, model=model,
                                    provider={"only": ["byteplus", "venice"], "allow_fallbacks": False},
                                    additional_cost_reservation_microdollars=900_000)
    assert fallback.status_code == 200, fallback.text
    data = fallback.json()["data"]
    assert data["video_tariff_resolution"] == "1080p"
    auth = STORE.get_gateway_authorization(data["authorization_id"])
    assert auth.candidate_endpoint_ids == [f"{model}@venice/prepaid"]
    assert auth.estimated_microdollars == 900_000
    response = settle(client, auth.id, 0, selected_model=model,
                      selected_endpoint=f"{model}@venice/prepaid", additional_cost_microdollars=900_000)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("resolution,status", [("480p", 200), ("720p", 200), ("1080p", 400)])
def test_token_endpoint_without_table_has_legacy_resolution_limit(client, inference_key, token_endpoint, resolution, status):
    response = resolution_authorize(client, inference_key, video_resolution=resolution, provider={"only": ["venice"]})
    assert response.status_code == status, response.text


def test_absent_resolution_preserves_snapshot_response_and_fingerprint(client, inference_key, monkeypatch):
    from trusted_router.video_billing import video_pricing_snapshot

    endpoint_id = f"{MODEL}@byteplus/prepaid"
    original = MODEL_ENDPOINTS[endpoint_id]
    # Compare to the pre-table catalog and the unchanged two-argument snapshot.
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint_id, replace(original, output_token_price_per_m_by_resolution=None))
    response = resolution_authorize(client, inference_key, video_resolution=None)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "video_tariff_resolution" not in data
    auth = STORE.get_gateway_authorization(data["authorization_id"])
    assert auth.video_pricing_snapshot == video_pricing_snapshot([original], 300_000)
    assert auth.estimated_microdollars == 3_386_550
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint_id, original)
    replay = resolution_authorize(client, inference_key, video_resolution=None)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["authorization_id"] == auth.id
    assert "video_tariff_resolution" not in replay.json()["data"]
    changed_resolution = resolution_authorize(client, inference_key)
    assert changed_resolution.status_code == 200, changed_resolution.text
    assert changed_resolution.json()["data"]["authorization_id"] == auth.id
    assert changed_resolution.json()["data"]["idempotent_replay"] is True
    assert "video_tariff_resolution" not in changed_resolution.json()["data"]
    conflict = resolution_authorize(client, inference_key, request_fingerprint="d" * 64)
    assert conflict.status_code == 409, conflict.text


@pytest.mark.parametrize("overrides", [
    {"video_resolution": "4K"}, {"video_resolution": "1080P"}, {"video_resolution": ""},
    {"video_resolution": 1080}, {"video_resolution": ["1080p"]},
    {"route_type": "chat.completions"}, {"route_type": "images"}, {"route_type": None},
])
def test_invalid_resolution_contract_is_400(client, inference_key, overrides):
    response = resolution_authorize(client, inference_key, **overrides)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "bad_request"
