from __future__ import annotations

from dataclasses import asdict

from fastapi.testclient import TestClient

from trusted_router.catalog import (
    MODELS,
    PRIVACY_TIER_STANDARD,
    endpoint_for_id,
    endpoint_privacy_tier,
    endpoint_stores_content,
    endpoints_for_model,
)
from trusted_router.config import Settings
from trusted_router.routing import video_route_endpoint_candidates
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage import STORE

VIDEO_MODELS = {
    "bytedance/seedance-2.0",
    "bytedance/seedance-2.0-fast",
    "google/veo-3.1",
    "google/veo-3.1-fast",
    "google/gemini-omni-flash",
    "openai/sora-2",
    "openai/sora-2-pro",
    "runway/gen-4.5",
    "kling/v3-pro",
    "kling/o3-pro",
    "alibaba/wan-2.7",
    "shengshu/vidu-q3",
    "pixverse/c1",
    "lightricks/ltx-2.3",
    "lightricks/ltx-2.3-fast",
    "minimax/hailuo-3",
    "x-ai/grok-imagine-video",
}

NATIVE_VIDEO_PROVIDERS = {
    "lightricks/ltx-2.3": ("ltx",),
    "lightricks/ltx-2.3-fast": ("ltx",),
    "minimax/hailuo-3": ("atlas-cloud",),
    "google/veo-3.1": ("google-ai-studio",),
    "google/veo-3.1-fast": ("google-ai-studio",),
    "alibaba/wan-2.7": ("alibaba",),
    "x-ai/grok-imagine-video": ("grok",),
    "runway/gen-4.5": ("runway",),
    "openai/sora-2": ("openai",),
    "openai/sora-2-pro": ("openai",),
    "kling/v3-pro": ("kling",),
    "kling/o3-pro": ("kling",),
}


def _authorize_video(
    client: TestClient,
    raw_key: str,
    *,
    model: str = "minimax/hailuo-3",
    quote: int = 850_500,
    idempotency_key: str = "video-test-1",
    request_fingerprint: str = "a" * 64,
) -> dict[str, object]:
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": lookup_hash_api_key(raw_key),
            "model": model,
            "estimated_input_tokens": 0,
            "max_output_tokens": 1,
            "route_type": "videos",
            "additional_cost_reservation_microdollars": quote,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_launch_video_catalog_is_explicit_and_credits_only() -> None:
    for model_id in VIDEO_MODELS:
        model = MODELS[model_id]
        assert model.supports_video is True
        assert model.supports_chat is False
        assert model.prepaid_available is True
        assert model.byok_available is False
        endpoints = endpoints_for_model(model_id)
        expected = list(NATIVE_VIDEO_PROVIDERS.get(model_id, ("venice",)))
        if model_id in NATIVE_VIDEO_PROVIDERS and model_id != "x-ai/grok-imagine-video":
            expected.append("venice")
        assert [endpoint.provider for endpoint in endpoints] == expected
        assert all(endpoint.usage_type == "Credits" for endpoint in endpoints)
        assert all(endpoint.upstream_id for endpoint in endpoints)

    assert MODELS["minimax/hailuo-3"].name == "MiniMax Hailuo 3 (H3)"
    assert MODELS["minimax/hailuo-3"].input_modalities == (
        "text",
        "image",
        "audio",
        "video",
    )


def test_sora_video_authorizes_direct_openai_before_standard_fallback() -> None:
    for model_id in ("openai/sora-2", "openai/sora-2-pro"):
        direct = endpoint_for_id(f"{model_id}@openai/prepaid")
        fallback = endpoint_for_id(f"{model_id}@venice/prepaid")
        assert direct is not None
        assert fallback is not None
        assert endpoint_privacy_tier(direct) == PRIVACY_TIER_STANDARD
        assert endpoint_stores_content(direct) is True
        assert endpoint_privacy_tier(fallback) == PRIVACY_TIER_STANDARD
        assert endpoint_stores_content(fallback) is True


