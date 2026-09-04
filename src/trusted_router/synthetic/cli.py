from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import sys
import time
import uuid
from dataclasses import asdict
from typing import Any

import httpx

from trusted_router.client_context import parse_gateway_request_id
from trusted_router.config import Settings, get_settings
from trusted_router.provider_reliability import model_deadlines
from trusted_router.storage_models import ProviderBenchmarkSample, SyntheticProbeSample
from trusted_router.synthetic.internal_auth import (
    synthetic_observer_token,
    synthetic_transaction_token,
)
from trusted_router.synthetic.probes import (
    DEFAULT_SYNTHETIC_BILLING_CONCURRENCY,
    SyntheticTarget,
    _attested_ssl_context,
    choose_rotation_target,
    client_telemetry_canary_probe,
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
_REMEDIATOR_OVERLAP_MESSAGE = "Synthetic operation is already in progress"
_STAGE_D_PROBE_MODEL = "trustedrouter/cheap"


async def streaming_chat_completion_probe(
    client: httpx.AsyncClient,
    *,
    api_base_url: str,
    api_key: str,
    model: str,
    idempotency_key: str,
) -> str:
    """Require a real SSE first event, terminal chunk, and ``[DONE]`` marker."""

    url = f"{api_base_url.rstrip('/')}/chat/completions"
    saw_data = False
    saw_terminal = False
    saw_done = False
    gateway_request_id: str | None = None
    async with client.stream(
        "POST",
        url,
        headers={
            "authorization": f"Bearer {api_key}",
            "idempotency-key": idempotency_key,
            "accept": "text/event-stream",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "stream": True,
        },
    ) as response:
        response.raise_for_status()
        gateway_request_id = parse_gateway_request_id(response.headers.get("x-request-id"))
        if gateway_request_id is None:
            raise RuntimeError("streaming probe returned an invalid x-request-id")
        if "text/event-stream" not in response.headers.get("content-type", "").lower():
            raise RuntimeError("streaming probe did not return text/event-stream")
        async for line in response.aiter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                if not saw_data:
                    raise RuntimeError("streaming probe first SSE field was not data")
                continue
            payload = line.removeprefix("data:").strip()
            if not payload:
                continue
            saw_data = True
            if payload == "[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise RuntimeError("streaming probe returned invalid SSE JSON") from exc
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            if isinstance(choices, list) and any(
                isinstance(choice, dict) and choice.get("finish_reason") is not None
                for choice in choices
            ):
                saw_terminal = True
    if not saw_data:
        raise RuntimeError("streaming probe returned no SSE data")
    if not saw_terminal or not saw_done:
        raise RuntimeError("streaming probe did not return a valid terminal")
    assert gateway_request_id is not None
    return gateway_request_id


async def assert_stage_d_authorization(
    client: httpx.AsyncClient,
    *,
    control_plane_base_url: str,
    internal_gateway_token: str,
    gateway_request_id: str,
    expected_boot_kid: str | None = None,
    timeout_seconds: float = 15.0,
) -> None:
    """Poll the router's cross-repository evidence contract after a stream."""

    expected_keys = {
        "authorization_id",
        "gateway_request_id",
        "workspace_id",
        "authorization_kind",
        "settled",
        "disposition",
        "stage_d_boot_kid",
        "heartbeat_seq",
    }
    url = (
        f"{control_plane_base_url.rstrip('/')}/v1/internal/gateway/authorizations/"
        f"by-gateway-request-id/{gateway_request_id}"
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = await client.get(
            url,
            headers={"authorization": f"Bearer {internal_gateway_token}"},
        )
        if response.status_code != 404:
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or set(data) != expected_keys:
                raise RuntimeError("Stage D evidence response has an invalid key set")
            if data["gateway_request_id"] != gateway_request_id:
                raise RuntimeError("Stage D evidence response used another gateway request id")
            if type(data["settled"]) is not bool:  # noqa: E721 - bool must exclude int
                raise RuntimeError("Stage D evidence settled field is not boolean")
            if data["settled"]:
                if not isinstance(data["authorization_id"], str):
                    raise RuntimeError("Stage D evidence authorization id is not a string")
                if not isinstance(data["workspace_id"], str):
                    raise RuntimeError("Stage D evidence workspace id is not a string")
                if data["authorization_kind"] != "local_typed":
                    raise RuntimeError("Stage D probe authorization was not local_typed")
                disposition = data["disposition"]
                if disposition is not None and not isinstance(disposition, str):
                    raise RuntimeError("Stage D evidence disposition has an invalid type")
                if not isinstance(data["stage_d_boot_kid"], str):
                    raise RuntimeError("Stage D probe authorization has no stage_d_boot_kid")
                heartbeat_seq = data["heartbeat_seq"]
                if type(heartbeat_seq) is not int or heartbeat_seq <= 0:
                    raise RuntimeError("Stage D probe authorization has no durable heartbeat")
                if expected_boot_kid is not None and data["stage_d_boot_kid"] != expected_boot_kid:
                    raise RuntimeError(
                        "Stage D probe authorization used an unexpected boot kid"
                    )
                return
            if data["stage_d_boot_kid"] is not None and not isinstance(
                data["stage_d_boot_kid"], str
            ):
                raise RuntimeError("Stage D probe authorization has no stage_d_boot_kid")
        if time.monotonic() >= deadline:
            raise RuntimeError("Stage D probe authorization did not settle with durable evidence")
        await asyncio.sleep(0.25)


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
            # The inference probes' SDK sessions beacon to this plane.
            control_plane_base_url=control_plane,
        )
    )
    if not api_key:
        return await synthetic_task
    canary_samples: list[SyntheticProbeSample] = []
    if not internal_token:
        # No internal token means no ledger probes, but the client-telemetry
        # canary needs only the monitor key: it is the positive control that
        # proves the beacon path (route -> outbox -> ClickHouse) is alive on
        # every cloud, and the GCP monitor is THIS job, not the in-process
        # scheduler behind /internal/synthetic/run.
        async with httpx.AsyncClient(timeout=timeout) as client:
            canary_samples.append(
                await client_telemetry_canary_probe(
                    client,
                    control_plane_base_url=control_plane,
                    monitor_region=monitor_region,
                    api_key=api_key,
                )
            )
        return [*(await synthetic_task), *canary_samples]
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
        # Client-telemetry canary: one schema-valid synthetic batch per pass
        # through the public beacon route with the monitor key. Not a ledger
        # probe (no reserve/settle), so it needs no limiter slot.
        gateway_samples.append(
            await client_telemetry_canary_probe(
                client,
                control_plane_base_url=control_plane,
                monitor_region=monitor_region,
                api_key=api_key,
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
        if response.status_code == 429:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if (
                isinstance(error, dict)
                and error.get("message") == _REMEDIATOR_OVERLAP_MESSAGE
            ):
                print("remediator skipped: another pass is already in progress")
                return True
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


async def _run_scheduled_remediator(
    *,
    url: str,
    internal_token: str,
    timeout_seconds: float,
) -> bool:
    """Own the HTTP client so remediation can overlap independent probes."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
        return await _post_remediator(
            client,
            url=url,
            internal_token=internal_token,
            timeout_seconds=timeout_seconds,
        )


async def run(*, expect_stage_d: bool = False) -> int:
    settings = get_settings()
    monitor_region = (
        os.environ.get("TR_SYNTHETIC_MONITOR_REGION")
        or settings.synthetic_monitor_region
        or settings.primary_region
    )
    control_plane = os.environ.get("TR_SYNTHETIC_CONTROL_PLANE_URL", "https://trustedrouter.com")
    observer_token = synthetic_observer_token(settings)
    transaction_token = synthetic_transaction_token(settings)
    api_key = (
        os.environ.get("TR_STAGE_D_PROBE_API_KEY")
        if expect_stage_d
        else settings.synthetic_monitor_api_key
    )
    timeout = httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
    remediator_url = os.environ.get("TR_SYNTHETIC_REMEDIATOR_URL")
    remediator_timeout_seconds = max(
        30.0,
        float(
            os.environ.get(
                "TR_SYNTHETIC_REMEDIATOR_TIMEOUT_SECONDS",
                str(_DEFAULT_REMEDIATOR_TIMEOUT_SECONDS),
            )
        ),
    )
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
    stream_probe_ok = True
    if not throughput_only:
        if not api_key:
            if expect_stage_d:
                print(
                    "TR_STAGE_D_PROBE_API_KEY is required for the Stage D probe",
                    file=sys.stderr,
                )
                return 2
        else:
            if expect_stage_d and not settings.internal_gateway_token:
                print(
                    "TR_INTERNAL_GATEWAY_TOKEN is required for the Stage D evidence lookup",
                    file=sys.stderr,
                )
                return 2
            idempotency_key = f"stage-d-stream-probe-{uuid.uuid4()}"
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    gateway_request_id = await streaming_chat_completion_probe(
                        client,
                        api_base_url=settings.api_base_url,
                        api_key=api_key,
                        model=(
                            _STAGE_D_PROBE_MODEL
                            if expect_stage_d
                            else settings.synthetic_monitor_model
                        ),
                        idempotency_key=idempotency_key,
                    )
                    if expect_stage_d:
                        assert settings.internal_gateway_token is not None
                        await assert_stage_d_authorization(
                            client,
                            control_plane_base_url=control_plane,
                            internal_gateway_token=settings.internal_gateway_token,
                            gateway_request_id=gateway_request_id,
                            expected_boot_kid=(
                                os.environ.get("TR_STAGE_D_PROBE_BOOT_KID") or None
                            ),
                            timeout_seconds=float(
                                os.environ.get(
                                    "TR_STAGE_D_PROBE_LOOKUP_TIMEOUT_SECONDS", "60"
                                )
                            ),
                        )
            except Exception as exc:  # noqa: BLE001 - a failed probe fails the job
                print(
                    f"streaming probe failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                stream_probe_ok = False
    if expect_stage_d:
        # The dedicated key belongs to a separate local-typed workspace and
        # cannot use the ordinary monitor-only model or run the monitor key's
        # billing probes. This job's sole contract is the real stream plus its
        # durable Stage D evidence.
        return 0 if stream_probe_ok else 1
    remediator_task = (
        asyncio.create_task(
            _run_scheduled_remediator(
                url=remediator_url,
                internal_token=observer_token,
                timeout_seconds=remediator_timeout_seconds,
            )
        )
        if remediator_url and observer_token and not throughput_only
        else None
    )
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
                # Split observer jobs never hold billing authority. The
                # explicit combined migration bridge still does, so it must
                # keep exercising authorize/settle/fallback until the
                # internal service takes ownership.
                internal_token=transaction_token,
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
    if not observer_token:
        for probe_sample in all_samples:
            print(probe_sample.public_dict())
        for benchmark_sample in benchmark_samples:
            print(asdict(benchmark_sample))
        print("TR_OBSERVER_INTERNAL_TOKEN is required to ingest samples", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(timeout=timeout) as client:
        ok = stream_probe_ok
        if all_samples:
            response = await client.post(
                ingest_url,
                headers={"x-trustedrouter-internal-token": observer_token},
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
                headers={"x-trustedrouter-internal-token": observer_token},
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
                    internal_token=observer_token,
                )
    if remediator_task is not None:
        ok = (await remediator_task) and ok
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run recurring synthetic checks, including a streaming SSE completion. "
            "--expect-stage-d requires a heartbeat-capable local-typed key whose "
            "workspace is in TR_STAGE_D_PILOT_WORKSPACE_IDS and outside the "
            "regional-quota pilot path."
        )
    )
    parser.add_argument(
        "--expect-stage-d",
        action="store_true",
        help=(
            "also require the durable authorization to have stage_d_boot_kid and "
            "heartbeat_seq > 0"
        ),
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(expect_stage_d=args.expect_stage_d))


if __name__ == "__main__":
    raise SystemExit(main())
