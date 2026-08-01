from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime

import httpx

from trusted_router.config import get_settings
from trusted_router.synthetic.probes import (
    VIDEO_GENERATION_DURATION_SECONDS,
    VIDEO_GENERATION_MODEL,
    VIDEO_GENERATION_PROVIDER,
    VIDEO_GENERATION_RESOLUTION,
    SyntheticTarget,
    video_generation_probe,
)


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
    model = os.environ.get("TR_SYNTHETIC_VIDEO_MODEL", VIDEO_GENERATION_MODEL)
    provider = os.environ.get("TR_SYNTHETIC_VIDEO_PROVIDER", VIDEO_GENERATION_PROVIDER)
    duration_seconds = max(
        1,
        int(
            os.environ.get(
                "TR_SYNTHETIC_VIDEO_DURATION_SECONDS",
                str(VIDEO_GENERATION_DURATION_SECONDS),
            )
        ),
    )
    resolution = os.environ.get("TR_SYNTHETIC_VIDEO_RESOLUTION", VIDEO_GENERATION_RESOLUTION)
    timeout_seconds = float(os.environ.get("TR_SYNTHETIC_VIDEO_TIMEOUT_SECONDS", "300"))
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
    idempotency_key = f"{idempotency_prefix}-{datetime.now(UTC):%Y-%m-%d}"
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
