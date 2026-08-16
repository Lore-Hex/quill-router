from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from trusted_router.errors import api_error
from trusted_router.serialization import user_model_public_shape
from trusted_router.storage import STORE
from trusted_router.storage_custom_models import normalize_custom_model_id
from trusted_router.types import ErrorType


def register_user_model_public_routes(router: APIRouter) -> None:
    @router.get("/models/user-provided")
    async def list_public_user_models(
        kind: Literal["machine", "agent", "human"] | None = Query(default=None),
    ) -> dict[str, Any]:
        return {
            "data": [
                user_model_public_shape(model)
                for model in STORE.list_public_user_models(kind=kind)
            ]
        }

    @router.get("/models/user-provided/{model_id:path}")
    async def get_public_user_model(model_id: str) -> dict[str, Any]:
        model = STORE.get_user_model(normalize_custom_model_id(model_id))
        if model is None or not model.enabled or model.status != "active":
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": user_model_public_shape(model)}
