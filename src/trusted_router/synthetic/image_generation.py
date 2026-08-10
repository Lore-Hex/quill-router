from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

from trusted_router.config import get_settings
from trusted_router.synthetic.probes import (
    IMAGE_GENERATION_MODEL,
    IMAGE_GENERATION_PROVIDER,
    SyntheticTarget,
    image_generation_probe,
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
    model = os.environ.get("TR_SYNTHETIC_IMAGE_MODEL", IMAGE_GENERATION_MODEL)
    provider = os.environ.get("TR_SYNTHETIC_IMAGE_PROVIDER", IMAGE_GENERATION_PROVIDER)
    timeout_seconds = float(os.environ.get("TR_SYNTHETIC_IMAGE_TIMEOUT_SECONDS", "120"))
    confirmation_delay_seconds = max(
        0.0,
        float(
            os.environ.get(
                "TR_SYNTHETIC_IMAGE_CONFIRMATION_DELAY_SECONDS",
                "2",
            )
        ),
    )
    control_plane = os.environ.get(
        "TR_SYNTHETIC_CONTROL_PLANE_URL",
        "https://trustedrouter.com",
    )
    ingest_url = os.environ.get(
        "TR_SYNTHETIC_INGEST_URL",
        f"{control_plane.rstrip('/')}/v1/internal/synthetic/samples",
    )
    target = SyntheticTarget("canonical", settings.api_base_url, settings.primary_region)
    timeout = httpx.Timeout(timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        samples = [
            await image_generation_probe(
                client,
                target,
                monitor_region=monitor_region,
                api_key=api_key,
                model=model,
                provider=provider,
            )
        ]
        if samples[-1].status != "up":
            if confirmation_delay_seconds:
                await asyncio.sleep(confirmation_delay_seconds)
            samples.append(
                await image_generation_probe(
                    client,
                    target,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    model=model,
                    provider=provider,
                )
            )

        sample = samples[-1]
        response = await client.post(
            ingest_url,
            headers={"x-trustedrouter-internal-token": internal_token},
            json={"samples": [item.public_dict() for item in samples]},
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
                "attempts": len(samples),
                "total_cost_microdollars": sum(item.cost_microdollars or 0 for item in samples),
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
