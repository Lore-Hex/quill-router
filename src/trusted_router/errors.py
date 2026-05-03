from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def error_body(code: int, message: str, type_: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "type": type_}}


def api_error(code: int, message: str, type_: str) -> HTTPException:
    return HTTPException(status_code=code, detail=error_body(code, message, type_))


def error_response(code: int, message: str, type_: str) -> JSONResponse:
    return JSONResponse(error_body(code, message, type_), status_code=code)


def not_supported() -> JSONResponse:
    return error_response(501, "Endpoint is not supported in the TrustedRouter alpha", "not_supported_in_alpha")


def deprecated() -> JSONResponse:
    return error_response(410, "Endpoint is deprecated and not supported", "deprecated")

