from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from trusted_router.auth import ManagementPrincipal, Principal
from trusted_router.errors import api_error
from trusted_router.routes.helpers import json_body
from trusted_router.serialization import member_shape, workspace_shape
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def register_workspace_routes(router: APIRouter) -> None:
    @router.get("/workspaces")
    async def workspaces(principal: ManagementPrincipal) -> dict[str, list[dict[str, Any]]]:
        if principal.user:
            items = STORE.list_workspaces_for_user(principal.user.id)
        else:
            items = [principal.workspace]
        return {"data": [workspace_shape(w) for w in items]}

    @router.post("/workspaces")
    async def create_workspace(request: Request, principal: ManagementPrincipal) -> JSONResponse:
        if principal.user is None:
            raise api_error(403, "User auth is required to create workspaces", ErrorType.FORBIDDEN)
        body = await json_body(request)
        workspace = STORE.create_workspace(principal.user.id, str(body.get("name") or "Workspace"))
        return JSONResponse({"data": workspace_shape(workspace)}, status_code=201)

    @router.get("/workspaces/{id}")
    async def get_workspace(id: str, principal: ManagementPrincipal) -> dict[str, Any]:  # noqa: A002
        workspace = _require_managed_workspace(id, principal)
        return {"data": workspace_shape(workspace)}

    @router.patch("/workspaces/{id}")
    async def patch_workspace(
        id: str,  # noqa: A002
        request: Request,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        workspace = _require_managed_workspace(id, principal)
        body = await json_body(request)
        if body.get("content_storage_enabled"):
            raise api_error(
                400,
                "Prompt/output content storage is disabled",
                ErrorType.CONTENT_STORAGE_DISABLED,
            )
        if "name" in body:
            updated = STORE.update_workspace(workspace.id, name=str(body["name"]))
            if updated is None:
                raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
            workspace = updated
        return {"data": workspace_shape(workspace)}

    @router.delete("/workspaces/{id}")
    async def delete_workspace(id: str, principal: ManagementPrincipal) -> dict[str, Any]:  # noqa: A002
        workspace = _require_managed_workspace(id, principal)
        STORE.update_workspace(workspace.id, deleted=True)
        return {"data": {"deleted": True, "id": id}}

    @router.post("/workspaces/{id}/members/add")
    async def add_workspace_members(
        id: str,  # noqa: A002
        request: Request,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        _require_managed_workspace(id, principal)
        body = await json_body(request)
        emails = body.get("emails") or body.get("members") or []
        if not isinstance(emails, list):
            raise api_error(400, "emails must be a list", ErrorType.BAD_REQUEST)
        members = STORE.add_members(id, [str(e) for e in emails], role=str(body.get("role") or "member"))
        return {"data": [member_shape(m) for m in members]}

    @router.post("/workspaces/{id}/members/remove")
    async def remove_workspace_members(
        id: str,  # noqa: A002
        request: Request,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        _require_managed_workspace(id, principal)
        body = await json_body(request)
        user_ids = body.get("user_ids") or body.get("members") or []
        if not isinstance(user_ids, list):
            raise api_error(400, "user_ids must be a list", ErrorType.BAD_REQUEST)
        STORE.remove_members(id, [str(uid) for uid in user_ids])
        return {"data": {"removed": len(user_ids)}}

    @router.get("/organization/members")
    async def organization_members(principal: ManagementPrincipal) -> dict[str, list[dict[str, Any]]]:
        return {"data": [member_shape(m) for m in STORE.list_members(principal.workspace.id)]}


def _require_managed_workspace(workspace_id: str, principal: Principal) -> Any:
    workspace = STORE.get_workspace(workspace_id)
    if workspace is None:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    if not _principal_can_manage_workspace(principal, workspace_id):
        raise api_error(403, "Forbidden", ErrorType.FORBIDDEN)
    return workspace


def _principal_can_manage_workspace(principal: Principal, workspace_id: str) -> bool:
    if principal.user is None:
        return principal.workspace.id == workspace_id
    return STORE.user_can_manage(principal.user.id, workspace_id)
