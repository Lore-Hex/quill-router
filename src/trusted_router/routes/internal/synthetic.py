from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.public_analytics_snapshots import current_public_analytics_snapshot
from trusted_router.routes.helpers import json_body
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.storage import STORE, ProviderBenchmarkSample, SyntheticProbeSample
from trusted_router.storage_models import FUTURE_SAMPLE_SKEW_SECONDS, scrub_provider_error_message
from trusted_router.storage_rate_limits import InMemoryRateLimits
from trusted_router.synthetic.alerts import alert_on_failure_streak
from trusted_router.synthetic.cli import rotation_pass
from trusted_router.synthetic.client_watch import evaluate_client_watch, report_client_watch
from trusted_router.synthetic.components import OPS_PROBE_TYPES, sample_slo_class_ids
from trusted_router.synthetic.fleet import record_heartbeat
from trusted_router.synthetic.probes import (
    client_telemetry_canary_probe,
    gateway_billing_probe,
    gateway_fallback_probe,
    run_synthetic_once,
)
from trusted_router.synthetic.remediator import run_remediator_pass
from trusted_router.synthetic.route_health import (
    evaluate_route_health,
    report_image_generation_failures,
    report_route_health,
    report_video_generation_failures,
)
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)

_MAX_PROBE_SAMPLES_PER_REQUEST = 256
_MAX_BENCHMARK_SAMPLES_PER_REQUEST = 128
_OPERATION_LIMITS_PER_MINUTE = {
    "health": 120,
    "samples": 60,
    "benchmark": 60,
    "route_health": 6,
    "remediate": 2,
    "run": 2,
}
_OPERATION_RATE_LIMITS = InMemoryRateLimits(lock=threading.RLock(), max_buckets=32)
_OPERATION_SLOTS = {
    "health": threading.BoundedSemaphore(8),
    "samples": threading.BoundedSemaphore(2),
    "benchmark": threading.BoundedSemaphore(2),
    "route_health": threading.BoundedSemaphore(1),
    "remediate": threading.BoundedSemaphore(1),
    "run": threading.BoundedSemaphore(1),
}
_BACKGROUND_RUNS: set[asyncio.Task[dict[str, Any]]] = set()
_REMEDIATOR_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="synthetic-remediator",
)


