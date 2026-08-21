from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Response
from starlette.concurrency import run_in_threadpool

from trusted_router.errors import api_error
from trusted_router.public_user_models import (
    PUBLIC_USER_MODEL_CACHE_CONTROL,
    PublicUserModelReadLimited,
    PublicUserModelUnavailable,
    cached_public_user_model,
    cached_public_user_model_list,
    normalized_public_user_model_id,
)
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def register_user_model_public_routes(router: APIRouter) -> None:
    @router.get("/models/user-provided")
    async def list_public_user_models(
        response: Response,
        kind: Literal["machine", "agent", "human"] | None = Query(default=None),
    ) -> dict[str, Any]:
        try:
            rows = await run_in_threadpool(
                cached_public_user_model_list,
                kind,
                lambda limit: STORE.list_public_user_models(kind=kind, limit=limit),
            )
        except PublicUserModelUnavailable as exc:
            raise api_error(
                503,
                "Public model listing temporarily unavailable",
                ErrorType.SERVICE_UNAVAILABLE,
                headers={"Retry-After": "2", "cache-control": "no-store"},
            ) from exc
        response.headers["cache-control"] = PUBLIC_USER_MODEL_CACHE_CONTROL
        return {"data": rows}

    @router.get("/models/user-provided/{model_id:path}")
    async def get_public_user_model(model_id: str, response: Response) -> dict[str, Any]:
        normalized = normalized_public_user_model_id(model_id)
        response.headers["cache-control"] = PUBLIC_USER_MODEL_CACHE_CONTROL
        if normalized is None:
            raise api_error(
                404,
                "Resource not found",
                ErrorType.NOT_FOUND,
                headers={"cache-control": PUBLIC_USER_MODEL_CACHE_CONTROL},
            )
        try:
            shape = await run_in_threadpool(
                cached_public_user_model,
                normalized,
                lambda: STORE.get_user_model(normalized),
            )
        except PublicUserModelReadLimited as exc:
            raise api_error(
                429,
                "Public model lookup rate exceeded",
                ErrorType.RATE_LIMITED,
                headers={"Retry-After": "30", "cache-control": "no-store"},
            ) from exc
        except PublicUserModelUnavailable as exc:
            raise api_error(
                503,
                "Public model lookup temporarily unavailable",
                ErrorType.SERVICE_UNAVAILABLE,
                headers={"Retry-After": "2", "cache-control": "no-store"},
            ) from exc
        if shape is None:
            raise api_error(
                404,
                "Resource not found",
                ErrorType.NOT_FOUND,
                headers={"cache-control": PUBLIC_USER_MODEL_CACHE_CONTROL},
            )
        return {"data": shape}