def test_video_router_rejects_text_models_and_honors_provider_filters() -> None:
    candidates = video_route_endpoint_candidates(
        {
            "model": "minimax/hailuo-3",
            "provider": {"only": ["venice"]},
        },
        Settings(environment="test"),
    )
    assert [(model.id, endpoint.provider) for model, endpoint in candidates] == [
        ("minimax/hailuo-3", "venice")
    ]
    atlas_candidates = video_route_endpoint_candidates(
        {
            "model": "minimax/hailuo-3",
            "provider": {"only": ["atlas-cloud"]},
        },
        Settings(environment="test"),
    )
    assert [(model.id, endpoint.provider) for model, endpoint in atlas_candidates] == [
        ("minimax/hailuo-3", "atlas-cloud")
    ]


def test_video_authorize_and_settle_bill_exact_fixed_microdollars(
    client: TestClient,
    inference_key: str,
) -> None:
    quote = 850_500
    auth = _authorize_video(client, inference_key, quote=quote)
    authorization = STORE.get_gateway_authorization(str(auth["authorization_id"]))
    assert authorization is not None
    assert authorization.estimated_microdollars == quote
    assert authorization.additional_cost_reservation_microdollars == quote
    assert auth["provider"] == "atlas-cloud"
    assert auth["usage_type"] == "Credits"

    response = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "elapsed_seconds": 2.5,
            "finish_reason": "completed",
            "route_type": "videos",
            "selected_model": "minimax/hailuo-3",
            "selected_endpoint": auth["endpoint_id"],
            "additional_cost_microdollars": quote,
            "video_input_mode": "image",
            "video_duration_seconds": 5,
            "video_resolution": "2K",
            "video_aspect_ratio": "source",
            "video_generate_audio": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_microdollars"] == quote
    generations = list(STORE.generation_store.generations.values())
    assert len(generations) == 1
    assert generations[0].total_cost_microdollars == quote
    assert generations[0].model == "minimax/hailuo-3"
    assert generations[0].route_type == "videos"
    assert generations[0].video_input_mode == "image"
    assert generations[0].video_duration_seconds == 5
    assert generations[0].video_resolution == "2K"
    assert generations[0].video_aspect_ratio == "source"
    assert generations[0].video_generate_audio is True
    benchmark = STORE.provider_benchmark_samples(date=None, limit=10)[0]
    assert benchmark.route_type == "videos"
    assert benchmark.video_duration_seconds == 5
    assert benchmark.video_resolution == "2K"


def test_video_settlement_cannot_exceed_content_free_quote(
    client: TestClient,
    inference_key: str,
) -> None:
    auth = _authorize_video(client, inference_key, quote=400_000)
    response = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "route_type": "videos",
            "selected_endpoint": auth["endpoint_id"],
            "additional_cost_microdollars": 400_001,
        },
    )
    assert response.status_code == 400, response.text
    authorization = STORE.get_gateway_authorization(str(auth["authorization_id"]))
    assert authorization is not None
    assert authorization.settled is False


def test_video_idempotency_binds_request_not_fresh_provider_quote(
    client: TestClient,
    inference_key: str,
) -> None:
    first = _authorize_video(
        client,
        inference_key,
        quote=850_500,
        idempotency_key="video-price-retry",
        request_fingerprint="1" * 64,
    )
    replay = _authorize_video(
        client,
        inference_key,
        quote=900_000,
        idempotency_key="video-price-retry",
        request_fingerprint="1" * 64,
    )
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["additional_cost_reservation_microdollars"] == 850_500

    mismatch = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": lookup_hash_api_key(inference_key),
            "model": "minimax/hailuo-3",
            "estimated_input_tokens": 0,
            "max_output_tokens": 1,
            "route_type": "videos",
            "additional_cost_reservation_microdollars": 900_000,
            "idempotency_key": "video-price-retry",
            "request_fingerprint": "2" * 64,
        },
    )
    assert mismatch.status_code == 409


