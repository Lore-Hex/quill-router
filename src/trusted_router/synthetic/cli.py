from __future__ import annotations

import asyncio
import os
import sys

import httpx

from trusted_router.config import get_settings
from trusted_router.synthetic.probes import (
    gateway_billing_probe,
    gateway_fallback_probe,
    run_synthetic_once,
)


async def run() -> int:
    settings = get_settings()
    monitor_region = os.environ.get("TR_SYNTHETIC_MONITOR_REGION") or settings.synthetic_monitor_region
    control_plane = os.environ.get("TR_SYNTHETIC_CONTROL_PLANE_URL", "https://trustedrouter.com")
    internal_token = settings.internal_gateway_token
    api_key = settings.synthetic_monitor_api_key
    timeout = httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
    samples = await run_synthetic_once(settings, monitor_region=monitor_region, api_key=api_key)
    if api_key and internal_token:
        async with httpx.AsyncClient(timeout=timeout) as client:
            samples.extend(
                await gateway_billing_probe(
                    client,
                    control_plane_base_url=control_plane,
                    monitor_region=monitor_region or settings.primary_region,
                    api_key=api_key,
                    internal_token=internal_token,
                    model=settings.synthetic_monitor_model,
                )
            )
            samples.extend(
                await gateway_fallback_probe(
                    client,
                    control_plane_base_url=control_plane,
                    monitor_region=monitor_region or settings.primary_region,
                    api_key=api_key,
                    internal_token=internal_token,
                    model=settings.synthetic_monitor_model,
                )
            )

    ingest_url = os.environ.get(
        "TR_SYNTHETIC_INGEST_URL",
        f"{control_plane.rstrip('/')}/v1/internal/synthetic/samples",
    )
    if not internal_token:
        for sample in samples:
            print(sample.public_dict())
        print("TR_INTERNAL_GATEWAY_TOKEN is required to ingest samples", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            ingest_url,
            headers={"x-trustedrouter-internal-token": internal_token},
            json={"samples": [sample.public_dict() for sample in samples]},
        )
    print(response.text)
    return 0 if response.status_code == 200 else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
