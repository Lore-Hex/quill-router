from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.byok_crypto import encrypt_control_secret
from trusted_router.errors import api_error
from trusted_router.schemas import (
    BroadcastDestinationCreateRequest,
    BroadcastDestinationPatchRequest,
)
from trusted_router.services.broadcast import (
    POSTHOG_DEFAULT_ENDPOINT,
    broadcast_secret_context,
    public_destination_shape,
    test_destination,
)
from trusted_router.storage import STORE, BroadcastDestination
from trusted_router.types import ErrorType


def register_broadcast_routes(router: APIRouter) -> None:
    @router.get("/broadcast/destinations")
    async def list_destinations(principal: ManagementPrincipal) -> dict[str, Any]:
        return {
            "data": [
                public_destination_shape(destination)
                for destination in STORE.list_broadcast_destinations(principal.workspace.id)
            ]
        }

    @router.post("/broadcast/destinations")
    async def create_destination(
        body: BroadcastDestinationCreateRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        endpoint = _endpoint_for(body.type, body.endpoint)
        _validate_destination(body.type, endpoint)
        destination = STORE.create_broadcast_destination(
            workspace_id=principal.workspace.id,
            type=body.type,
            name=body.name,
            endpoint=endpoint,
            enabled=body.enabled,
            include_content=body.include_content,
            method=body.method,
        )
        destination = _apply_secret_patch(
            destination,
            settings=settings,
            api_key=body.api_key,
            headers=body.headers,
            require_posthog_key=body.type == "posthog",
        )
        return JSONResponse({"data": public_destination_shape(destination)}, status_code=201)

    @router.get("/broadcast/destinations/{destination_id}")
    async def get_destination(
        destination_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        return {"data": public_destination_shape(_require_destination(principal.workspace.id, destination_id))}

    @router.patch("/broadcast/destinations/{destination_id}")
    async def patch_destination(
        destination_id: str,
        body: BroadcastDestinationPatchRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        destination = _require_destination(principal.workspace.id, destination_id)
        endpoint = _endpoint_for(destination.type, body.endpoint) if body.endpoint is not None else None
        if endpoint is not None:
            _validate_destination(destination.type, endpoint)
        patch = body.model_dump(exclude_unset=True, exclude_none=True)
        patch.pop("api_key", None)
        patch.pop("headers", None)
        if endpoint is not None:
            patch["endpoint"] = endpoint
        updated = STORE.update_broadcast_destination(principal.workspace.id, destination_id, **patch)
        if updated is None:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        destination = updated
        if body.api_key is not None or body.headers is not None:
            destination = _apply_secret_patch(
                destination,
                settings=settings,
                api_key=body.api_key,
                headers=body.headers,
                require_posthog_key=False,
            )
        return {"data": public_destination_shape(destination)}

    @router.delete("/broadcast/destinations/{destination_id}")
    async def delete_destination(
        destination_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        if not STORE.delete_broadcast_destination(principal.workspace.id, destination_id):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "id": destination_id}}

    @router.post("/broadcast/destinations/{destination_id}/test")
    async def test_broadcast_destination(
        destination_id: str,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        destination = _require_destination(principal.workspace.id, destination_id)
        ok, message = await test_destination(destination, settings)
        return JSONResponse(
            {"data": {"ok": ok, "message": message}},
            status_code=200 if ok else 400,
        )


def _require_destination(workspace_id: str, destination_id: str) -> BroadcastDestination:
    destination = STORE.get_broadcast_destination(workspace_id, destination_id)
    if destination is None:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return destination


def _apply_secret_patch(
    destination: BroadcastDestination,
    *,
    settings: Any,
    api_key: str | None,
    headers: dict[str, str] | None,
    require_posthog_key: bool,
) -> BroadcastDestination:
    patch: dict[str, Any] = {}
    if api_key is not None:
        if not api_key.strip():
            raise api_error(400, "api_key is empty", ErrorType.BAD_REQUEST)
        patch["encrypted_api_key"] = encrypt_control_secret(
            api_key,
            settings,
            workspace_id=destination.workspace_id,
            purpose=broadcast_secret_context(destination.id, "api_key"),
        )
        patch["replace_api_key"] = True
    elif require_posthog_key:
        raise api_error(400, "api_key is required for PostHog", ErrorType.BAD_REQUEST)
    if headers is not None:
        clean_headers = {str(key): str(value) for key, value in headers.items() if str(key).strip()}
        patch["encrypted_headers"] = (
            encrypt_control_secret(
                json.dumps(clean_headers, separators=(",", ":"), sort_keys=True),
                settings,
                workspace_id=destination.workspace_id,
                purpose=broadcast_secret_context(destination.id, "headers"),
            )
            if clean_headers
            else None
        )
        patch["header_names"] = sorted(clean_headers)
        patch["replace_headers"] = True
    if not patch:
        return destination
    updated = STORE.update_broadcast_destination(destination.workspace_id, destination.id, **patch)
    if updated is None:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return updated


def _endpoint_for(destination_type: str, endpoint: str | None) -> str:
    if destination_type == "posthog":
        return (endpoint or POSTHOG_DEFAULT_ENDPOINT).rstrip("/")
    return (endpoint or "").strip()


def _validate_destination(destination_type: str, endpoint: str) -> None:
    if destination_type not in {"posthog", "webhook"}:
        raise api_error(400, "unsupported broadcast destination type", ErrorType.BAD_REQUEST)
    if not endpoint.startswith(("https://", "http://")):
        raise api_error(400, "endpoint must be an HTTP URL", ErrorType.BAD_REQUEST)
    if destination_type == "webhook" and not endpoint:
        raise api_error(400, "endpoint is required", ErrorType.BAD_REQUEST)
