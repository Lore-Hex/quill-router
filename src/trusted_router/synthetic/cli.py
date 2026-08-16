from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import sys
import time
from dataclasses import asdict
from typing import Any

import httpx

from trusted_router.config import Settings, get_settings
from trusted_router.provider_reliability import model_deadlines
from trusted_router.storage_models import ProviderBenchmarkSample, SyntheticProbeSample
from trusted_router.synthetic.probes import (
    DEFAULT_SYNTHETIC_BILLING_CONCURRENCY,
    SyntheticTarget,
    _attested_ssl_context,
    choose_rotation_target,
    gateway_billing_probe,
    gateway_fallback_probe,
    provider_rotation_probe,
    provider_throughput_probe,
    rotation_candidates,
    run_synthetic_once,
)
from trusted_router.synthetic.throughput import (
    THROUGHPUT_INTERVAL_SECONDS,
    choose_throughput_target,
    throughput_candidates,
)

# Inside-a-single-cron-invocation cadence. Cloud Scheduler is minute-granularity
# at best (`* * * * *`). Keep production to one bounded pass per invocation;
# sub-minute passes made provider-effective timeouts stack up and caused the
# monitor itself to hit Cloud Run Job timeouts.
_DEFAULT_RUNS_PER_INVOCATION = 1
_DEFAULT_RUN_SPACING_SECONDS = 30.0

# Provider/model rotation probe — how many random provider+model samples to
# take per pass. Dark-launched: only runs when TR_SYNTHETIC_ROTATION_ENABLED is
# truthy, so we can watch real token spend for ~24h before ramping.
_DEFAULT_ROTATION_PER_PASS = 4
_DEFAULT_THROUGHPUT_ROUTE_LIMIT = 200
_DEFAULT_THROUGHPUT_MAX_TOKENS = 512
_DEFAULT_THROUGHPUT_MINIMUM_OUTPUT_TOKENS = 128
_DEFAULT_THROUGHPUT_TIMEOUT_SECONDS = 90.0
_DEFAULT_THROUGHPUT_TIMEOUT_CEILING_SECONDS = 210.0
_DEFAULT_THROUGHPUT_INTERVAL_SECONDS = THROUGHPUT_INTERVAL_SECONDS
_DEFAULT_REMEDIATOR_TIMEOUT_SECONDS = 90.0


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


async def _one_probe_pass(
    *,
    settings: Settings,
    monitor_region: str,
    control_plane: str,
    internal_token: str | None,
    api_key: str | None,
    timeout: httpx.Timeout,
    billing_semaphore: asyncio.Semaphore | None = None,
) -> list[SyntheticProbeSample]:
    limiter = billing_semaphore or asyncio.Semaphore(DEFAULT_SYNTHETIC_BILLING_CONCURRENCY)
    synthetic_task = asyncio.create_task(
        run_synthetic_once(
            settings,
            monitor_region=monitor_region,
            api_key=api_key,
            billing_semaphore=limiter,
        )
    )
    if not (api_key and internal_token):
        return await synthetic_task
    # Keep ledger-style probes ordered. They reserve, settle, and refund
    # against the same synthetic key; running them concurrently turns the
    # monitor into a Spanner/key-row contention test and can create false
    # router-core failures.
    gateway_samples: list[SyntheticProbeSample] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with limiter:
            gateway_samples.extend(
                await gateway_billing_probe(
                    client,
                    control_plane_base_url=control_plane,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    internal_token=internal_token,
                    model=settings.synthetic_monitor_model,
                )
            )
        async with limiter:
            gateway_samples.extend(
                await gateway_fallback_probe(
                    client,
                    control_plane_base_url=control_plane,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    internal_token=internal_token,
                    model=settings.synthetic_monitor_model,
                )
            )
    synthetic_samples = await synthetic_task
    return [*synthetic_samples, *gateway_samples]


