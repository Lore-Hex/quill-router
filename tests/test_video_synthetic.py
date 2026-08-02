from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from trusted_router.config import Settings
from trusted_router.storage_models import SyntheticProbeSample
from trusted_router.synthetic.probes import (
    VIDEO_GENERATION_DURATION_SECONDS,
    VIDEO_GENERATION_MODEL,
    VIDEO_GENERATION_PROVIDER,
    VIDEO_GENERATION_RESOLUTION,
    SyntheticTarget,
    video_generation_probe,
)
from trusted_router.synthetic.route_health import report_video_generation_failures
from trusted_router.synthetic.video_generation import (
    DAILY_VIDEO_PROFILES,
    DailyVideoProfile,
    daily_video_profile,
)


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypisom" + (b"\x00" * 2048)


def test_daily_video_profiles_rotate_all_direct_providers_at_minimum_cost() -> None:
    assert len(DAILY_VIDEO_PROFILES) == 7
    assert len({profile.provider for profile in DAILY_VIDEO_PROFILES}) == 7
    assert [daily_video_profile(date(2026, 8, day)).provider for day in range(3, 10)] == [
        "grok",
        "runway",
        "alibaba",
        "kling",
        "ltx",
        "google-ai-studio",
        "minimax",
    ]
    assert sum(profile.expected_cost_microdollars for profile in DAILY_VIDEO_PROFILES) == (
        2_499_276
    )
    assert max(profile.expected_cost_microdollars for profile in DAILY_VIDEO_PROFILES) <= 672_000
    profiles = {profile.provider: profile for profile in DAILY_VIDEO_PROFILES}
    assert profiles["minimax"].generate_audio is True
    assert profiles["google-ai-studio"].generate_audio is True
    assert all(
        not profile.generate_audio
        for provider, profile in profiles.items()
        if provider not in {"minimax", "google-ai-studio"}
    )


@pytest.mark.asyncio
async def test_video_probe_generates_once_validates_media_and_keeps_only_metadata() -> None:
    requests: list[tuple[str, str]] = []
    create_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/videos":
            create_bodies.append(json.loads(request.content))
            assert request.headers["idempotency-key"] == "daily-video-2026-08-01"
            return httpx.Response(
                202,
                json={
                    "id": "job-video-synthetic",
                    "polling_url": "/v1/videos/job-video-synthetic",
                    "status": "pending",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/videos/job-video-synthetic":
            return httpx.Response(
                200,
                json={
                    "id": "job-video-synthetic",
                    "status": "completed",
                    "generation_id": "gen-video-synthetic",
                    "unsigned_urls": ["/v1/videos/job-video-synthetic/content"],
                    "usage": {"cost_microdollars": 60_000},
                },
            )
        if request.method == "GET" and request.url.path == "/v1/videos/job-video-synthetic/content":
            return httpx.Response(200, content=_mp4(), headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await video_generation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            idempotency_key="daily-video-2026-08-01",
            poll_interval_seconds=0,
        )

    assert sample.status == "up"
    assert sample.output_match is True
    assert sample.generation_id == "gen-video-synthetic"
    assert sample.cost_microdollars == 60_000
    assert requests == [
        ("POST", "/v1/videos"),
        ("GET", "/v1/videos/job-video-synthetic"),
        ("GET", "/v1/videos/job-video-synthetic/content"),
    ]
    assert create_bodies == [
        {
            "model": VIDEO_GENERATION_MODEL,
            "prompt": "A white dot moves once across a plain black background.",
            "duration": VIDEO_GENERATION_DURATION_SECONDS,
            "resolution": VIDEO_GENERATION_RESOLUTION,
            "aspect_ratio": "16:9",
            "generate_audio": False,
            "provider": {"only": [VIDEO_GENERATION_PROVIDER], "allow_fallbacks": False},
        }
    ]
    public = json.dumps(sample.public_dict())
    assert "white dot" not in public
    assert "ftyp" not in public
    assert "sk-test" not in public


@pytest.mark.asyncio
async def test_video_probe_failure_never_retries_generation_or_copies_error_body() -> None:
    create_calls = 0
    private_error = "provider secret and private request must not be retained"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        if request.method == "POST" and request.url.path == "/v1/videos":
            create_calls += 1
            return httpx.Response(502, json={"error": {"message": private_error}})
        return httpx.Response(404)

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await video_generation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            idempotency_key="daily-video-2026-08-01",
            poll_interval_seconds=0,
        )

    assert create_calls == 1
    assert sample.status == "down"
    assert sample.error_type == "video_generation_http_502"
    assert private_error not in json.dumps(sample.public_dict())


@pytest.mark.asyncio
async def test_video_probe_accepts_already_completed_daily_idempotent_job() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "id": "job-already-cleaned",
                "status": "completed",
                "generation_id": "gen-already-cleaned",
                "usage": {"cost_microdollars": 60_000},
            },
        )

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await video_generation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            idempotency_key="daily-video-2026-08-01",
        )

    assert sample.status == "up"
    assert sample.output_match is True
    assert sample.generation_id == "gen-already-cleaned"


