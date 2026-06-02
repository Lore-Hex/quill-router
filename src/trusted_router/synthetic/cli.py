from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx

from trusted_router.config import Settings, get_settings
from trusted_router.storage_models import SyntheticProbeSample
from trusted_router.synthetic.probes import (
    gateway_billing_probe,
    gateway_fallback_probe,
    run_synthetic_once,
)

# Inside-a-single-cron-invocation cadence. Cloud Scheduler is minute-
# granularity at best (`* * * * *`); to get sub-minute sampling we
# run the probe multiple times per invocation with a sleep between
# starts. 2 passes × 30s spacing = ~32K samples/day per region pair,
# fits comfortably in a 60s scheduler tick on Cloud Run Job defaults.
#
# Going more aggressive (6 × 10s for ~96K samples/day) caused probe
# executions to stack up under load: with default 1 CPU / 512Mi the
# concurrent TLS handshakes serialized, individual probe latency
# ballooned from ~2s to ~12s, and 60s cron ticks fired faster than
# 90-400s executions could finish. Bumping Cloud Run Job to 2 CPU /
# 1Gi in synthetic.sh should make 10s feasible again — try that in
# a separate PR after watching a stable 30s baseline.
# Override via TR_SYNTHETIC_RUNS_PER_INVOCATION (1 = old behaviour).
_DEFAULT_RUNS_PER_INVOCATION = 2
_DEFAULT_RUN_SPACING_SECONDS = 30.0


async def _one_probe_pass(
    *, settings: Settings, monitor_region: str, control_plane: str,
    internal_token: str | None, api_key: str | None,
    timeout: httpx.Timeout,
) -> list[SyntheticProbeSample]:
    samples = await run_synthetic_once(
        settings, monitor_region=monitor_region, api_key=api_key,
    )
    if api_key and internal_token:
        async with httpx.AsyncClient(timeout=timeout) as client:
            samples.extend(
                await gateway_billing_probe(
                    client,
                    control_plane_base_url=control_plane,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    internal_token=internal_token,
                    model=settings.synthetic_monitor_model,
                )
            )
            samples.extend(
                await gateway_fallback_probe(
                    client,
                    control_plane_base_url=control_plane,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    internal_token=internal_token,
                    model=settings.synthetic_monitor_model,
                )
            )
    return samples


async def run() -> int:
    settings = get_settings()
    monitor_region = (
        os.environ.get("TR_SYNTHETIC_MONITOR_REGION")
        or settings.synthetic_monitor_region
        or settings.primary_region
    )
    control_plane = os.environ.get("TR_SYNTHETIC_CONTROL_PLANE_URL", "https://trustedrouter.com")
    internal_token = settings.internal_gateway_token
    api_key = settings.synthetic_monitor_api_key
    timeout = httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
    runs_per_invocation = max(
        1,
        int(
            os.environ.get(
                "TR_SYNTHETIC_RUNS_PER_INVOCATION",
                str(_DEFAULT_RUNS_PER_INVOCATION),
            )
        ),
    )
    run_spacing_seconds = float(
        os.environ.get(
            "TR_SYNTHETIC_RUN_SPACING_SECONDS",
            str(_DEFAULT_RUN_SPACING_SECONDS),
        )
    )

    all_samples: list[SyntheticProbeSample] = []
    pass_start_monotonic = time.monotonic()
    for pass_idx in range(runs_per_invocation):
        all_samples.extend(
            await _one_probe_pass(
                settings=settings,
                monitor_region=monitor_region,
                control_plane=control_plane,
                internal_token=internal_token,
                api_key=api_key,
                timeout=timeout,
            )
        )
        # Sleep until the next probe pass should start, but only if
        # there IS a next pass. Compensates for the time the probe
        # itself took so the spacing is between pass-starts, not
        # pass-ends.
        if pass_idx + 1 < runs_per_invocation:
            target = (pass_idx + 1) * run_spacing_seconds
            elapsed = time.monotonic() - pass_start_monotonic
            to_sleep = target - elapsed
            if to_sleep > 0:
                await asyncio.sleep(to_sleep)

    ingest_url = os.environ.get(
        "TR_SYNTHETIC_INGEST_URL",
        f"{control_plane.rstrip('/')}/v1/internal/synthetic/samples",
    )
    if not internal_token:
        for sample in all_samples:
            print(sample.public_dict())
        print("TR_INTERNAL_GATEWAY_TOKEN is required to ingest samples", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            ingest_url,
            headers={"x-trustedrouter-internal-token": internal_token},
            json={"samples": [sample.public_dict() for sample in all_samples]},
        )
    print(response.text)
    return 0 if response.status_code == 200 else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