async def _probe_and_rotation_pass(
    *,
    settings: Settings,
    monitor_region: str,
    control_plane: str,
    internal_token: str | None,
    api_key: str | None,
    timeout: httpx.Timeout,
    rotation_enabled: bool,
    rotation_per_pass: int,
    rotation_rng: random.Random,
    throughput_enabled: bool = False,
    throughput_region: str = "us-central1",
    throughput_route_limit: int = _DEFAULT_THROUGHPUT_ROUTE_LIMIT,
    throughput_max_tokens: int = _DEFAULT_THROUGHPUT_MAX_TOKENS,
    throughput_minimum_output_tokens: int = _DEFAULT_THROUGHPUT_MINIMUM_OUTPUT_TOKENS,
    throughput_timeout_seconds: float = _DEFAULT_THROUGHPUT_TIMEOUT_SECONDS,
    throughput_timeout_ceiling_seconds: float = _DEFAULT_THROUGHPUT_TIMEOUT_CEILING_SECONDS,
    throughput_interval_seconds: int = _DEFAULT_THROUGHPUT_INTERVAL_SECONDS,
    billing_concurrency: int = DEFAULT_SYNTHETIC_BILLING_CONCURRENCY,
) -> tuple[list[SyntheticProbeSample], list[ProviderBenchmarkSample]]:
    billing_semaphore = asyncio.Semaphore(max(1, billing_concurrency))
    probe_task = asyncio.create_task(
        _one_probe_pass(
            settings=settings,
            monitor_region=monitor_region,
            control_plane=control_plane,
            internal_token=internal_token,
            api_key=api_key,
            timeout=timeout,
            billing_semaphore=billing_semaphore,
        )
    )
    benchmark_tasks: list[asyncio.Task[list[ProviderBenchmarkSample]]] = []
    if rotation_enabled and api_key:
        benchmark_tasks.append(
            asyncio.create_task(
                rotation_pass(
                    settings=settings,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    timeout=timeout,
                    count=rotation_per_pass,
                    rng=rotation_rng,
                    billing_semaphore=billing_semaphore,
                )
            )
        )
    if throughput_enabled and api_key and monitor_region == throughput_region:
        benchmark_tasks.append(
            asyncio.create_task(
                _throughput_pass(
                    settings=settings,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    route_limit=throughput_route_limit,
                    max_tokens=throughput_max_tokens,
                    minimum_output_tokens=throughput_minimum_output_tokens,
                    timeout_seconds=throughput_timeout_seconds,
                    timeout_ceiling_seconds=throughput_timeout_ceiling_seconds,
                    interval_seconds=throughput_interval_seconds,
                )
            )
        )
    if not benchmark_tasks:
        return await probe_task, []
    probe_samples, benchmark_groups = await asyncio.gather(
        probe_task,
        asyncio.gather(*benchmark_tasks),
    )
    benchmark_samples = [sample for group in benchmark_groups for sample in group]
    return probe_samples, benchmark_samples


async def rotation_pass(
    *,
    settings: Settings,
    monitor_region: str,
    api_key: str,
    timeout: httpx.Timeout,
    count: int,
    rng: random.Random,
    billing_semaphore: asyncio.Semaphore | None = None,
    models: frozenset[str] | None = None,
) -> list[ProviderBenchmarkSample]:
    """One rotation pass: `count` random provider+model picks, probed live.

    Public because /internal/synthetic/run also drives it — deployments
    without a monitor-pool CLI (the standalone EU cloud, where cadence
    comes from an EventBridge rule) get provider rotation through the
    route. `models` narrows the candidate pool to specific model ids so a
    caller can pin rotation to a family (e.g. the DSv4 models) without
    losing the equal-airtime-per-provider pick.
    """
    pool = rotation_candidates()
    if models is not None:
        pool = {
            provider: [model for model in candidates if model in models]
            for provider, candidates in pool.items()
        }
        pool = {provider: candidates for provider, candidates in pool.items() if candidates}
    # The rotation target must inherit the canonical target's TRANSPORT, not
    # just its URL.
    #
    # This probe had NEVER once succeeded on the AWS plane: 10,993 error
    # samples against 10 successes, and all 10 of those were test fixtures.
    # Every real attempt returned ConnectError, because the AWS gateway serves
    # a SELF-SIGNED certificate minted inside the enclave — trust comes from
    # the attestation binding the cert, not from a CA — and this function
    # built a plain httpx.AsyncClient whose default verification rejects it
    # before a byte of the request is sent.
    #
    # The failure was invisible for two reasons. The EventBridge Input pinned
    # rotation to two DeepSeek ids, so it looked like a narrow gap rather than
    # a dead path; and a benchmark sample that records status="error" is
    # indistinguishable, on a leaderboard, from a provider that is genuinely
    # down. The board was not reporting bad providers — it was reporting a
    # monitor that could not reach its own gateway.
    target = SyntheticTarget(
        "rotation",
        settings.api_base_url,
        monitor_region,
        attested=settings.synthetic_canonical_attested,
        expected_pcr0=settings.attestation_expected_pcr0,
    )
    verify: Any = _attested_ssl_context() if target.attested else True
    limiter = billing_semaphore or asyncio.Semaphore(DEFAULT_SYNTHETIC_BILLING_CONCURRENCY)
    probes = []
    async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
        for _ in range(max(0, count)):
            picked = choose_rotation_target(pool, rng)
            if picked is None:
                break
            provider, model = picked
            probes.append(
                _run_rotation_probe(
                    limiter,
                    client,
                    target,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    provider=provider,
                    model=model,
                    default_timeout_seconds=settings.synthetic_monitor_timeout_seconds,
                )
            )
        if not probes:
            return []
        return list(await asyncio.gather(*probes))


