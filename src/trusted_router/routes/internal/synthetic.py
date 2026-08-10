from __future__ import annotations

import asyncio
import datetime as dt
import random
from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.routes.helpers import json_body
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.storage import STORE, ProviderBenchmarkSample, SyntheticProbeSample
from trusted_router.storage_models import FUTURE_SAMPLE_SKEW_SECONDS, scrub_provider_error_message
from trusted_router.synthetic.alerts import alert_on_failure_streak
from trusted_router.synthetic.cli import rotation_pass
from trusted_router.synthetic.probes import (
    gateway_billing_probe,
    gateway_fallback_probe,
    run_synthetic_once,
)
from trusted_router.synthetic.route_health import (
    evaluate_route_health,
    report_image_generation_failures,
    report_route_health,
    report_video_generation_failures,
)
from trusted_router.types import ErrorType


async def _run_and_record(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    monitor_region = _optional_str(body.get("monitor_region"))
    # Which control plane the billing probes (authorize+settle) hit.
    # Precedence: request body > settings > canonical GCP plane. The
    # settings tier exists because the hardcoded fallback is a
    # wrong-cloud trap for standalone deployments: the EU service
    # probing https://trustedrouter.com would record the US plane's
    # health under an EU monitor region.
    control_plane_base_url = str(
        body.get("control_plane_base_url")
        or settings.synthetic_control_plane_base_url
        or "https://trustedrouter.com"
    )
    samples = await run_synthetic_once(settings, monitor_region=monitor_region)
    if settings.synthetic_monitor_api_key and settings.internal_gateway_token:
        timeout = httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            samples.extend(
                await gateway_billing_probe(
                    client,
                    control_plane_base_url=control_plane_base_url,
                    monitor_region=monitor_region
                    or settings.synthetic_monitor_region
                    or settings.primary_region,
                    api_key=settings.synthetic_monitor_api_key,
                    internal_token=settings.internal_gateway_token,
                    model=settings.synthetic_monitor_model,
                )
            )
            samples.extend(
                await gateway_fallback_probe(
                    client,
                    control_plane_base_url=control_plane_base_url,
                    monitor_region=monitor_region
                    or settings.synthetic_monitor_region
                    or settings.primary_region,
                    api_key=settings.synthetic_monitor_api_key,
                    internal_token=settings.internal_gateway_token,
                    model=settings.synthetic_monitor_model,
                )
            )
    benchmark_recorded = 0
    rotation_count = _rotation_count(body)
    if rotation_count and settings.synthetic_monitor_api_key:
        # Provider/model rotation through the gateway — REAL inference,
        # same pool mechanics as the GCP monitor CLI. Exposed via this
        # route because standalone deployments (EU) have no monitor
        # pool: their once-a-minute cadence is an EventBridge rule
        # whose Input JSON sets rotation_count (and optionally
        # rotation_models to pin a family, e.g. the DSv4 ids).
        benchmark_samples = await rotation_pass(
            settings=settings,
            monitor_region=monitor_region
            or settings.synthetic_monitor_region
            or settings.primary_region,
            api_key=settings.synthetic_monitor_api_key,
            timeout=httpx.Timeout(settings.synthetic_monitor_timeout_seconds),
            count=rotation_count,
            rng=random.Random(),  # noqa: S311 - picks which model to probe, not cryptographic
            models=_rotation_models(body),
        )
        await run_in_threadpool(_record_benchmark_samples, benchmark_samples)
        benchmark_recorded = len(benchmark_samples)
    await run_in_threadpool(_record_probe_samples, samples)
    return {
        "data": {
            "recorded": len(samples),
            "benchmark_recorded": benchmark_recorded,
            "samples": [s.public_dict() for s in samples],
        }
    }


async def run_synthetic_pass(settings: Settings, *, rotation_count: int = 0) -> dict[str, Any]:
    """One synthetic pass, for callers that are not an HTTP request.

    The in-process scheduler (main.py) uses this so a cloud without its own
    scheduler still gets a monitor. Same code path as the route, so the two
    cannot drift into measuring different things.
    """
    return await _run_and_record(
        settings, {"rotation_count": rotation_count} if rotation_count else {}
    )


def register(router: APIRouter) -> None:
    @router.get("/internal/synthetic/health")
    async def synthetic_health(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        return {
            "data": {
                "status": "ok",
                "monitor_region": settings.synthetic_monitor_region or settings.primary_region,
            }
        }

    @router.post("/internal/synthetic/samples")
    async def synthetic_samples(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        body = await json_body(request)
        raw_samples = body.get("samples", [body])
        if not isinstance(raw_samples, list):
            raise api_error(400, "samples must be an array", ErrorType.BAD_REQUEST)
        samples = [_sample_from_body(item) for item in raw_samples]
        # Offload the blocking storage writes so a slow write never stalls the
        # shared event loop; still awaited so `recorded` stays truthful.
        await run_in_threadpool(_record_probe_samples, samples)
        await run_in_threadpool(report_image_generation_failures, samples)
        await run_in_threadpool(report_video_generation_failures, samples)
        return {"data": {"recorded": len(samples)}}

    @router.post("/internal/synthetic/benchmark")
    async def synthetic_benchmark(request: Request, settings: SettingsDep) -> dict[str, Any]:
        # Ingest for provider/model rotation-probe samples. Distinct from
        # /samples (which feeds the /status router-health SLO): these are
        # ProviderBenchmarkSamples that join the same per-provider/model
        # performance store as organic production traffic.
        require_internal_gateway(request, settings)
        body = await json_body(request)
        raw_samples = body.get("samples", [body])
        if not isinstance(raw_samples, list):
            raise api_error(400, "samples must be an array", ErrorType.BAD_REQUEST)
        samples = [_benchmark_from_body(item) for item in raw_samples]
        await run_in_threadpool(_record_benchmark_samples, samples)
        return {"data": {"recorded": len(samples)}}

    @router.post("/internal/synthetic/route-health")
    async def synthetic_route_health(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        flags = await run_in_threadpool(evaluate_route_health, STORE)
        await run_in_threadpool(report_route_health, flags)
        return {"data": {"flagged": [asdict(flag) for flag in flags]}}

    @router.post("/internal/synthetic/run")
    async def synthetic_run(
        request: Request, settings: SettingsDep, response: Response
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        body = await json_body(request)
        # Fire-and-forget mode for schedulers with a short response
        # deadline. EventBridge API destinations give up waiting after
        # ~5s; a full probe pass takes 10-17s (longer with rotation), so
        # EVERY tick was recorded as a FailedInvocation even though the
        # app completed the run and returned 200. The scheduler retries,
        # then EventBridge gives up on the connection entirely — and
        # nothing on the service side reports any of it, because from the
        # app's point of view every request succeeded.
        #
        # With detach=true we acknowledge immediately and run the pass on
        # the event loop. `recorded` is then unknowable at response time,
        # so we do not pretend: the body says scheduled, not recorded.
        if _is_true(body.get("detach")):
            asyncio.create_task(_run_and_record(settings, body))  # noqa: RUF006 - fire-and-forget by design
            response.status_code = 202
            return {"data": {"scheduled": True}}
        return await _run_and_record(settings, body)


def _record_probe_samples(samples: list[SyntheticProbeSample]) -> None:
    for sample in samples:
        STORE.record_synthetic_probe_sample(sample)
        # After recording, so the streak query sees this sample. Swallows its
        # own exceptions — alerting must never break probe ingestion.
        alert_on_failure_streak(STORE, sample)


def _record_benchmark_samples(samples: list[ProviderBenchmarkSample]) -> None:
    for sample in samples:
        STORE.record_provider_benchmark(sample)


def _sample_from_body(body: Any) -> SyntheticProbeSample:
    if not isinstance(body, dict):
        raise api_error(400, "sample must be an object", ErrorType.BAD_REQUEST)
    kwargs: dict[str, Any] = {
        "id": str(body.get("id") or ""),
        "probe_type": str(body.get("probe_type") or ""),
        "target": str(body.get("target") or ""),
        "target_url": str(body.get("target_url") or ""),
        "monitor_region": str(body.get("monitor_region") or ""),
        "target_region": _optional_str(body.get("target_region")),
        "status": str(body.get("status") or ""),
        "latency_milliseconds": _optional_int(body.get("latency_milliseconds")),
        "ttfb_milliseconds": _optional_int(body.get("ttfb_milliseconds")),
        "dns_milliseconds": _optional_int(body.get("dns_milliseconds")),
        "tcp_connect_milliseconds": _optional_int(body.get("tcp_connect_milliseconds")),
        "tls_handshake_milliseconds": _optional_int(body.get("tls_handshake_milliseconds")),
        "gateway_processing_milliseconds": _optional_int(
            body.get("gateway_processing_milliseconds")
        ),
        "connection_reused": body.get("connection_reused")
        if isinstance(body.get("connection_reused"), bool)
        else None,
        "protocol": _optional_str(body.get("protocol")),
        "http_status": _optional_int(body.get("http_status")),
        "error_type": _optional_str(body.get("error_type")),
        "provider": _optional_str(body.get("provider")),
        "model": _optional_str(body.get("model")),
        "selected_provider": _optional_str(body.get("selected_provider")),
        "selected_model": _optional_str(body.get("selected_model")),
        "generation_id": _optional_str(body.get("generation_id")),
        "attestation_digest": _optional_str(body.get("attestation_digest")),
        "source_commit": _optional_str(body.get("source_commit")),
        "cost_microdollars": int(body.get("cost_microdollars") or 0),
        "output_match": body.get("output_match")
        if isinstance(body.get("output_match"), bool)
        else None,
    }
    if body.get("created_at"):
        created_at = str(body["created_at"])
        # Ingest is the boundary: a future-dated sample is poison, not
        # data. One accepted year-7748 row permanently disabled the
        # staleness detector (it defined "latest" forever); storage and
        # status now also guard, but rejecting here keeps the poison out
        # of the store entirely instead of merely filtered on read.
        try:
            created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            raise api_error(
                400, "created_at is not a valid timestamp", ErrorType.BAD_REQUEST
            ) from None
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.UTC)
        skew = (created - dt.datetime.now(dt.UTC)).total_seconds()
        if skew > FUTURE_SAMPLE_SKEW_SECONDS:
            raise api_error(
                400,
                f"created_at is {int(skew)}s in the future (max {FUTURE_SAMPLE_SKEW_SECONDS}s)",
                ErrorType.BAD_REQUEST,
            )
        kwargs["created_at"] = created_at
    for field in ("id", "probe_type", "target", "target_url", "monitor_region", "status"):
        if not kwargs[field]:
            raise api_error(400, f"{field} is required", ErrorType.BAD_REQUEST)
    return SyntheticProbeSample(**kwargs)


def _benchmark_from_body(body: Any) -> ProviderBenchmarkSample:
    if not isinstance(body, dict):
        raise api_error(400, "sample must be an object", ErrorType.BAD_REQUEST)
    kwargs: dict[str, Any] = {
        "id": str(body.get("id") or ""),
        "model": str(body.get("model") or ""),
        "provider": str(body.get("provider") or ""),
        "provider_name": str(body.get("provider_name") or ""),
        "status": str(body.get("status") or ""),
        "usage_type": str(body.get("usage_type") or "Credits"),
        "streamed": bool(body.get("streamed", False)),
        "input_tokens": int(body.get("input_tokens") or 0),
        "output_tokens": int(body.get("output_tokens") or 0),
        "total_cost_microdollars": int(body.get("total_cost_microdollars") or 0),
        "speed_tokens_per_second": _optional_float(body.get("speed_tokens_per_second")),
        "elapsed_milliseconds": _optional_int(body.get("elapsed_milliseconds")),
        "first_token_milliseconds": _optional_int(body.get("first_token_milliseconds")),
        "ttfb_milliseconds": _optional_int(body.get("ttfb_milliseconds")),
        "finish_reason": _optional_str(body.get("finish_reason")),
        "error_type": _optional_str(body.get("error_type")),
        "error_status": _optional_int(body.get("error_status")),
        # Scrub server-side too: the probe already redacts, but the ingest
        # boundary must not trust any internal caller with key-shaped material.
        "error_message": scrub_provider_error_message(
            _optional_str(body.get("error_message")) or ""
        )[:300]
        or None,
        "region": _optional_str(body.get("region")),
        # This ingest is the synthetic rotation path; default provenance is
        # synthetic (the probe also sets it explicitly).
        "source": str(body.get("source") or "synthetic"),
    }
    if body.get("created_at"):
        kwargs["created_at"] = str(body["created_at"])
    for field in ("id", "model", "provider", "provider_name", "status"):
        if not kwargs[field]:
            raise api_error(400, f"{field} is required", ErrorType.BAD_REQUEST)
    return ProviderBenchmarkSample(**kwargs)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


# Ceiling on rotation probes per /run invocation. Each one is a real,
# billed completion; with a 1-minute EventBridge cadence the worst case is
# ROTATION_MAX_PER_RUN real requests per minute. A typo'd or hostile Input
# JSON must not be able to turn the monitor into a spend firehose.
ROTATION_MAX_PER_RUN = 8


def _rotation_count(body: dict[str, Any]) -> int:
    try:
        count = int(body.get("rotation_count") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(count, ROTATION_MAX_PER_RUN))


def _rotation_models(body: dict[str, Any]) -> frozenset[str] | None:
    raw = body.get("rotation_models")
    if not isinstance(raw, list):
        return None
    models = frozenset(str(item) for item in raw if isinstance(item, str) and item)
    return models or None


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
