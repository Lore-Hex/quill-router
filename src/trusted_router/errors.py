from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

PROVIDER_ERROR_TYPES = frozenset(
    {
        "provider_auth_error",
        "provider_error",
        "provider_rate_limited",
    }
)


def default_error_source(type_: str) -> str:
    return "provider" if str(type_) in PROVIDER_ERROR_TYPES else "router"


def error_body(
    code: int,
    message: str,
    type_: str,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "type": type_,
            "source": source or default_error_source(type_),
        }
    }


def api_error(
    code: int,
    message: str,
    type_: str,
    *,
    source: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail=error_body(code, message, type_, source=source),
        headers=headers,
    )


def assert_workspace_billing_active(workspace: Any) -> None:
    """Quiesce guard for the typed-billing migration (and a general billing kill
    switch): raise 503 + Retry-After if the workspace is billing-paused. Call at
    EVERY entry point that starts new billable work or mints an API key — gateway
    authorize/validate, and every key-creation path (/v1/keys, console keys, OAuth
    code exchange, chat-browser key issuance) — so a paused workspace truly drains
    to zero holds before a flip. Settle is intentionally NOT guarded (it routes by
    reservation origin and must still finalize in-flight work)."""
    if workspace is not None and getattr(workspace, "billing_paused", False):
        from trusted_router.types import ErrorType

        raise api_error(
            503,
            "Workspace billing is paused",
            ErrorType.SERVICE_UNAVAILABLE,
            headers={"Retry-After": "30"},
        )


def error_response(
    code: int,
    message: str,
    type_: str,
    *,
    source: str | None = None,
) -> JSONResponse:
    return JSONResponse(error_body(code, message, type_, source=source), status_code=code)


def not_supported() -> JSONResponse:
    return error_response(501, "Endpoint is not supported by TrustedRouter", "endpoint_not_supported")


def deprecated() -> JSONResponse:
    return error_response(410, "Endpoint is deprecated and not supported", "deprecated")