class _HeldOperationSlot:
    """A semaphore admission that can cross an unkillable worker thread.

    Async run tasks release their raw BoundedSemaphore in ``finally``. A
    remediator pass is different: cancelling the executor await cannot stop
    the worker. This lease lets the HTTP request return at its deadline while
    the worker retains admission until its eventual ``finally``. The
    underlying semaphore remains bounded as the guard on every real release.
    """

    def __init__(self, semaphore: threading.BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> bool:
        with self._lock:
            if self._released:
                return False
            self._semaphore.release()
            self._released = True
            return True


def _background_run_done(
    task: asyncio.Task[dict[str, Any]],
    slot: _HeldOperationSlot,
) -> None:
    # A task can be cancelled before its coroutine executes even one line, in
    # which case no coroutine finally block runs. The callback is the last
    # cancellation backstop for the admission.
    slot.release()
    _BACKGROUND_RUNS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None and not isinstance(error, TimeoutError):
        log.error(
            "synthetic.background_run_failed",
            exc_info=(type(error), error, error.__traceback__),
        )


def _admit_operation(name: str) -> threading.BoundedSemaphore:
    slot = _OPERATION_SLOTS[name]
    if not slot.acquire(blocking=False):
        raise api_error(
            429,
            "Synthetic operation is already in progress",
            ErrorType.RATE_LIMITED,
            headers={"Retry-After": "1"},
        )
    hit = _OPERATION_RATE_LIMITS.hit(
        namespace="synthetic_operation",
        subject=name,
        limit=_OPERATION_LIMITS_PER_MINUTE[name],
        window_seconds=60,
    )
    if not hit.allowed:
        slot.release()
        raise api_error(
            429,
            "Synthetic operation rate limit exceeded",
            ErrorType.RATE_LIMITED,
            headers={"Retry-After": str(hit.retry_after_seconds)},
        )
    return slot


async def _run_with_held_slot(
    settings: Settings,
    body: dict[str, Any],
    slot: _HeldOperationSlot,
) -> dict[str, Any]:
    try:
        return await _run_and_record(settings, body)
    finally:
        slot.release()


async def _run_with_deadline(
    settings: Settings,
    body: dict[str, Any],
    slot: _HeldOperationSlot,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            _run_with_held_slot(settings, body, slot),
            timeout=settings.synthetic_run_deadline_seconds,
        )
    except TimeoutError:
        log.error(
            "synthetic.run_deadline_exceeded elapsed_seconds=%.3f pass_args=%r",
            time.monotonic() - started,
            body,
        )
        raise
    finally:
        # Covers cancellation before wait_for has started the child coroutine.
        # The child's own finally remains the normal owner; this is a no-op
        # after that release.
        slot.release()


async def _run_high_authority_with_held_slot(
    settings: Settings,
    body: dict[str, Any],
    slot: _HeldOperationSlot,
) -> dict[str, Any]:
    try:
        return await _run_high_authority_and_record(settings, body)
    finally:
        slot.release()


async def _run_high_authority_with_deadline(
    settings: Settings,
    body: dict[str, Any],
    slot: _HeldOperationSlot,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            _run_high_authority_with_held_slot(settings, body, slot),
            timeout=settings.synthetic_run_deadline_seconds,
        )
    except TimeoutError:
        log.error(
            "synthetic.run_deadline_exceeded elapsed_seconds=%.3f pass_args=%r",
            time.monotonic() - started,
            body,
        )
        raise
    finally:
        slot.release()


async def _run_and_record(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    """Run an observer-triggered pass without any billing-gateway authority."""
    if "control_plane_base_url" in body:
        raise api_error(
            400,
            "control_plane_base_url is deployment configuration, not a request option",
            ErrorType.BAD_REQUEST,
        )
    return await _run_and_record_impl(
        settings,
        body,
        allow_gateway_probes=False,
    )


async def _run_high_authority_and_record(
    settings: Settings,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Run only from the private in-process owner that already holds billing authority."""
    return await _run_and_record_impl(
        settings,
        body,
        allow_gateway_probes=True,
    )


async def _run_and_record_impl(
    settings: Settings,
    body: dict[str, Any],
    *,
    allow_gateway_probes: bool,
) -> dict[str, Any]:
    # AWS's existing EventBridge rule is the single recurring owner for its
    # observer plane. Run remediation beside (not inside every autoscaled web
    # replica's startup loop) when that authenticated scheduler asks for it.
    # The separate remediation semaphore still collapses an operator-triggered
    # pass that happens to overlap this tick.
    remediator_task = (
        asyncio.create_task(_run_scheduled_remediator_pass(settings))
        if _is_true(body.get("run_remediator"))
        else None
    )
    monitor_region = _optional_str(body.get("monitor_region"))
    # The observer credential controls cadence/options, never a destination.
    # This exact HTTPS origin is validated when Settings is constructed. A
    # request-body URL used to make the internal service send its monitor key
    # and billing gateway token to an attacker-chosen host.
    control_plane_base_url = str(
        settings.synthetic_control_plane_base_url or "https://trustedrouter.com"
    )
    try:
        samples = await run_synthetic_once(settings, monitor_region=monitor_region)
        if settings.synthetic_monitor_api_key:
            timeout = httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
            # Keys never follow redirects. The configured value is validated
            # as an exact HTTPS origin, and a 3xx response is a failed canary,
            # not permission to forward credentials to another origin.
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
            ) as client:
                samples.append(
                    await client_telemetry_canary_probe(
                        client,
                        control_plane_base_url=control_plane_base_url,
                        monitor_region=monitor_region
                        or settings.synthetic_monitor_region
                        or settings.primary_region,
                        api_key=settings.synthetic_monitor_api_key,
                    )
                )
                if allow_gateway_probes and settings.internal_gateway_token:
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
            # pool: their EventBridge cadence owns provider rotation.
            benchmark_samples = await rotation_pass(
                settings=settings,
                monitor_region=monitor_region
                or settings.synthetic_monitor_region
                or settings.primary_region,
                api_key=settings.synthetic_monitor_api_key,
                timeout=httpx.Timeout(settings.synthetic_monitor_timeout_seconds),
                count=rotation_count,
                rng=random.Random(),  # noqa: S311 - model selection, not cryptography
                models=_rotation_models(body),
            )
            await run_in_threadpool(_record_benchmark_samples, benchmark_samples)
            benchmark_recorded = len(benchmark_samples)
        await run_in_threadpool(_record_probe_samples, samples)
        try:
            await run_in_threadpool(_client_watch_pass, settings, samples)
        except Exception:
            log.warning("client_watch.pass_failed", exc_info=True)
        result: dict[str, Any] = {
            "data": {
                "recorded": len(samples),
                "benchmark_recorded": benchmark_recorded,
                "samples": [s.public_dict() for s in samples],
            }
        }
    finally:
        remediator_decisions = (
            await remediator_task if remediator_task is not None else None
        )
    if remediator_decisions is not None:
        result["data"]["remediator_decisions"] = remediator_decisions
    return result


def _run_remediator_with_held_slot(
    settings: Settings,
    slot: _HeldOperationSlot,
) -> int:
    try:
        record_heartbeat("scheduler:remediator", settings=settings)
        decisions = run_remediator_pass(settings)
        record_heartbeat("scheduler:remediator", settings=settings)
        return len(decisions)
    finally:
        slot.release()


def _run_remediator_worker(
    settings: Settings,
    slot: _HeldOperationSlot,
    started: threading.Event,
) -> int:
    started.set()
    return _run_remediator_with_held_slot(settings, slot)


async def _run_remediator_with_deadline(
    settings: Settings,
    slot: _HeldOperationSlot,
) -> int:
    started = time.monotonic()
    worker_started = threading.Event()
    loop = asyncio.get_running_loop()
    worker = loop.run_in_executor(
        _REMEDIATOR_EXECUTOR,
        _run_remediator_worker,
        settings,
        slot,
        worker_started,
    )
    try:
        return await asyncio.wait_for(
            asyncio.shield(worker),
            timeout=settings.synthetic_remediator_deadline_seconds,
        )
    except TimeoutError:
        # The worker thread cannot be killed. Abandon the await so this HTTP
        # request releases Cloud Run concurrency immediately, but leave the
        # process-local admission with the live worker until it actually exits.
        # This prevents each scheduler tick from piling another blocked reader
        # onto the same process. If cancellation won before the worker started,
        # nobody else can own the admission, so release it here.
        if not worker_started.is_set():
            slot.release()
        log.error(
            "synthetic.remediator_deadline_exceeded elapsed_seconds=%.3f",
            time.monotonic() - started,
        )
        raise


async def _run_scheduled_remediator_pass(settings: Settings) -> int | None:
    """Run the EventBridge-owned pass without sacrificing its synthetic tick."""
    try:
        slot = _HeldOperationSlot(_admit_operation("remediate"))
    except HTTPException:
        log.warning("scheduled remediator skipped because another pass owns the slot")
        return None
    try:
        # Keep the bounded pass in one worker dispatch. A detached request may
        # be cancelled while TestClient (or the server) tears down its event
        # loop; separate threadpool awaits allowed that cancellation to land
        # after the first heartbeat and before remediation was dispatched.
        return await _run_remediator_with_deadline(settings, slot)
    except Exception:
        # A remediation read/decision failure must remain visible, but it must
        # not discard the independent synthetic results from the same tick.
        log.exception("scheduled remediator pass failed")
        return None


async def run_synthetic_pass(settings: Settings, *, rotation_count: int = 0) -> dict[str, Any]:
    """One synthetic pass, for callers that are not an HTTP request.

    The in-process scheduler (main.py) uses this so a cloud without its own
    scheduler still gets a monitor. Same code path as the route, so the two
    cannot drift into measuring different things.
    """
    semaphore = _OPERATION_SLOTS["run"]
    if not semaphore.acquire(blocking=False):
        log.info("synthetic.pass_skipped_already_running")
        return {"data": {"scheduled": False, "reason": "already_running"}}
    slot = _HeldOperationSlot(semaphore)
    body: dict[str, Any] = {"rotation_count": rotation_count} if rotation_count else {}
    return await _run_high_authority_with_deadline(settings, body, slot)


def register(router: APIRouter) -> None:
    @router.get("/internal/synthetic/health")
    async def synthetic_health(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        slot = _admit_operation("health")
        try:
            return {
                "data": {
                    "status": "ok",
                    "monitor_region": settings.synthetic_monitor_region
                    or settings.primary_region,
                }
            }
        finally:
            slot.release()

    @router.post("/internal/synthetic/samples")
    async def synthetic_samples(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        slot = _admit_operation("samples")
        try:
            body = await json_body(request)
            raw_samples = body.get("samples", [body])
            if not isinstance(raw_samples, list):
                raise api_error(400, "samples must be an array", ErrorType.BAD_REQUEST)
            if len(raw_samples) > _MAX_PROBE_SAMPLES_PER_REQUEST:
                raise api_error(
                    400,
                    f"samples may contain at most {_MAX_PROBE_SAMPLES_PER_REQUEST} items",
                    ErrorType.BAD_REQUEST,
                )
            samples = [_sample_from_body(item) for item in raw_samples]
            # Offload the blocking storage writes so a slow write never stalls the
            # shared event loop; still awaited so `recorded` stays truthful.
            await run_in_threadpool(_record_probe_samples, samples)
            await run_in_threadpool(report_image_generation_failures, samples)
            await run_in_threadpool(report_video_generation_failures, samples)
            # The GCP monitor is a Cloud Run Job (synthetic.cli) that posts its
            # samples here; it never runs _run_and_record. Evaluate the client
            # watch on THIS side, where STORE and the ClickHouse reader live, so
            # the invisible-outage / stale alerts fire on every cloud's pass.
            try:
                await run_in_threadpool(_client_watch_pass, settings, samples)
            except Exception:
                log.warning("client_watch.pass_failed", exc_info=True)
            return {"data": {"recorded": len(samples)}}
        finally:
            slot.release()

    @router.post("/internal/synthetic/benchmark")
    async def synthetic_benchmark(request: Request, settings: SettingsDep) -> dict[str, Any]:
        # Ingest for provider/model rotation-probe samples. Distinct from
        # /samples (which feeds the /status router-health SLO): these are
        # ProviderBenchmarkSamples that join the same per-provider/model
        # performance store as organic production traffic.
        require_internal_gateway(request, settings)
        slot = _admit_operation("benchmark")
        try:
            body = await json_body(request)
            raw_samples = body.get("samples", [body])
            if not isinstance(raw_samples, list):
                raise api_error(400, "samples must be an array", ErrorType.BAD_REQUEST)
            if len(raw_samples) > _MAX_BENCHMARK_SAMPLES_PER_REQUEST:
                raise api_error(
                    400,
                    "samples may contain at most "
                    f"{_MAX_BENCHMARK_SAMPLES_PER_REQUEST} items",
                    ErrorType.BAD_REQUEST,
                )
            samples = [_benchmark_from_body(item) for item in raw_samples]
            await run_in_threadpool(_record_benchmark_samples, samples)
            return {"data": {"recorded": len(samples)}}
        finally:
            slot.release()

    @router.post("/internal/synthetic/route-health")
    async def synthetic_route_health(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        slot = _admit_operation("route_health")
        try:
            flags = await run_in_threadpool(evaluate_route_health, STORE)
            await run_in_threadpool(report_route_health, flags)
            return {"data": {"flagged": [asdict(flag) for flag in flags]}}
        finally:
            slot.release()

    @router.post("/internal/synthetic/remediate")
    async def synthetic_remediate(request: Request, settings: SettingsDep) -> dict[str, Any]:
        """Run one remediation pass under request CPU.

        GCP's scheduled synthetic worker calls this endpoint. Publishing the
        heartbeat before detection gives the pass read-your-write liveness
        even while the durable analytics copy catches up.
        """
        require_internal_gateway(request, settings)
        slot = _HeldOperationSlot(_admit_operation("remediate"))
        decisions = await _run_remediator_with_deadline(settings, slot)
        return {"data": {"decisions": decisions}}

    @router.post("/internal/synthetic/run")
    async def synthetic_run(
        request: Request, settings: SettingsDep, response: Response
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        slot = _HeldOperationSlot(_admit_operation("run"))
        try:
            body = await json_body(request)
        except BaseException:
            slot.release()
            raise
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
            try:
                task = asyncio.create_task(_run_with_deadline(settings, body, slot))
            except Exception:
                slot.release()
                raise
            _BACKGROUND_RUNS.add(task)
            task.add_done_callback(lambda done: _background_run_done(done, slot))
            response.status_code = 202
            return {"data": {"scheduled": True}}
        return await _run_with_deadline(settings, body, slot)


def _record_probe_samples(samples: list[SyntheticProbeSample]) -> None:
    for sample in samples:
        STORE.record_synthetic_probe_sample(sample)
        # After recording, so the streak query sees this sample. Swallows its
        # own exceptions — alerting must never break probe ingestion.
        alert_on_failure_streak(STORE, sample)


def _record_benchmark_samples(samples: list[ProviderBenchmarkSample]) -> None:
    for sample in samples:
        STORE.record_provider_benchmark(sample)


def _client_watch_pass(settings: Settings, samples: list[SyntheticProbeSample]) -> None:
    if not settings.client_events_enabled:
        return
    if not (
        settings.operational_analytics_clickhouse_url
        and settings.operational_analytics_clickhouse_password
    ):
        return
    reader = getattr(STORE, "public_analytics_snapshot", None)
    if not callable(reader):
        return
    snapshot = current_public_analytics_snapshot("client_reliability", reader=reader)
    router_core_samples = [
        sample
        for sample in samples
        if sample.probe_type not in OPS_PROBE_TYPES
        and "router_core" in sample_slo_class_ids(sample)
    ]
    router_core_up = bool(router_core_samples) and all(
        sample.status == "up" for sample in router_core_samples
    )
    alerts = evaluate_client_watch(
        snapshot,
        router_core_up=router_core_up,
        now=dt.datetime.now(dt.UTC),
    )
    report_client_watch(alerts, settings=settings)


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
