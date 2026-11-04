"""Fail closed on retired creator namespaces, including pre-migration IDs."""

from trusted_router.errors import api_error
from trusted_router.types import ErrorType

RETIRED_MODEL_PREFIXES = (
    "tr-custom-model/",
    "tr-user-model/",
    "trustedrouter/user-",
    "nyte/user-",
)


def reject_retired_models(*model_ids: object) -> None:
    if any(
        isinstance(model_id, str)
        and model_id.strip().lower().startswith(RETIRED_MODEL_PREFIXES)
        for model_id in model_ids
    ):
        raise api_error(
            410,
            "Custom models and user-provided models have been retired. Choose a catalog model.",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
