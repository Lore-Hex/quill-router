"""HTTP-error mapping for provider failures.

Lifted out of services/inference so the transport runners can stay focused
on chat/stream flow. The gateway settle path also reuses
`default_provider_secret_ref` so that pointer answers from the same place
that the inline path uses on a local-key fallback.
"""

from __future__ import annotations

from fastapi import HTTPException

from trusted_router.errors import api_error
from trusted_router.providers import ProviderError
from trusted_router.types import ErrorType


def provider_http_error(exc: ProviderError) -> HTTPException:
    status = 502
    type_: str = ErrorType.PROVIDER_ERROR
    if exc.status_code in {401, 403}:
        status = 401
        type_ = ErrorType.PROVIDER_AUTH_ERROR
    elif exc.status_code == 404:
        status = 400
        type_ = ErrorType.MODEL_NOT_SUPPORTED
    elif exc.status_code == 429:
        status = 429
        type_ = ErrorType.PROVIDER_RATE_LIMITED
    elif exc.status_code in {500, 502, 503, 504, 529}:
        status = 502
        type_ = ErrorType.PROVIDER_ERROR
    return api_error(status, f"{exc.provider}: {exc.message}", type_)


def provider_error_type(status_code: int) -> str:
    if status_code in {401, 403}:
        return ErrorType.PROVIDER_AUTH_ERROR
    if status_code == 429:
        return ErrorType.PROVIDER_RATE_LIMITED
    return ErrorType.PROVIDER_ERROR


def default_provider_secret_ref(provider: str) -> str:
    """Default Secret Manager / env reference for a provider's API key.

    Centralized here so both the inline /chat/completions path (when it falls
    back to local key files) and the enclave authorize path return the same
    pointer for a given provider."""
    env_names = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "cerebras": "CEREBRAS_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "kimi": "KIMI_API_KEY",
        "vertex": "VERTEX_ACCESS_TOKEN",
    }
    name = env_names.get(provider, f"{provider.upper()}_API_KEY")
    return f"env://{name}"


def is_rollover_http_error(exc: HTTPException) -> bool:
    if exc.status_code not in {429, 500, 502, 503, 504}:
        return False
    detail = exc.detail
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            return str(error.get("type")) in {
                ErrorType.PROVIDER_ERROR,
                ErrorType.PROVIDER_RATE_LIMITED,
                ErrorType.PROVIDER_AUTH_ERROR,
            }
    return exc.status_code in {429, 502, 503, 504}


def http_error_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or exc.status_code)
    return str(detail)


def all_candidates_failed(errors: list[str]) -> HTTPException:
    suffix = "; ".join(errors[-3:]) if errors else "no candidates were available"
    return api_error(
        502,
        "All route candidates failed: " + suffix,
        ErrorType.PROVIDER_ERROR,
    )
