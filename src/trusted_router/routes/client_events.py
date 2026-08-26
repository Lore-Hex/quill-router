"""Fire-and-forget ingest for content-free client reliability beacons."""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import get_authorization_bearer, require_inference_key
from trusted_router.client_events_schema import ClientEventsBatch
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.routes.helpers import enforce_rate_limit, read_json_body_bounded
from trusted_router.storage import STORE
from trusted_router.storage_operational_analytics import build_client_events_payload
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)

_SEMAPHORE_STATE_LOCK = threading.Lock()


def _accepted_response(
    settings: Settings,
    *,
    accepted_events: int,
    accepted_counters: int,
    dropped: int,
    pause_seconds: int,
    telemetry_off: bool = False,
) -> JSONResponse:
    response = JSONResponse(
        status_code=202,
        content={
            "data": {
                "accepted_events": accepted_events,
                "accepted_counters": accepted_counters,
                "dropped": dropped,
            },
            "policy": {
                "success_sample_rate": settings.client_events_success_sample_rate,
                "flush_seconds": settings.client_events_flush_seconds,
                "pause_seconds": pause_seconds,
            },
        },
    )
    if telemetry_off:
        response.headers["x-tr-telemetry"] = "off"
    return response


def _write_semaphore(request: Request, settings: Settings) -> threading.BoundedSemaphore:
    semaphore = getattr(request.app.state, "client_events_write_semaphore", None)
    if semaphore is not None:
        return semaphore
    with _SEMAPHORE_STATE_LOCK:
        semaphore = getattr(request.app.state, "client_events_write_semaphore", None)
        if semaphore is None:
            # Floor at 1: the kill switch is client_events_pause_seconds, not a
            # zero-width semaphore that would 503 every batch forever.
            semaphore = threading.BoundedSemaphore(
                max(1, int(settings.client_events_write_concurrency))
            )
            request.app.state.client_events_write_semaphore = semaphore
    return semaphore


def _server_identifies_synthetic(request: Request, settings: Settings, workspace_id: str) -> bool:
    if workspace_id in settings.client_events_synthetic_workspace_ids:
        return True
    monitor_key = settings.synthetic_monitor_api_key
    bearer = get_authorization_bearer(request)
    return bool(monitor_key and bearer and secrets.compare_digest(bearer, monitor_key))


def _enforce_client_events_limits(
    settings: Settings,
    *,
    key_hash: str,
    workspace_id: str,
) -> None:
    limits = (
        ("client-events-key", key_hash, settings.client_events_key_per_minute),
        (
            "client-events-workspace",
            workspace_id,
            settings.client_events_workspace_per_minute,
        ),
    )
    for namespace, subject, limit in limits:
        hit = enforce_rate_limit(
            namespace,
            subject,
            limit,
            window_seconds=60,
        )
        if hit is not None and not hit.allowed:
            raise api_error(
                429,
                "Client events rate limit exceeded",
                ErrorType.RATE_LIMITED,
                headers={"Retry-After": str(hit.retry_after_seconds)},
            )


def register_client_events_routes(router: APIRouter) -> None:
    @router.post("/client-events")
    async def client_events(request: Request) -> JSONResponse:
        settings: Settings = request.app.state.settings
        if not settings.client_events_enabled:
            return _accepted_response(
                settings,
                accepted_events=0,
                accepted_counters=0,
                dropped=0,
                pause_seconds=86_400,
                telemetry_off=True,
            )
        if settings.client_events_pause_seconds > 0:
            return _accepted_response(
                settings,
                accepted_events=0,
                accepted_counters=0,
                dropped=0,
                pause_seconds=settings.client_events_pause_seconds,
                telemetry_off=True,
            )

        # Per docs/design/oauth-scopes-and-app-economy.md ("Scopes: small,
        # real, enforced"), reliability beacons deliberately ride the same
        # inference credential as the requests they describe.
        principal = require_inference_key(request, settings)
        api_key = principal.api_key
        if api_key is None:  # pragma: no cover - guaranteed by require_inference_key.
            raise api_error(401, "An inference API key is required", ErrorType.UNAUTHORIZED)

        raw_body = await read_json_body_bounded(
            request,
            settings.client_events_max_body_bytes,
        )
        try:
            batch = ClientEventsBatch.model_validate_json(raw_body)
        except ValidationError as exc:
            raise api_error(
                400,
                "Invalid client events batch",
                ErrorType.BAD_REQUEST,
            ) from exc

        _enforce_client_events_limits(
            settings,
            key_hash=api_key.hash,
            workspace_id=principal.workspace.id,
        )
        payload = build_client_events_payload(
            batch,
            tenant_id=principal.workspace.id,
            key_id=api_key.hash,
            received_at=dt.datetime.now(dt.UTC),
            is_synthetic=_server_identifies_synthetic(
                request,
                settings,
                principal.workspace.id,
            ),
            success_sample_rate=settings.client_events_success_sample_rate,
        )

        semaphore = _write_semaphore(request, settings)
        if not semaphore.acquire(blocking=False):
            raise api_error(
                503,
                "Client events storage is busy",
                ErrorType.SERVICE_UNAVAILABLE,
                headers={"Retry-After": "60"},
            )
        try:
            await run_in_threadpool(STORE.record_client_events_batch, payload)
        except Exception as exc:
            log.exception(
                "client_events.store_unavailable",
                extra={
                    "tenant": str(payload["tenant_id"])[:12],
                    "error_class": type(exc).__name__,
                },
            )
            raise api_error(
                503,
                "Client events storage is unavailable",
                ErrorType.SERVICE_UNAVAILABLE,
                headers={"Retry-After": "60"},
            ) from exc
        finally:
            semaphore.release()

        log.info(
            "client_events.accepted tenant=%s events=%d counters=%d dropped=%d sdk=%s synthetic=%s",
            str(payload["tenant_id"])[:12],
            len(batch.events),
            len(batch.counters),
            batch.dropped_since_last,
            batch.sdk.name,
            payload["synthetic"],
        )
        return _accepted_response(
            settings,
            accepted_events=len(batch.events),
            accepted_counters=len(batch.counters),
            dropped=batch.dropped_since_last,
            pause_seconds=0,
        )
