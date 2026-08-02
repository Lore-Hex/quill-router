from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from trusted_router.config import get_settings
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    video_generation_probe,
)


@dataclass(frozen=True)
class DailyVideoProfile:
    model: str
    provider: str
    duration_seconds: int
    resolution: str
    expected_cost_microdollars: int
    generate_audio: bool = False


# One shortest-valid direct generation per UTC day. The order is deliberately
# stable so retries select the same provider and every route is exercised once
# per week without multiplying the number of paid generations.
DAILY_VIDEO_PROFILES: tuple[DailyVideoProfile, ...] = (
    DailyVideoProfile("x-ai/grok-imagine-video", "grok", 1, "480p", 60_000),
    DailyVideoProfile("runway/gen-4.5", "runway", 2, "720p", 288_000),
    DailyVideoProfile("alibaba/wan-2.7", "alibaba", 2, "720p", 240_000),
    DailyVideoProfile("kling/v3-pro", "kling", 3, "720p", 327_276),
    DailyVideoProfile("lightricks/ltx-2.3-fast", "ltx", 6, "1080p", 432_000),
    DailyVideoProfile(
        "google/veo-3.1-fast",
        "google-ai-studio",
        4,
        "720p",
        480_000,
        True,
    ),
    DailyVideoProfile("minimax/hailuo-3", "minimax", 4, "2K", 672_000, True),
)


def daily_video_profile(day: date) -> DailyVideoProfile:
    return DAILY_VIDEO_PROFILES[day.weekday()]


async def run() -> int:
    settings = get_settings()
    api_key = settings.synthetic_monitor_api_key
    internal_token = settings.internal_gateway_token
    if not api_key:
        print("TR_SYNTHETIC_MONITOR_API_KEY is required", file=sys.stderr)
        return 2
    if not internal_token:
        print("TR_INTERNAL_GATEWAY_TOKEN is required", file=sys.stderr)
        return 2

    monitor_region = os.environ.get("TR_SYNTHETIC_MONITOR_REGION", "us-central1")
    run_day = datetime.now(UTC).date()
    profile = daily_video_profile(run_day)
    model = os.environ.get("TR_SYNTHETIC_VIDEO_MODEL", profile.model)
    provider = os.environ.get("TR_SYNTHETIC_VIDEO_PROVIDER", profile.provider)
    duration_seconds = max(
        1,
        int(
            os.environ.get(
                "TR_SYNTHETIC_VIDEO_DURATION_SECONDS",
                str(profile.duration_seconds),
            )
        ),
    )
    resolution = os.environ.get("TR_SYNTHETIC_VIDEO_RESOLUTION", profile.resolution)
    audio_override = os.environ.get("TR_SYNTHETIC_VIDEO_GENERATE_AUDIO")
    generate_audio = (
        profile.generate_audio
        if audio_override is None
        else audio_override.strip().casefold() in {"1", "true", "yes", "on"}
    )
    timeout_seconds = float(os.environ.get("TR_SYNTHETIC_VIDEO_TIMEOUT_SECONDS", "900"))
    poll_interval_seconds = max(
        0.0,
        float(os.environ.get("TR_SYNTHETIC_VIDEO_POLL_INTERVAL_SECONDS", "5")),
    )
    control_plane = os.environ.get(
        "TR_SYNTHETIC_CONTROL_PLANE_URL",
        "https://trustedrouter.com",
    )
    ingest_url = os.environ.get(
        "TR_SYNTHETIC_INGEST_URL",
        f"{control_plane.rstrip('/')}/v1/internal/synthetic/samples",
    )
    idempotency_prefix = os.environ.get(
        "TR_SYNTHETIC_VIDEO_IDEMPOTENCY_PREFIX",
        "trustedrouter-daily-video",
    )
    idempotency_key = f"{idempotency_prefix}-{run_day:%Y-%m-%d}"
    target = SyntheticTarget("canonical", settings.api_base_url, settings.primary_region)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        sample = await video_generation_probe(
            client,
            target,
            monitor_region=monitor_region,
            api_key=api_key,
            idempotency_key=idempotency_key,
            model=model,
            provider=provider,
            duration_seconds=duration_seconds,
            resolution=resolution,
            generate_audio=generate_audio,
            poll_interval_seconds=poll_interval_seconds,
            total_timeout_seconds=timeout_seconds,
        )
        response = await client.post(
            ingest_url,
            headers={"x-trustedrouter-internal-token": internal_token},
            json={"samples": [sample.public_dict()]},
        )

    print(
        json.dumps(
            {
                "probe_type": sample.probe_type,
                "status": sample.status,
                "http_status": sample.http_status,
                "error_type": sample.error_type,
                "model": sample.model,
                "provider": sample.selected_provider or sample.provider,
                "expected_cost_microdollars": profile.expected_cost_microdollars,
                "generation_id": sample.generation_id,
                "cost_microdollars": sample.cost_microdollars,
                "ingest_status": response.status_code,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if sample.status == "up" and response.status_code == 200 else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
