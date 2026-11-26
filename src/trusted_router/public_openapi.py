from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response
from fastapi.routing import APIRoute

_ASSET_DIR = Path(__file__).with_name("static")
_SCHEMA_PATH = _ASSET_DIR / "openapi-public.json"
_GZIP_SCHEMA_PATH = _ASSET_DIR / "openapi-public.json.gz"
CONTROL_PLANE_API_SERVER = "https://trustedrouter.com/v1"
# Every attested gateway region serves the same inference routes.
INFERENCE_API_SERVERS: tuple[dict[str, str], ...] = (
    {"url": "https://api.trustedrouter.com/v1", "description": "Global"},
    {"url": "https://api-europe-west4.quillrouter.com/v1", "description": "EU regional"},
)
HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})


def catalog_pricing_responses() -> dict[str, Any]:
    """Document the additive pricing fields without filtering catalog responses."""
    rates = {
        "prompt": {"type": "string", "description": "USD per input token."},
        "completion": {"type": "string", "description": "USD per output token."},
        "input_cache_read": {"type": "string", "description": "USD per cached input token, when declared."},
    }
    pricing = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            **rates,
            "tiers": {
                "type": "array",
                "minItems": 2,
                "description": (
                    "Ordered customer token prices, present for routes with multiple tiers; "
                    "the tier is chosen by total prompt tokens including cached tokens "
                    "(cache reads and cache creation)."
                ),
                "items": {
                    "type": "object",
                    "required": ["max_prompt_tokens", "prompt", "completion"],
                    "properties": {
                        "max_prompt_tokens": {
                            "anyOf": [{"type": "integer"}, {"type": "null"}],
                            "description": "Inclusive prompt-token bound; null for the final tier.",
                        },
                        **rates,
                    },
                },
            },
        },
    }
    return {"200": {"content": {"application/json": {"schema": {
        "type": "object",
        "properties": {"data": {"type": "array", "items": {
            "type": "object", "properties": {"pricing": pricing},
        }}},
    }}}}}


def inference_servers() -> list[dict[str, str]]:
    return [dict(server) for server in INFERENCE_API_SERVERS]


def document_router_server(router: APIRouter) -> None:
    """Label a registered gateway route group before it is mounted bare and under /v1."""
    for route in router.routes:
        if isinstance(route, APIRoute):
            route.openapi_extra = {**(route.openapi_extra or {}), "servers": inference_servers()}


# Schema metadata only: authentication continues to be enforced by the existing
# dependencies and handlers. Public catalog/pages and unsupported stubs stay public.
BEARER_SECURITY: list[dict[str, list[str]]] = [{"BearerAuth": []}]


def _requires_bearer(dependant: Dependant) -> bool:
    call = dependant.call
    if (
        getattr(call, "__module__", "") == "trusted_router.auth"
        and getattr(call, "__qualname__", "") in {
            "require_management", "require_inference_key", "require_authenticated",
            "require_scope.<locals>.dependency",
        }
    ):
        return True
    return any(_requires_bearer(child) for child in dependant.dependencies)


def _document_authentication(router: Any) -> None:
    for route in router.routes:
        # FastAPI >= 0.141 keeps included routers nested instead of flattening.
        if hasattr(route, "original_router"):
            _document_authentication(route.original_router)
        elif isinstance(route, APIRoute) and _requires_bearer(route.dependant):
            route.openapi_extra = {**(route.openapi_extra or {}), "security": BEARER_SECURITY}


def install_operation_servers(app: FastAPI, api: APIRouter) -> None:
    """Derive account/API ownership from registration, with gateway overrides.

    Public pages and account operations default to the control-plane host.
    Only registered inference groups and explicitly shared operations opt into
    the gateway. Work on the generated schema so nested routers and both /v1
    and bare mounts get the same treatment across FastAPI versions.
    """
    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        _document_authentication(app)
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "TrustedRouter API key",
            "description": (
                "Send Authorization: Bearer <TrustedRouter API key>. Management operations "
                "require a management key or management session; inference requires an inference key."
            ),
        }
        api_paths = get_openapi(title=app.title, version=app.version, routes=api.routes)["paths"]
        for path, path_item in schema["paths"].items():
            for method, operation in path_item.items():
                if method not in HTTP_METHODS:
                    continue
                is_bare_api = method in api_paths.get(path, {})
                default_server = (
                    CONTROL_PLANE_API_SERVER if is_bare_api
                    else CONTROL_PLANE_API_SERVER.removesuffix("/v1")
                )
                servers = operation.setdefault("servers", [{"url": default_server}])
                # OpenAPI appends the path to the server URL. Already-versioned
                # paths must not become /v1/v1/keys (or /v1/v1/chat/completions).
                if path.startswith("/v1/"):
                    operation["servers"] = [
                        {**server, "url": server["url"].removesuffix("/v1")}
                        for server in servers
                    ]
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


@dataclass(frozen=True)
class PublicOpenAPIPayload:
    body: bytes
    gzip_body: bytes
    etag: str
    gzip_etag: str


@lru_cache(maxsize=1)
def load_public_openapi_payload() -> PublicOpenAPIPayload:
    """Load pre-generated bytes; never construct route inventory at runtime."""

    body = _SCHEMA_PATH.read_bytes()
    gzip_body = _GZIP_SCHEMA_PATH.read_bytes()
    if gzip.decompress(gzip_body) != body:
        raise RuntimeError("public OpenAPI assets do not describe the same representation")
    return PublicOpenAPIPayload(
        body=body,
        gzip_body=gzip_body,
        etag=f'"{hashlib.sha256(body).hexdigest()}"',
        gzip_etag=f'"{hashlib.sha256(gzip_body).hexdigest()}"',
    )


def public_openapi_response(
    request: Request,
    payload: PublicOpenAPIPayload,
) -> Response:
    accepted: dict[str, float] = {}
    for item in request.headers.get("accept-encoding", "").split(","):
        token, *parameters = item.split(";")
        quality = 1.0
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if separator and name.lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
                if not 0.0 <= quality <= 1.0:
                    quality = 0.0
        accepted[token.strip().lower()] = quality
    explicit = [accepted[coding] for coding in ("gzip", "x-gzip") if coding in accepted]
    gzip_quality = max(explicit) if explicit else accepted.get("*", 0.0)
    serve_gzip = gzip_quality > 0
    body = payload.gzip_body if serve_gzip else payload.body
    etag = payload.gzip_etag if serve_gzip else payload.etag
    headers = {
        "Cache-Control": "public, max-age=300, s-maxage=3600",
        "ETag": etag,
        "Vary": "Accept-Encoding",
    }
    if serve_gzip:
        headers["Content-Encoding"] = "gzip"
    elif "gzip" in request.headers.get("accept-encoding", ""):
        # Prevent outer GZipMiddleware from changing the representation after
        # its strong identity ETag has been selected.
        headers["Content-Encoding"] = "identity"
    validators = {
        token.strip().removeprefix("W/").strip()
        for token in request.headers.get("if-none-match", "").split(",")
    }
    if "*" in validators or etag in validators:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
