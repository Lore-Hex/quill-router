from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import AuthenticatedPrincipal, ManagementPrincipal
from trusted_router.errors import api_error, error_response
from trusted_router.request_tags import InvalidTags, validate_tags
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
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> dict[str, Any]:
        if tag_value is not None and tag_key is None:
            raise api_error(400, "tag_value requires tag_key", ErrorType.INVALID_TAGS)
        try:
            if tag_key is not None:
                validate_tags({tag_key: tag_value or ""})
            group_by_tag = (
                group_by.removeprefix("tag:")
                if group_by and group_by.startswith("tag:")
                else None
            )
            if group_by_tag is not None:
                validate_tags({group_by_tag: ""})
        except InvalidTags as exc:
            raise api_error(400, str(exc), ErrorType.INVALID_TAGS) from exc
        if group_by in {"none", "request", "generation"}:
            normalized_limit = max(1, min(limit, 1000))
            result = await run_in_threadpool(
                STORE.activity_events_result,
                principal.workspace.id,
                api_key_hash=api_key_hash,
                date=date,
                limit=normalized_limit,
                tag_key=tag_key,
                tag_value=tag_value,
            )
            return {
                "data": result.data,
                "meta": _activity_meta(result, tag_filter=tag_key is not None),
            }
        result = await run_in_threadpool(
            STORE.activity_result,
            principal.workspace.id,
            api_key_hash=api_key_hash,
            date=date,
            tag_key=tag_key,
            tag_value=tag_value,
            group_by_tag=group_by_tag,
        )
        return {
            "data": result.data,
            "meta": _activity_meta(result, tag_filter=tag_key is not None),
        }

    @router.get("/generation")
    async def generation(id: str, principal: AuthenticatedPrincipal) -> dict[str, Any]:  # noqa: A002
        # Either an inference key or a management session works here.
        # OpenRouter accepts the same bearer token the caller used for
        # the chat completion that produced this generation_id, so
        # forty.news (and any OpenRouter-shaped client) can issue the
        # follow-up GET without juggling key types. Workspace scoping
        # below stops cross-workspace lookups.
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


def _activity_meta(result: Any, *, tag_filter: bool) -> dict[str, Any]:
    return {
        "truncated": bool(result.truncated),
        "groups_truncated": bool(result.groups_truncated),
        "scanned": int(result.scanned),
        "scan_limit": result.scan_limit,
        "tag_filter_scope": "recent_window" if tag_filter else None,
    }