async def _run_rotation_probe(
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    **kwargs: Any,
) -> ProviderBenchmarkSample:
    async with semaphore:
        return await provider_rotation_probe(client, target, **kwargs)


async def _throughput_pass(
    *,
    settings: Settings,
    monitor_region: str,
    api_key: str,
    route_limit: int,
    max_tokens: int,
    minimum_output_tokens: int,
    timeout_seconds: float,
    timeout_ceiling_seconds: float,
    interval_seconds: int,
) -> list[ProviderBenchmarkSample]:
    candidates = throughput_candidates(limit=route_limit)
    picked = choose_throughput_target(
        candidates,
        interval_seconds=interval_seconds,
    )
    if picked is None:
        return []
    provider, model = picked
    model_timeout_seconds = max(
        timeout_seconds,
        model_deadlines(
            model,
            provider=provider,
            default_first_token_seconds=settings.synthetic_monitor_timeout_seconds,
        ).completion_seconds,
    )
    effective_timeout_seconds = min(
        model_timeout_seconds,
        max(timeout_ceiling_seconds, 1.0),
    )
    target = SyntheticTarget("throughput", settings.api_base_url, monitor_region)
    async with httpx.AsyncClient(timeout=httpx.Timeout(effective_timeout_seconds)) as client:
        return [
            await provider_throughput_probe(
                client,
                target,
                monitor_region=monitor_region,
                api_key=api_key,
                provider=provider,
                model=model,
                max_tokens=max_tokens,
                minimum_output_tokens=minimum_output_tokens,
                total_timeout_seconds=effective_timeout_seconds,
            )
        ]


async def _post_route_health(
    client: httpx.AsyncClient,
    *,
    url: str,
    internal_token: str,
) -> None:
    try:
        response = await client.post(
            url,
            headers={"x-trustedrouter-internal-token": internal_token},
        )
        response.raise_for_status()
        payload = response.json()
        flagged = payload.get("data", {}).get("flagged") if isinstance(payload, dict) else None
        if not isinstance(flagged, list):
            raise ValueError("route-health response did not contain a flagged list")
        print(f"route-health flagged: {len(flagged)}")
    except Exception as exc:
        print(f"route-health check failed: {exc}", file=sys.stderr)


async def _post_route_health_if_due(
    client: httpx.AsyncClient,
    *,
    url: str,
    internal_token: str,
    now: dt.datetime | None = None,
) -> None:
    # Fingerprinting groups emissions into one Sentry issue per route. Hourly
    # re-emission (~24/day/route) keeps the signal fresh without burning quota.
    every_pass = os.environ.get("TR_SYNTHETIC_ROUTE_HEALTH_EVERY_PASS") == "1"
    if not every_pass and (now or dt.datetime.now(dt.UTC)).minute >= 2:
        return
    await _post_route_health(client, url=url, internal_token=internal_token)


