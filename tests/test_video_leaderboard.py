from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE
from trusted_router.storage_models import ProviderBenchmarkSample, VideoJob
from trusted_router.synthetic.video_leaderboard import aggregate_video_leaderboard


def _settings() -> Settings:
    return Settings(
        environment="test",
        sentry_dsn=None,
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        google_client_id=None,
        google_client_secret=None,
        google_oauth_redirect_url=None,
        github_client_id=None,
        github_client_secret=None,
        github_oauth_redirect_url=None,
    )


def _video_sample(
    sample_id: str,
    *,
    model: str = "minimax/hailuo-3",
    provider: str = "venice",
    status: str = "success",
    elapsed_ms: int = 120_000,
    cost: int = 972_000,
    duration: int = 5,
) -> ProviderBenchmarkSample:
    return ProviderBenchmarkSample(
        id=sample_id,
        model=model,
        provider=provider,
        provider_name=provider.title(),
        status=status,
        usage_type="Credits",
        streamed=False,
        elapsed_milliseconds=elapsed_ms,
        total_cost_microdollars=cost if status == "success" else 0,
        error_type=None if status == "success" else "provider_error",
        route_type="videos",
        video_input_mode="text",
        video_duration_seconds=duration,
        video_resolution="2K",
        video_aspect_ratio="16:9",
        video_generate_audio=True,
    )


def test_video_leaderboard_aggregates_async_media_metrics() -> None:
    payload = aggregate_video_leaderboard(
        [
            _video_sample("v1", elapsed_ms=100_000, cost=1_000_001),
            _video_sample("v2", elapsed_ms=200_000, cost=1_500_000),
            _video_sample("v3", status="error"),
        ]
    )

    model = payload["models"][0]
    assert model["success_rate"] == 0.6667
    assert model["p50_completion_ms"] == 100_000
    assert model["p95_completion_ms"] == 200_000
    assert model["p50_cost_microdollars"] == 1_000_001
    assert model["p50_cost_per_second_microdollars"] == 200_001
    assert model["p50_cost_per_second_usd"] == "0.200001"
    assert model["p50_generation_seconds_per_output_second"] == 20.0
    assert model["top_error"] == "provider_error"
    assert model["input_modes"] == ["text"]
    assert model["audio_rate"] == 1.0


def test_video_leaderboard_ranks_success_before_raw_speed() -> None:
    payload = aggregate_video_leaderboard(
        [
            _video_sample("reliable", model="google/veo-3.1", elapsed_ms=200_000),
            _video_sample("fast", model="runway/gen-4.5", elapsed_ms=20_000),
            _video_sample("fast-error", model="runway/gen-4.5", status="error"),
        ]
    )
    assert [row["model"] for row in payload["models"]] == [
        "google/veo-3.1",
        "runway/gen-4.5",
    ]


def test_video_leaderboard_lists_configured_routes_while_samples_warm_up() -> None:
    payload = aggregate_video_leaderboard(
        [],
        configured_routes={
            ("ltx", "lightricks/ltx-2.3-fast"),
            ("minimax", "minimax/hailuo-3"),
        },
    )

    assert payload["total_samples"] == 0
    assert payload["provider_count"] == 2
    assert payload["model_count"] == 2
    assert {row["provider"] for row in payload["providers"]} == {"ltx", "minimax"}
    assert all(row["measurement_status"] == "awaiting_samples" for row in payload["models"])
    assert all(row["rank"] is None for row in payload["models"])


def test_video_leaderboard_page_and_json_are_separate_from_text_metrics() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    STORE.record_provider_benchmark(_video_sample("video-page"))

    page = client.get("/leaderboard/video")
    payload = client.get("/leaderboard/video.json")
    text_page = client.get("/leaderboard")

    assert page.status_code == 200
    assert payload.status_code == 200
    assert "Video generation performance" in page.text
    assert "p50 cost / output sec" in page.text
    assert "minimax/hailuo-3" in page.text
    data = payload.json()["data"]
    assert data["model_count"] > 1
    routes = {(row["provider"], row["model"]) for row in data["models"]}
    assert ("atlas-cloud", "minimax/hailuo-3") in routes
    assert ("ltx", "lightricks/ltx-2.3-fast") in routes
    assert "Awaiting sample" in page.text
    assert "minimax/hailuo-3" not in text_page.text


def test_video_leaderboard_public_payload_contains_no_content_or_tenant_fields() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    STORE.record_provider_benchmark(_video_sample("video-redaction"))

    payload = client.get("/leaderboard/video.json").json()

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {nested for item in value.values() for nested in keys(item)}
        if isinstance(value, list):
            return {nested for item in value for nested in keys(item)}
        return set()

    for forbidden in (
        "prompt",
        "output",
        "workspace_id",
        "key_hash",
        "download_url",
        "reference_url",
    ):
        assert forbidden not in keys(payload)


def test_video_failure_sample_never_publishes_raw_provider_error() -> None:
    sample = ProviderBenchmarkSample.from_video_job_failure(
        VideoJob(
            id="job-private-error",
            workspace_id="ws-private",
            key_hash="key-private",
            authorization_id="auth-private",
            model="minimax/hailuo-3",
            provider="venice",
            endpoint_id="minimax/hailuo-3@venice/prepaid",
            provider_model="minimax-h3-text-to-video",
            quoted_microdollars=972_000,
            status="failed",
            last_error="Bearer secret-token private prompt contents",
        ),
        provider_name="Venice",
    )

    assert sample.error_type == "provider_error"
    assert "secret-token" not in str(sample)
    assert "private prompt" not in str(sample)


def test_video_leaderboard_is_in_sitemap_and_linked_from_video_docs() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    assert (
        "<loc>https://trustedrouter.com/leaderboard/video</loc>"
        in client.get("/sitemap-core.xml").text
    )
    assert 'href="/leaderboard/video"' in client.get("/docs/video").text