def test_video_job_state_is_content_free_idempotent_and_key_scoped(
    client: TestClient,
    inference_key: str,
) -> None:
    auth = _authorize_video(client, inference_key)
    endpoint = endpoint_for_id(str(auth["endpoint_id"]))
    assert endpoint is not None
    prepare_body = {
        "job_id": "job-0123456789abcdef",
        "authorization_id": auth["authorization_id"],
        "model": "minimax/hailuo-3",
        "provider": auth["provider"],
        "endpoint_id": endpoint.id,
        "provider_model": "MiniMax-H3",
        "quoted_microdollars": 850_500,
        "input_mode": "reference",
        "duration_seconds": 5,
        "resolution": "2K",
        "aspect_ratio": "16:9",
        "generate_audio": True,
        "region": "us-central1",
    }
    first = client.post("/v1/internal/gateway/video/jobs/prepare", json=prepare_body)
    second = client.post("/v1/internal/gateway/video/jobs/prepare", json=prepare_body)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data"]["created"] is True
    assert second.json()["data"]["created"] is False

    forbidden = client.post(
        "/v1/internal/gateway/video/jobs/prepare",
        json={**prepare_body, "prompt": "must never cross into the control plane"},
    )
    assert forbidden.status_code == 400

    queued = client.post(
        "/v1/internal/gateway/video/jobs/job-0123456789abcdef/queued",
        json={
            "provider_job_id": "provider-job-1",
            "poll_after_seconds": 1,
        },
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["data"]["provider"] == auth["provider"]
    assert queued.json()["data"]["endpoint_id"] == endpoint.id
    assert queued.json()["data"]["quoted_microdollars"] == 850_500
    stored = STORE.get_video_job("job-0123456789abcdef")
    assert stored is not None
    assert stored.input_mode == "reference"
    assert stored.duration_seconds == 5
    assert stored.resolution == "2K"
    assert stored.aspect_ratio == "16:9"
    assert stored.generate_audio is True
    assert stored.region == "us-central1"
    stored.next_poll_at = "2000-01-01T00:00:00Z"

    claimed = client.post(
        "/v1/internal/gateway/video/jobs/claim",
        json={"lease_owner": "worker-1", "limit": 1, "lease_seconds": 60},
    )
    assert claimed.status_code == 200, claimed.text
    assert [job["id"] for job in claimed.json()["data"]] == [stored.id]

    completed = client.post(
        f"/v1/internal/gateway/video/jobs/{stored.id}/update",
        json={
            "status": "completed",
            "lease_owner": "worker-1",
            "provider_status": "COMPLETED",
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["generation_id"] is None

    repaired = client.post(
        f"/v1/internal/gateway/video/jobs/{stored.id}/update",
        json={"status": "completed", "generation_id": "gen-video-1"},
    )
    assert repaired.status_code == 200, repaired.text
    completed_data = repaired.json()["data"]
    assert completed_data["generation_id"] == "gen-video-1"
    assert completed_data["content_expires_at"]
    assert completed_data["next_poll_at"] == completed_data["content_expires_at"]

    raw_fields = asdict(STORE.get_video_job(stored.id))
    assert not ({"prompt", "output", "media", "download_url"} & raw_fields.keys())
    assert "must never cross" not in repr(raw_fields)

    bob_key = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "bob@example.com"},
        json={"name": "bob video key"},
    )
    assert bob_key.status_code == 201, bob_key.text
    lookup = client.post(
        f"/v1/internal/gateway/video/jobs/{stored.id}/lookup",
        json={"api_key_lookup_hash": lookup_hash_api_key(bob_key.json()["key"])},
    )
    assert lookup.status_code == 404


def test_failed_video_job_records_one_public_safe_benchmark_sample(
    client: TestClient,
    inference_key: str,
) -> None:
    auth = _authorize_video(client, inference_key, idempotency_key="video-failure")
    prepared = client.post(
        "/v1/internal/gateway/video/jobs/prepare",
        json={
            "job_id": "job-failure-safe",
            "authorization_id": auth["authorization_id"],
            "model": "minimax/hailuo-3",
            "provider": auth["provider"],
            "endpoint_id": auth["endpoint_id"],
            "provider_model": "MiniMax-H3",
            "quoted_microdollars": 850_500,
            "input_mode": "text",
            "duration_seconds": 5,
            "resolution": "2K",
            "aspect_ratio": "16:9",
            "generate_audio": True,
            "region": "europe-west4",
        },
    )
    assert prepared.status_code == 200, prepared.text

    update = {
        "status": "failed",
        "provider_status": "FAILED",
        "error": "Bearer private-key and private prompt",
    }
    first = client.post("/v1/internal/gateway/video/jobs/job-failure-safe/update", json=update)
    replay = client.post("/v1/internal/gateway/video/jobs/job-failure-safe/update", json=update)
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text

    rows = [
        sample
        for sample in STORE.provider_benchmark_samples(date=None, limit=20)
        if sample.model == "minimax/hailuo-3"
    ]
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].error_type == "provider_error"
    assert rows[0].region == "europe-west4"
    assert rows[0].video_duration_seconds == 5
    assert "private-key" not in repr(rows[0])
    assert "private prompt" not in repr(rows[0])


