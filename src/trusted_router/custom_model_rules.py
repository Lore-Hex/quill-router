from __future__ import annotations

from trusted_router.catalog import MODELS, MONITOR_MODEL_ID, Model
from trusted_router.errors import api_error
from trusted_router.storage_custom_models import is_custom_model_id
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
