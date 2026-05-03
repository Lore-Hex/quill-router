from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.auth import ManagementPrincipal
from trusted_router.errors import api_error, error_response
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def register_activity_routes(router: APIRouter) -> None:
    @router.get("/activity")
    async def activity(
        principal: ManagementPrincipal,
        date: str | None = None,
        api_key_hash: str | None = None,
        group_by: str | None = None,
        limit: int = 100,
    ) -> dict[str, list[dict[str, Any]]]:
        if group_by in {"none", "request", "generation"}:
            normalized_limit = max(1, min(limit, 1000))
            return {
                "data": STORE.activity_events(
                    principal.workspace.id,
                    api_key_hash=api_key_hash,
                    date=date,
                    limit=normalized_limit,
                )
            }
        return {"data": STORE.activity(principal.workspace.id, api_key_hash=api_key_hash, date=date)}

    @router.get("/generation")
    async def generation(id: str, principal: ManagementPrincipal) -> dict[str, Any]:  # noqa: A002
        gen = STORE.get_generation(id)
        if gen is None or gen.workspace_id != principal.workspace.id:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": gen.to_openrouter_generation()}

    @router.get("/generation/content")
    async def generation_content(id: str) -> JSONResponse:  # noqa: A002
        _ = id
        return error_response(
            404,
            "TrustedRouter does not store prompt or output content",
            ErrorType.CONTENT_NOT_STORED,
        )