def test_video_job_prepare_rejects_mismatched_quote(
    client: TestClient,
    inference_key: str,
) -> None:
    auth = _authorize_video(client, inference_key, quote=550_000)
    response = client.post(
        "/v1/internal/gateway/video/jobs/prepare",
        json={
            "job_id": "job-quote-mismatch",
            "authorization_id": auth["authorization_id"],
            "model": "minimax/hailuo-3",
            "provider": auth["provider"],
            "endpoint_id": auth["endpoint_id"],
            "provider_model": "gemini-omni-flash-text-to-video",
            "quoted_microdollars": 550_001,
        },
    )
    assert response.status_code == 400


def test_video_job_can_persist_an_authorized_fallback_route_and_exact_charge(
    client: TestClient,
    inference_key: str,
) -> None:
    auth = _authorize_video(
        client,
        inference_key,
        quote=900_000,
        idempotency_key="video-authorized-fallback",
    )
    assert auth["provider"] == "atlas-cloud"
    venice = next(
        candidate for candidate in auth["route_candidates"] if candidate["provider"] == "venice"
    )
    prepare = client.post(
        "/v1/internal/gateway/video/jobs/prepare",
        json={
            "job_id": "job-authorized-fallback",
            "authorization_id": auth["authorization_id"],
            "model": "minimax/hailuo-3",
            "provider": "atlas-cloud",
            "endpoint_id": auth["endpoint_id"],
            "provider_model": "minimax/h3/text-to-video",
            "quoted_microdollars": 840_000,
            "duration_seconds": 5,
            "resolution": "2K",
        },
    )
    assert prepare.status_code == 200, prepare.text

    queued = client.post(
        "/v1/internal/gateway/video/jobs/job-authorized-fallback/queued",
        json={
            "provider_job_id": "venice-fallback-job",
            "provider": "venice",
            "endpoint_id": venice["endpoint_id"],
            "provider_model": "minimax-h3-text-to-video",
            "quoted_microdollars": 850_500,
            "poll_after_seconds": 5,
        },
    )
    assert queued.status_code == 200, queued.text
    job = queued.json()["data"]
    assert job["provider"] == "venice"
    assert job["endpoint_id"] == venice["endpoint_id"]
    assert job["quoted_microdollars"] == 850_500

    unauthorized = client.post(
        "/v1/internal/gateway/video/jobs/job-authorized-fallback/queued",
        json={
            "provider_job_id": "other-job",
            "provider": "grok",
            "endpoint_id": "x-ai/grok-imagine-video@grok/prepaid",
            "provider_model": "grok-imagine-video",
            "quoted_microdollars": 300_000,
            "poll_after_seconds": 5,
        },
    )
    assert unauthorized.status_code == 400
