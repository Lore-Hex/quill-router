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
) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail=error_body(code, message, type_, source=source),
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