@pytest.mark.asyncio
async def test_daily_video_job_ingests_one_metadata_only_sample(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trusted_router.synthetic import video_generation as video_job

    create_calls = 0
    ingested: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        if request.method == "POST" and request.url.path == "/v1/videos":
            create_calls += 1
            assert request.headers["idempotency-key"].startswith("trustedrouter-daily-video-")
            assert json.loads(request.content)["generate_audio"] is True
            return httpx.Response(
                200,
                json={
                    "id": "job-daily",
                    "status": "completed",
                    "generation_id": "gen-daily",
                    "unsigned_urls": ["/v1/videos/job-daily/content"],
                    "usage": {"cost_microdollars": 60_000},
                },
            )
        if request.method == "GET" and request.url.path == "/v1/videos/job-daily/content":
            return httpx.Response(200, content=_mp4(), headers={"content-type": "video/mp4"})
        if request.method == "POST" and request.url.path == "/v1/internal/synthetic/samples":
            ingested.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"recorded": 1}})
        return httpx.Response(404)

    settings = Settings(
        environment="test",
        api_base_url="https://api.trustedrouter.com/v1",
        internal_gateway_token="internal-test",  # noqa: S106 - test placeholder.
        synthetic_monitor_api_key="sk-tr-test",
    )
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(video_job, "get_settings", lambda: settings)
    monkeypatch.setattr(
        video_job,
        "daily_video_profile",
        lambda _day: DailyVideoProfile(
            VIDEO_GENERATION_MODEL,
            VIDEO_GENERATION_PROVIDER,
            VIDEO_GENERATION_DURATION_SECONDS,
            VIDEO_GENERATION_RESOLUTION,
            60_000,
            True,
        ),
    )
    monkeypatch.setattr(video_job.httpx, "AsyncClient", client_factory)
    monkeypatch.setenv(
        "TR_SYNTHETIC_INGEST_URL",
        "https://trustedrouter.com/v1/internal/synthetic/samples",
    )
    monkeypatch.setenv("TR_SYNTHETIC_VIDEO_POLL_INTERVAL_SECONDS", "0")

    result = await video_job.run()

    assert result == 0
    assert create_calls == 1
    assert len(ingested) == 1
    sample = ingested[0]["samples"][0]
    assert sample["probe_type"] == "video_generation"
    assert sample["status"] == "up"
    assert sample["cost_microdollars"] == 60_000
    assert "prompt" not in json.dumps(ingested).casefold()
    output = json.loads(capsys.readouterr().out)
    assert output["generation_id"] == "gen-daily"
    assert output["cost_microdollars"] == 60_000


def test_video_failure_alert_is_grouped_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentry_sdk

    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda message, *, level: captured.append((message, level)),
    )
    sample = SyntheticProbeSample(
        id="syn-video-failed",
        probe_type="video_generation",
        target="canonical",
        target_url="https://api.trustedrouter.com/v1/videos",
        monitor_region="us-central1",
        status="down",
        provider=VIDEO_GENERATION_PROVIDER,
        model=VIDEO_GENERATION_MODEL,
        error_type="video_generation_failed",
    )

    report_video_generation_failures([sample])

    assert captured == [
        (
            "video-generation-canary: grok/x-ai/grok-imagine-video failed "
            "(video_generation_failed, HTTP none)",
            "error",
        )
    ]


def test_video_synthetic_deploy_is_one_rotating_generation_per_day() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/deploy/synthetic.sh"
    body = script.read_text()
    section = body.split("daily video-generation Cloud Run job", maxsplit=1)[1]

    assert 'video_scheduler_name="${video_job_name}-daily"' in body
    assert "rotate through seven providers weekly" in body
    assert "TR_SYNTHETIC_VIDEO_MODEL=" not in section
    assert "TR_SYNTHETIC_VIDEO_PROVIDER=" not in section
    assert "TR_SYNTHETIC_VIDEO_DURATION_SECONDS=" not in section
    assert "TR_SYNTHETIC_VIDEO_RESOLUTION=" not in section
    assert '"TR_SYNTHETIC_VIDEO_TIMEOUT_SECONDS=900"' in body
    assert '--args="-m,trusted_router.synthetic.video_generation"' in section
    assert "--max-retries 0" in section
    assert "--task-timeout 1200s" in section
    assert '"41 9 * * *"' in section
    assert "*/" not in section.split('"41 9 * * *"', maxsplit=1)[0]
