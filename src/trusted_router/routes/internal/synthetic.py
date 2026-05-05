from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.routes.helpers import json_body
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.storage import STORE, SyntheticProbeSample
from trusted_router.synthetic.probes import (
    gateway_billing_probe,
    gateway_fallback_probe,
    run_synthetic_once,
)
from trusted_router.types import ErrorType


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
        for sample in samples:
            STORE.record_synthetic_probe_sample(sample)
        return {"data": {"recorded": len(samples)}}

    @router.post("/internal/synthetic/run")
    async def synthetic_run(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        body = await json_body(request)
        monitor_region = _optional_str(body.get("monitor_region"))
        samples = await run_synthetic_once(settings, monitor_region=monitor_region)
        if settings.synthetic_monitor_api_key and settings.internal_gateway_token:
            timeout = httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout) as client:
                samples.extend(
                    await gateway_billing_probe(
                        client,
                        control_plane_base_url=str(
                            body.get("control_plane_base_url") or "https://trustedrouter.com"
                        ),
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
                        control_plane_base_url=str(
                            body.get("control_plane_base_url") or "https://trustedrouter.com"
                        ),
                        monitor_region=monitor_region
                        or settings.synthetic_monitor_region
                        or settings.primary_region,
                        api_key=settings.synthetic_monitor_api_key,
                        internal_token=settings.internal_gateway_token,
                        model=settings.synthetic_monitor_model,
                    )
                )
        for sample in samples:
            STORE.record_synthetic_probe_sample(sample)
        return {"data": {"recorded": len(samples), "samples": [s.public_dict() for s in samples]}}


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
        kwargs["created_at"] = str(body["created_at"])
    for field in ("id", "probe_type", "target", "target_url", "monitor_region", "status"):
        if not kwargs[field]:
            raise api_error(400, f"{field} is required", ErrorType.BAD_REQUEST)
    return SyntheticProbeSample(**kwargs)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
