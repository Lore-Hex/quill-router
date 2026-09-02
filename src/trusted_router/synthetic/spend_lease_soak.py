from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

from trusted_router.config import get_settings
from trusted_router.storage_gcp_secrets import secret_manager_text_loader
from trusted_router.synthetic.internal_auth import synthetic_observer_token
from trusted_router.synthetic.probes import SyntheticTarget, spend_lease_soak_probe


async def run() -> int:
    settings = get_settings()
    if not settings.spend_lease_soak_probe_enabled:
        print('{"enabled":false,"probe_type":"spend_lease_soak"}')
        return 0

    internal_token = synthetic_observer_token(settings)
    if not internal_token:
        print("TR_OBSERVER_INTERNAL_TOKEN is required", file=sys.stderr)
        return 2

    api_key_loader = secret_manager_text_loader(
        project_id=settings.gcp_project_id,
        secret_name=settings.spend_lease_probe_key_secret,
    )
    api_key = await asyncio.to_thread(api_key_loader)
    monitor_region = os.environ.get("TR_SYNTHETIC_MONITOR_REGION", "us-central1")
    control_plane = os.environ.get(
        "TR_SYNTHETIC_CONTROL_PLANE_URL",
        "https://trustedrouter.com",
    )
    ingest_url = os.environ.get(
        "TR_SYNTHETIC_INGEST_URL",
        f"{control_plane.rstrip('/')}/v1/internal/synthetic/samples",
    )
    target = SyntheticTarget("canonical", settings.api_base_url, settings.primary_region)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
    ) as client:
        sample = await spend_lease_soak_probe(
            client,
            target,
            monitor_region=monitor_region,
            api_key=api_key,
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
                "provider": sample.selected_provider,
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
