from __future__ import annotations

from trusted_router.catalog import MODELS, MONITOR_MODEL_ID, Model
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.storage_custom_models import is_custom_model_id
from trusted_router.storage_models import User
from trusted_router.types import ErrorType


def is_allowed_custom_model_base(model: Model) -> bool:
    if not model.supports_chat or model.id == MONITOR_MODEL_ID or is_custom_model_id(model.id):
        return False
    return True


def require_custom_model_base_model(model_id: str) -> None:
    if is_custom_model_id(model_id):
        raise api_error(
            400,
            "Custom models cannot use another custom model as their base model",
            ErrorType.BAD_REQUEST,
        )
    model = MODELS.get(model_id)
    if model is None or not is_allowed_custom_model_base(model):
        raise api_error(
            400,
            "Base model must be a supported TrustedRouter chat or routing model",
            ErrorType.MODEL_NOT_SUPPORTED,
        )


def missing_custom_model_requirements(
    user: User | None,
    settings: Settings,
) -> list[str]:
    if not settings.custom_models_verification_enforced:
        return []
    missing: list[str] = []
    if user is None or not user.email:
        missing.append("email")
    if user is None or not user.phone_verified:
        missing.append("phone_verified")
    if user is None or not user.identity_verified:
        missing.append("identity_verified")
    return missing


def assert_user_can_create_custom_models(user: User | None, settings: Settings) -> None:
    """Gate all four custom-model mutation entry points.

    The callers are API POST and PATCH plus console create and edit. This
    applies to both prompt-wrapper and future user-provided model kinds.
    Delete, list, get, and serving are intentionally not gated.
    """
    missing = missing_custom_model_requirements(user, settings)
    if missing:
        raise api_error(
            403,
            "Verify your account before creating or editing custom models",
            ErrorType.VERIFICATION_REQUIRED,
            extra={
                "missing_requirements": missing,
                "verification_url": "/console/account/verification",
            },
        )
