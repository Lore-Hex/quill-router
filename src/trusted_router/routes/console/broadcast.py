"""/console/broadcast — workspace Broadcast destinations.

Broadcast exports generation metadata to external observability systems.
Prompt/output content stays off by default and is only exported by the
attested gateway when a destination explicitly opts in.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.byok_crypto import encrypt_control_secret
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.services.broadcast import (
    broadcast_secret_context,
    public_destination_shape,
)
from trusted_router.services.broadcast_adapters import POSTHOG_DEFAULT_ENDPOINT, adapter_for
from trusted_router.storage import STORE, BroadcastDestination


def register(app: FastAPI) -> None:
    @app.get("/console/broadcast")
    async def console_broadcast(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        destinations = [
            public_destination_shape(destination)
            for destination in STORE.list_broadcast_destinations(ctx.workspace.id)
        ]
        return HTMLResponse(render(
            "console/broadcast.html",
            settings=settings,
            user=ctx.user,
            active="broadcast",
            page_title="Broadcast",
            page_subtitle="Export generation metadata to PostHog or an OTLP webhook.",
            destinations=destinations,
            default_posthog_endpoint=POSTHOG_DEFAULT_ENDPOINT,
            api_base_url=settings.api_base_url,
        ))

    @app.post("/console/broadcast")
    async def console_create_broadcast(
        ctx: ConsoleDep,
        settings: SettingsDep,
        name: str = Form(..., min_length=1, max_length=120),
        destination_type: str = Form(..., min_length=1, max_length=32),
        endpoint: str = Form("", max_length=512),
        method: str = Form("POST", max_length=8),
        api_key: str = Form("", max_length=512),
        headers_json: str = Form("", max_length=4096),
        enabled: str | None = Form(None),
        include_content: str | None = Form(None),
    ) -> Response:
        destination_type = destination_type.strip().lower()
        adapter = adapter_for(destination_type)
        if adapter is None:
            return RedirectResponse(url="/console/broadcast?error=type", status_code=303)
        clean_endpoint = adapter.normalize_endpoint(endpoint)
        if adapter.validate_endpoint(clean_endpoint) is not None:
            return RedirectResponse(url="/console/broadcast?error=endpoint", status_code=303)
        clean_method = method.strip().upper() or "POST"
        if clean_method not in {"POST", "PUT"}:
            return RedirectResponse(url="/console/broadcast?error=method", status_code=303)
        headers = _parse_headers(headers_json)
        if headers is None:
            return RedirectResponse(url="/console/broadcast?error=headers", status_code=303)
        if adapter.requires_api_key and not api_key.strip():
            return RedirectResponse(url="/console/broadcast?error=api_key", status_code=303)
        destination = STORE.create_broadcast_destination(
            workspace_id=ctx.workspace.id,
            type=destination_type,
            name=name.strip(),
            endpoint=clean_endpoint,
            method=clean_method,
            enabled=enabled == "on",
            include_content=include_content == "on",
        )
        STORE.update_broadcast_destination(
            ctx.workspace.id,
            destination.id,
            **_secret_patch(destination, settings=settings, api_key=api_key.strip(), headers=headers),
        )
        return RedirectResponse(url="/console/broadcast", status_code=303)

    @app.post("/console/broadcast/{destination_id}/delete")
    async def console_delete_broadcast(ctx: ConsoleDep, destination_id: str) -> Response:
        STORE.delete_broadcast_destination(ctx.workspace.id, destination_id)
        return RedirectResponse(url="/console/broadcast", status_code=303)


def _parse_headers(raw: str) -> dict[str, str] | None:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): str(value) for key, value in parsed.items() if str(key).strip()}


def _secret_patch(
    destination: BroadcastDestination,
    *,
    settings: Any,
    api_key: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if api_key:
        patch["encrypted_api_key"] = encrypt_control_secret(
            api_key,
            settings,
            workspace_id=destination.workspace_id,
            purpose=broadcast_secret_context(destination.id, "api_key"),
        )
        patch["replace_api_key"] = True
    patch["encrypted_headers"] = (
        encrypt_control_secret(
            json.dumps(headers, separators=(",", ":"), sort_keys=True),
            settings,
            workspace_id=destination.workspace_id,
            purpose=broadcast_secret_context(destination.id, "headers"),
        )
        if headers
        else None
    )
    patch["header_names"] = sorted(headers)
    patch["replace_headers"] = True
    return patch
