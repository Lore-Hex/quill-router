from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.auth import InferencePrincipal, ManagementPrincipal, Principal
from trusted_router.errors import api_error
from trusted_router.money import dollars_to_microdollars
from trusted_router.schemas import CreateKeyRequest, PatchKeyRequest, model_to_dict
from trusted_router.serialization import key_shape
from trusted_router.storage import STORE, ApiKey
from trusted_router.types import ErrorType


def register_key_routes(router: APIRouter) -> None:
    @router.get("/key")
    async def key(principal: InferencePrincipal) -> dict[str, Any]:
        assert principal.api_key is not None
        return {"data": key_shape(principal.api_key)}

    @router.get("/keys")
    async def keys(principal: ManagementPrincipal) -> dict[str, list[dict[str, Any]]]:
        return {"data": [key_shape(k) for k in STORE.list_keys(principal.workspace.id)]}

    @router.post("/keys")
    async def create_key(body: CreateKeyRequest, principal: ManagementPrincipal) -> JSONResponse:
        limit_microdollars: int | None = None
        if body.limit is not None:
            limit_microdollars = dollars_to_microdollars(body.limit)
            if limit_microdollars < 0:
                raise api_error(400, "limit must be non-negative", ErrorType.BAD_REQUEST)
        workspace_id = body.workspace_id or principal.workspace.id
        if workspace_id != principal.workspace.id:
            raise api_error(403, "Forbidden", ErrorType.FORBIDDEN)
        raw, k = STORE.create_api_key(
            workspace_id=workspace_id,
            name=body.name,
            creator_user_id=principal.user.id if principal.user else None,
            management=body.management,
            limit_microdollars=limit_microdollars,
            limit_reset=body.limit_reset,
            include_byok_in_limit=body.include_byok_in_limit,
            expires_at=body.expires_at,
        )
        return JSONResponse({"data": key_shape(k), "key": raw}, status_code=201)

    @router.get("/keys/{hash}")
    async def get_key(hash: str, principal: ManagementPrincipal) -> dict[str, Any]:  # noqa: A002
        return {"data": key_shape(_require_key_in_workspace(hash, principal))}

    @router.patch("/keys/{hash}")
    async def patch_key(
        hash: str,  # noqa: A002
        body: PatchKeyRequest,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        _require_key_in_workspace(hash, principal)
        patch = model_to_dict(body)
        if "limit" in patch:
            limit_microdollars = dollars_to_microdollars(patch.pop("limit"))
            if limit_microdollars < 0:
                raise api_error(400, "limit must be non-negative", ErrorType.BAD_REQUEST)
            patch["limit_microdollars"] = limit_microdollars
        updated = STORE.update_key(hash, patch)
        if updated is None:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": key_shape(updated)}

    @router.delete("/keys/{hash}")
    async def delete_key(hash: str, principal: ManagementPrincipal) -> dict[str, Any]:  # noqa: A002
        _require_key_in_workspace(hash, principal)
        if not STORE.delete_key(hash):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "hash": hash}}


def _require_key_in_workspace(key_hash: str, principal: Principal) -> ApiKey:
    key = STORE.get_key_by_hash(key_hash)
    if key is None or key.workspace_id != principal.workspace.id:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return key