async def _post_remediator(
    client: httpx.AsyncClient,
    *,
    url: str,
    internal_token: str,
    timeout_seconds: float = _DEFAULT_REMEDIATOR_TIMEOUT_SECONDS,
) -> bool:
    """Run the control-plane remediator and make scheduler failures visible."""
    try:
        response = await client.post(
            url,
            headers={"x-trustedrouter-internal-token": internal_token},
            timeout=httpx.Timeout(timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
        decisions = payload.get("data", {}).get("decisions") if isinstance(payload, dict) else None
        if not isinstance(decisions, int):
            raise ValueError("remediator response did not contain a decision count")
        print(f"remediator decisions: {decisions}")
        return True
    except Exception as exc:
        print(f"remediator check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


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
    rotation_enabled = _env_flag("TR_SYNTHETIC_ROTATION_ENABLED")
    rotation_per_pass = max(
        0,
        int(os.environ.get("TR_SYNTHETIC_ROTATION_PER_PASS", str(_DEFAULT_ROTATION_PER_PASS))),
    )
    rotation_rng = random.Random()  # noqa: S311 - picks which model to probe, not cryptographic
    throughput_enabled = _env_flag("TR_SYNTHETIC_THROUGHPUT_ENABLED")
    throughput_only = _env_flag("TR_SYNTHETIC_THROUGHPUT_ONLY")
    throughput_region = os.environ.get("TR_SYNTHETIC_THROUGHPUT_REGION", "us-central1")
    throughput_route_limit = max(
        0,
        int(
            os.environ.get(
                "TR_SYNTHETIC_THROUGHPUT_ROUTE_LIMIT",
                str(_DEFAULT_THROUGHPUT_ROUTE_LIMIT),
            )
        ),
    )
    throughput_max_tokens = max(
        2,
        int(
            os.environ.get(
                "TR_SYNTHETIC_THROUGHPUT_MAX_TOKENS",
                str(_DEFAULT_THROUGHPUT_MAX_TOKENS),
            )
        ),
    )
    throughput_minimum_output_tokens = max(
        2,
        int(
            os.environ.get(
                "TR_SYNTHETIC_THROUGHPUT_MINIMUM_OUTPUT_TOKENS",
                str(_DEFAULT_THROUGHPUT_MINIMUM_OUTPUT_TOKENS),
            )
        ),
    )
    throughput_timeout_seconds = float(
        os.environ.get(
            "TR_SYNTHETIC_THROUGHPUT_TIMEOUT_SECONDS",
            str(_DEFAULT_THROUGHPUT_TIMEOUT_SECONDS),
        )
    )
    throughput_timeout_ceiling_seconds = float(
        os.environ.get(
            "TR_SYNTHETIC_THROUGHPUT_TIMEOUT_CEILING_SECONDS",
            str(_DEFAULT_THROUGHPUT_TIMEOUT_CEILING_SECONDS),
        )
    )
    throughput_interval_seconds = max(
        1,
        int(
            os.environ.get(
                "TR_SYNTHETIC_THROUGHPUT_INTERVAL_SECONDS",
                str(_DEFAULT_THROUGHPUT_INTERVAL_SECONDS),
            )
        ),
    )
    billing_concurrency = max(
        1,
        int(
            os.environ.get(
                "TR_SYNTHETIC_BILLING_CONCURRENCY",
                str(DEFAULT_SYNTHETIC_BILLING_CONCURRENCY),
            )
        ),
    )
    start_delay_seconds = max(
        0.0,
        float(os.environ.get("TR_SYNTHETIC_START_DELAY_SECONDS", "0")),
    )

    all_samples: list[SyntheticProbeSample] = []
    benchmark_samples: list[ProviderBenchmarkSample] = []
    if start_delay_seconds:
        await asyncio.sleep(start_delay_seconds)
    if throughput_only:
        if not throughput_enabled:
            print(
                "TR_SYNTHETIC_THROUGHPUT_ONLY requires TR_SYNTHETIC_THROUGHPUT_ENABLED",
                file=sys.stderr,
            )
            return 2
        if monitor_region != throughput_region:
            print(
                "throughput-only job must run in TR_SYNTHETIC_THROUGHPUT_REGION",
                file=sys.stderr,
            )
            return 2
        if not api_key:
            print(
                "TR_SYNTHETIC_MONITOR_API_KEY is required for throughput probes",
                file=sys.stderr,
            )
            return 2
        benchmark_samples.extend(
            await _throughput_pass(
                settings=settings,
                monitor_region=monitor_region,
                api_key=api_key,
                route_limit=throughput_route_limit,
                max_tokens=throughput_max_tokens,
                minimum_output_tokens=throughput_minimum_output_tokens,
                timeout_seconds=throughput_timeout_seconds,
                timeout_ceiling_seconds=throughput_timeout_ceiling_seconds,
                interval_seconds=throughput_interval_seconds,
            )
        )
    else:
        pass_start_monotonic = time.monotonic()
        for pass_idx in range(runs_per_invocation):
            pass_samples, pass_benchmark_samples = await _probe_and_rotation_pass(
                settings=settings,
                monitor_region=monitor_region,
                control_plane=control_plane,
                internal_token=internal_token,
                api_key=api_key,
                timeout=timeout,
                rotation_enabled=rotation_enabled,
                rotation_per_pass=rotation_per_pass,
                rotation_rng=rotation_rng,
                throughput_enabled=throughput_enabled,
                throughput_region=throughput_region,
                throughput_route_limit=throughput_route_limit,
                throughput_max_tokens=throughput_max_tokens,
                throughput_minimum_output_tokens=throughput_minimum_output_tokens,
                throughput_timeout_seconds=throughput_timeout_seconds,
                throughput_timeout_ceiling_seconds=throughput_timeout_ceiling_seconds,
                throughput_interval_seconds=throughput_interval_seconds,
                billing_concurrency=billing_concurrency,
            )
            all_samples.extend(pass_samples)
            benchmark_samples.extend(pass_benchmark_samples)
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
        for probe_sample in all_samples:
            print(probe_sample.public_dict())
        for benchmark_sample in benchmark_samples:
            print(asdict(benchmark_sample))
        print("TR_INTERNAL_GATEWAY_TOKEN is required to ingest samples", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(timeout=timeout) as client:
        ok = True
        if all_samples:
            response = await client.post(
                ingest_url,
                headers={"x-trustedrouter-internal-token": internal_token},
                json={"samples": [sample.public_dict() for sample in all_samples]},
            )
            print(response.text)
            ok = response.status_code == 200
        if benchmark_samples:
            benchmark_url = os.environ.get(
                "TR_SYNTHETIC_BENCHMARK_INGEST_URL",
                f"{control_plane.rstrip('/')}/v1/internal/synthetic/benchmark",
            )
            bench_response = await client.post(
                benchmark_url,
                headers={"x-trustedrouter-internal-token": internal_token},
                json={"samples": [asdict(sample) for sample in benchmark_samples]},
            )
            print(bench_response.text)
            ok = ok and bench_response.status_code == 200
            if not throughput_only:
                route_health_url = os.environ.get(
                    "TR_SYNTHETIC_ROUTE_HEALTH_URL",
                    f"{control_plane.rstrip('/')}/v1/internal/synthetic/route-health",
                )
                await _post_route_health_if_due(
                    client,
                    url=route_health_url,
                    internal_token=internal_token,
                )
        remediator_url = os.environ.get("TR_SYNTHETIC_REMEDIATOR_URL")
        if remediator_url:
            remediator_timeout_seconds = max(
                30.0,
                float(
                    os.environ.get(
                        "TR_SYNTHETIC_REMEDIATOR_TIMEOUT_SECONDS",
                        str(_DEFAULT_REMEDIATOR_TIMEOUT_SECONDS),
                    )
                ),
            )
            ok = (
                await _post_remediator(
                    client,
                    url=remediator_url,
                    internal_token=internal_token,
                    timeout_seconds=remediator_timeout_seconds,
                )
                and ok
            )
    return 0 if ok else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
