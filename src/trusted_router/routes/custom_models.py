from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.auth import ManagementPrincipal, Principal
from trusted_router.custom_model_rules import require_custom_model_base_model
from trusted_router.errors import api_error
from trusted_router.schemas import (
    CustomModelCreateRequest,
    CustomModelPatchRequest,
    model_to_dict,
)
from trusted_router.serialization import custom_model_owner_shape
from trusted_router.storage import STORE, CustomModel
from trusted_router.storage_custom_models import (
    CUSTOM_MODEL_LIMIT_PER_USER,
    normalize_custom_model_id,
)
from trusted_router.types import ErrorType


def register_custom_model_routes(router: APIRouter) -> None:
    @router.get("/custom-models")
    async def list_custom_models(principal: ManagementPrincipal) -> dict[str, Any]:
        owner_user_id = _owner_user_id(principal)
        models = STORE.list_custom_models_for_user(owner_user_id)
        return {"data": [custom_model_owner_shape(model) for model in models]}

    @router.post("/custom-models")
    async def create_custom_model(
        body: CustomModelCreateRequest,
        principal: ManagementPrincipal,
    ) -> JSONResponse:
        owner_user_id = _owner_user_id(principal)
        _require_base_model(body.base_model_id)
        try:
            model = STORE.create_custom_model(
                owner_user_id=owner_user_id,
                owner_workspace_id=principal.workspace.id,
                name=body.name,
                base_model_id=body.base_model_id,
                hidden_prompt=body.hidden_prompt,
                enabled=body.enabled,
            )
        except ValueError as exc:
            if str(exc) == "custom_model_limit_exceeded":
                raise api_error(
                    400,
                    f"Custom model limit reached ({CUSTOM_MODEL_LIMIT_PER_USER})",
                    ErrorType.BAD_REQUEST,
                ) from exc
            raise
        return JSONResponse({"data": custom_model_owner_shape(model)}, status_code=201)

    @router.get("/custom-models/{model_id:path}")
    async def get_custom_model(
        model_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        return {"data": custom_model_owner_shape(_require_owner_model(model_id, principal))}

    @router.patch("/custom-models/{model_id:path}")
    async def patch_custom_model(
        model_id: str,
        body: CustomModelPatchRequest,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        existing = _require_owner_model(model_id, principal)
        patch = model_to_dict(body)
        base_model_id = patch.get("base_model_id")
        if base_model_id is not None:
            _require_base_model(str(base_model_id))
        updated = STORE.update_custom_model(
            existing.id,
            owner_user_id=existing.owner_user_id,
            patch=patch,
        )
        if updated is None:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": custom_model_owner_shape(updated)}

    @router.delete("/custom-models/{model_id:path}")
    async def delete_custom_model(
        model_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        existing = _require_owner_model(model_id, principal)
        if not STORE.delete_custom_model(existing.id, owner_user_id=existing.owner_user_id):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "id": existing.id}}


def _owner_user_id(principal: Principal) -> str:
    if principal.user is not None:
        return principal.user.id
    if principal.api_key is not None and principal.api_key.creator_user_id:
        return principal.api_key.creator_user_id
    raise api_error(
        403,
        "A user-owned management session or key is required",
        ErrorType.FORBIDDEN,
    )


def _require_owner_model(model_id: str, principal: Principal) -> CustomModel:
    owner_user_id = _owner_user_id(principal)
    model = STORE.get_custom_model(normalize_custom_model_id(model_id))
    if model is None or model.owner_user_id != owner_user_id:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return model


def _require_base_model(model_id: str) -> None:
    require_custom_model_base_model(model_id)
