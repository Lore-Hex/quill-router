"""Boot-authenticated, bounded dry-run refresh. Never an authorize dependency."""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import SettingsDep
from trusted_router.gateway_boot import parse_boot_auth_header
from trusted_router.routes.internal._shared import require_internal_gateway


def register(router: APIRouter) -> None:
    @router.get("/internal/speculation/shadow/status")
    def status(request: Request, settings: SettingsDep) -> JSONResponse:
        require_internal_gateway(request, settings)
        if not settings.speculative_provider_shadow_enabled:
            return JSONResponse({"status": "feature-disabled", "enabled": False})
        service = getattr(request.app.state, "speculation_shadow", None)
        state = getattr(request.app.state, "speculation_shadow_status", "not-started")
        if service is not None:
            state = "coverage-lost" if service.dispatcher.coverage_lost() else service.dispatcher.health
            if state == "observing" and service.signer is None:
                state = "shadow-issuer-unavailable"
        return JSONResponse({"status": state, "authority": "shadow-only", "grant_readiness": "evaluated-per-item",
                             "worker_rpcs": service.dispatcher.total_rpcs if service else 0,
                             "refresh_rpcs": service.total_rpcs if service else 0,
                             "queue_depth": service.dispatcher.pending.qsize() if service else 0,
                             "coverage_lost": service.dispatcher.coverage_lost() if service else False,
                             "coverage_loss_reason": service.dispatcher.coverage_loss_reason() if service else ""},
                            status_code=200 if state == "observing" else 503)

    @router.post("/internal/speculation/shadow/refresh")
    async def refresh(request: Request, settings: SettingsDep) -> JSONResponse:
        require_internal_gateway(request, settings)
        if not settings.speculative_provider_shadow_enabled:
            return JSONResponse({"miss": "feature-disabled"}, status_code=503)
        from trusted_router.services.speculation_shadow import MAX_BATCH, MAX_BODY, ShadowMiss
        # Stream with a hard cap before JSON parsing; no unbounded request.body().
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY:
                return JSONResponse({"miss": "batch-too-large"}, status_code=413)
            raw.extend(chunk)
        try:
            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for k, v in pairs:
                    if k in result:
                        raise ValueError("duplicate")
                    result[k] = v
                return result
            envelope = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(envelope, dict) or set(envelope) != {"items"}:
                raise ValueError("fields")
            items = envelope["items"]
            if not isinstance(items, list) or not items or len(items) > MAX_BATCH:
                return JSONResponse({"miss": "batch-too-large"}, status_code=413)
            for item in items:
                if not isinstance(item, dict) or set(item) != {"lookup_digest", "workspace_id", "key_id"}:
                    raise ValueError("fields")
                if not all(isinstance(v, str) and 0 < len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9_.:-]+", v) for v in item.values()):
                    raise ValueError("identity")
                if not re.fullmatch(r"[0-9a-f]{64}", item["lookup_digest"]):
                    raise ValueError("digest")
        except (ValueError, TypeError, RecursionError):
            return JSONResponse({"miss": "batch-invalid"}, status_code=400)
        headers = request.headers.getlist("X-TR-Boot-Auth")
        auth = parse_boot_auth_header(headers[0]) if len(headers) == 1 else None
        if auth is None:
            return JSONResponse({"miss": "boot-auth-invalid"}, status_code=401)
        service = getattr(request.app.state, "speculation_shadow", None)
        if service is None:
            return JSONResponse({"miss": getattr(request.app.state, "speculation_shadow_status", "not-started")}, status_code=503)
        try:
            results = await run_in_threadpool(service.refresh, items, auth, bytes(raw), request.method, request.url.path)
            return JSONResponse({"items": results, "authority": "shadow-only"})
        except ShadowMiss as exc:
            return JSONResponse({"miss": str(exc)}, status_code=401 if str(exc) == "boot-auth-invalid" else 503)
        except Exception:
            return JSONResponse({"miss": "shadow-unavailable"}, status_code=503)
