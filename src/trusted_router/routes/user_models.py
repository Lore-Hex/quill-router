from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.auth import ManagementPrincipal, Principal, SettingsDep
from trusted_router.custom_model_billing import validate_custom_model_price
from trusted_router.custom_model_rules import assert_user_can_create_custom_models
from trusted_router.errors import api_error
from trusted_router.schemas import UserModelCreateRequest, UserModelPatchRequest
from trusted_router.serialization import user_model_owner_shape
from trusted_router.services.user_model_probe import probe_user_model
from trusted_router.services.user_model_secrets import (
    encrypt_user_model_endpoint_key,
    encrypt_user_model_signing_secret,
)
from trusted_router.storage import STORE
from trusted_router.storage_custom_models import normalize_custom_model_id
from trusted_router.storage_models import UserProvidedModel
from trusted_router.storage_user_models import USER_PROVIDED_MODEL_LIMIT_PER_USER
from trusted_router.types import ErrorType
from trusted_router.user_model_rules import (
    validate_endpoint_url,
    validate_user_model_display_name,
    validate_user_model_slug,
)


def register_user_model_routes(router: APIRouter) -> None:
    @router.get("/user-models")
    async def list_user_models(principal: ManagementPrincipal) -> dict[str, Any]:
        models = STORE.list_user_models_for_user(_owner_user_id(principal))
        return {"data": [user_model_owner_shape(model) for model in models]}

    @router.post("/user-models")
    async def create_user_model(
        body: UserModelCreateRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        owner_user_id = _owner_user_id(principal)
        assert_user_can_create_custom_models(STORE.get_user(owner_user_id), settings)
        slug = validate_user_model_slug(body.slug) if body.slug is not None else None
        display_name = validate_user_model_display_name(body.display_name)
        endpoint_url = validate_endpoint_url(body.endpoint_url, settings)
        _validate_price(
            body.prompt_price_microdollars_per_million_tokens,
            body.completion_price_microdollars_per_million_tokens,
            kind=body.kind,
        )
        signing_secret = secrets.token_urlsafe(32)
        encrypted_signing_secret = encrypt_user_model_signing_secret(
            signing_secret,
            settings,
            workspace_id=principal.workspace.id,
        )
        encrypted_endpoint_api_key = (
            encrypt_user_model_endpoint_key(
                body.endpoint_api_key,
                settings,
                workspace_id=principal.workspace.id,
            )
            if body.endpoint_api_key is not None
            else None
        )
        try:
            model = STORE.create_user_model(
                owner_user_id=owner_user_id,
                owner_workspace_id=principal.workspace.id,
                name=body.name,
                kind=body.kind,
                description=body.description,
                display_identity=body.display_identity,
                display_name=display_name,
                endpoint_url=endpoint_url,
                upstream_model_id=body.upstream_model_id,
                encrypted_endpoint_api_key=encrypted_endpoint_api_key,
                endpoint_key_hint=_secret_hint(body.endpoint_api_key),
                encrypted_signing_secret=encrypted_signing_secret,
                supports_streaming=body.supports_streaming,
                heartbeat_interval_seconds=body.heartbeat_interval_seconds,
                max_concurrency=body.max_concurrency,
                prompt_price_microdollars_per_million_tokens=(
                    body.prompt_price_microdollars_per_million_tokens
                ),
                completion_price_microdollars_per_million_tokens=(
                    body.completion_price_microdollars_per_million_tokens
                ),
                human_verified=body.kind == "human",
                slug=slug,
            )
        except ValueError as exc:
            _raise_store_error(exc)
        data = user_model_owner_shape(model)
        data["signing_secret"] = signing_secret
        return JSONResponse({"data": data}, status_code=201)

    @router.get("/user-models/{model_id:path}")
    async def get_user_model(
        model_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        return {
            "data": user_model_owner_shape(
                _require_owner_user_model(model_id, principal)
            )
        }

    @router.patch("/user-models/{model_id:path}")
    async def patch_user_model(
        model_id: str,
        body: UserModelPatchRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        owner_user_id = _owner_user_id(principal)
        assert_user_can_create_custom_models(STORE.get_user(owner_user_id), settings)
        existing = _require_owner_user_model(model_id, principal)
        patch = body.model_dump(exclude_unset=True)
        if "slug" in patch:
            patch["slug"] = validate_user_model_slug(str(patch["slug"]))
        if "display_name" in patch:
            patch["display_name"] = validate_user_model_display_name(
                str(patch["display_name"])
            )
        if "endpoint_url" in patch:
            patch["endpoint_url"] = validate_endpoint_url(
                str(patch["endpoint_url"]), settings
            )
        kind = str(patch.get("kind", existing.kind))
        prompt_price = int(
            patch.get(
                "prompt_price_microdollars_per_million_tokens",
                existing.prompt_price_microdollars_per_million_tokens,
            )
        )
        completion_price = int(
            patch.get(
                "completion_price_microdollars_per_million_tokens",
                existing.completion_price_microdollars_per_million_tokens,
            )
        )
        _validate_price(prompt_price, completion_price, kind=kind)
        if "kind" in patch:
            patch["human_verified"] = kind == "human"
        if "endpoint_api_key" in body.model_fields_set:
            endpoint_api_key = patch.pop("endpoint_api_key", None)
            patch["encrypted_endpoint_api_key"] = (
                encrypt_user_model_endpoint_key(
                    str(endpoint_api_key),
                    settings,
                    workspace_id=existing.owner_workspace_id,
                )
                if endpoint_api_key is not None
                else None
            )
            patch["endpoint_key_hint"] = _secret_hint(
                None if endpoint_api_key is None else str(endpoint_api_key)
            )
        try:
            updated = STORE.update_user_model(
                existing.id,
                owner_user_id=existing.owner_user_id,
                patch=patch,
            )
        except ValueError as exc:
            _raise_store_error(exc)
        return {"data": user_model_owner_shape(updated)}

    @router.delete("/user-models/{model_id:path}")
    async def delete_user_model(
        model_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        if not STORE.delete_user_model(existing.id, owner_user_id=existing.owner_user_id):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "id": existing.id}}

    @router.post("/user-models/{model_id:path}/clock-in")
    async def clock_in_user_model(
        model_id: str,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        result = await probe_user_model(existing, settings)
        if not result.ok:
            STORE.set_user_model_online(
                existing.id,
                owner_user_id=existing.owner_user_id,
                online=False,
            )
            raise api_error(409, result.detail, ErrorType.CONFLICT)
        updated = STORE.set_user_model_online(
            existing.id,
            owner_user_id=existing.owner_user_id,
            online=True,
        )
        return {"data": user_model_owner_shape(updated)}

    @router.post("/user-models/{model_id:path}/clock-out")
    async def clock_out_user_model(
        model_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        updated = STORE.set_user_model_online(
            existing.id,
            owner_user_id=existing.owner_user_id,
            online=False,
        )
        return {"data": user_model_owner_shape(updated)}

    @router.post("/user-models/{model_id:path}/heartbeat")
    async def heartbeat_user_model(
        model_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        interval = existing.heartbeat_interval_seconds
        if interval is None:
            raise api_error(
                400,
                "No heartbeat interval is configured for this model",
                ErrorType.BAD_REQUEST,
            )
        expires_at = datetime.now(UTC) + timedelta(seconds=2 * interval)
        updated = STORE.record_user_model_heartbeat(
            existing.id,
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        )
        return {"data": user_model_owner_shape(updated)}

    @router.post("/user-models/{model_id:path}/probe")
    async def probe_user_model_route(
        model_id: str,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        result = await probe_user_model(existing, settings)
        updated = _require_owner_user_model(model_id, principal)
        return {
            "data": {
                **user_model_owner_shape(updated),
                "probe_detail": result.detail,
            }
        }

    @router.post("/user-models/{model_id:path}/rotate-secrets")
    async def rotate_user_model_secrets(
        model_id: str,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        signing_secret = secrets.token_urlsafe(32)
        encrypted = encrypt_user_model_signing_secret(
            signing_secret,
            settings,
            workspace_id=existing.owner_workspace_id,
        )
        updated = STORE.update_user_model(
            existing.id,
            owner_user_id=existing.owner_user_id,
            patch={"encrypted_signing_secret": encrypted},
        )
        return {
            "data": {
                **user_model_owner_shape(updated),
                "signing_secret": signing_secret,
            }
        }


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


def _require_owner_user_model(
    model_id: str,
    principal: Principal,
) -> UserProvidedModel:
    owner_user_id = _owner_user_id(principal)
    model = STORE.get_user_model(normalize_custom_model_id(model_id))
    if model is None or model.owner_user_id != owner_user_id:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return model


def _validate_price(prompt_price: int, completion_price: int, *, kind: str) -> None:
    try:
        validate_custom_model_price(prompt_price, completion_price, kind=kind)
    except ValueError as exc:
        raise api_error(
            400,
            "Custom model price is outside the allowed range for this kind",
            ErrorType.BAD_REQUEST,
        ) from exc


def _raise_store_error(exc: ValueError) -> NoReturn:
    error = str(exc)
    if error == "custom_model_limit_exceeded":
        raise api_error(
            400,
            f"User-provided model limit reached ({USER_PROVIDED_MODEL_LIMIT_PER_USER})",
            ErrorType.BAD_REQUEST,
        ) from exc
    if error == "invalid_custom_model_slug":
        raise api_error(
            400,
            "Model slug must be 3-64 lowercase letters, numbers, or hyphens",
            ErrorType.BAD_REQUEST,
        ) from exc
    if error == "custom_model_slug_taken":
        raise api_error(409, "Model slug is already in use", ErrorType.CONFLICT) from exc
    if error == "custom_model_not_found":
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND) from exc
    raise exc


def _secret_hint(secret: str | None) -> str | None:
    if secret is None:
        return None
    stripped = secret.strip()
    if not stripped:
        return None
    return f"...{stripped[-4:]}"
