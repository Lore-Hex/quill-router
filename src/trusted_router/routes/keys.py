from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.auth import InferencePrincipal, ManagementPrincipal, Principal
from trusted_router.errors import api_error, assert_workspace_billing_active
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
        # Quiesce: no new keys while paused, so the key set is stable through a flip.
        assert_workspace_billing_active(principal.workspace)
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
        # Deleting a key drops its typed tr_key_limit row; if a typed hold is still
        # in flight, the settle's release matches 0 rows ("release row-count != 1")
        # and strands the hold. DISABLE FIRST — the gateway rejects authorizes for a
        # disabled key (routes/internal/gateway.py), so once this commits NO new
        # hold can form — then refuse the delete while any hold from just before the
        # disable is still in flight (client retries after it drains, seconds). This
        # closes the common race; the sub-ms residual (an authorize that loaded the
        # enabled key microseconds before the disable) can only STRAND a hold, which
        # the reaper reclaims and the invariant auditor + row-count alert catch. A
        # fully atomic count+delete txn is the complete fix (tracked for whales).
        STORE.update_key(hash, {"disabled": True})
        if STORE.key_has_open_typed_hold(hash):
            raise api_error(
                503, "Key has in-flight requests; retry shortly",
                ErrorType.SERVICE_UNAVAILABLE, headers={"Retry-After": "5"},
            )
        if not STORE.delete_key(hash):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "hash": hash}}


def _require_key_in_workspace(key_hash: str, principal: Principal) -> ApiKey:
    key = STORE.get_key_by_hash(key_hash)
    if key is None or key.workspace_id != principal.workspace.id:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return key
