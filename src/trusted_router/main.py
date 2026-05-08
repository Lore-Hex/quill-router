"""TrustedRouter app factory + non-inference route registration.

main.py is intentionally short. It owns:
  * `create_app()` — the FastAPI factory
  * exception handler wiring
  * non-inference route registration (auth, billing, console, ...)

Heavy logic lives elsewhere:
  * `middleware.py` — request_id, rate_limit, security_headers
  * `routes/inference.py` — chat/messages/responses/embeddings
  * each `routes/*.py` module owns one feature surface
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response

from trusted_router.axiom_config import init_axiom
from trusted_router.config import Settings, get_settings
from trusted_router.errors import error_response
from trusted_router.middleware import register_http_middleware
from trusted_router.routes.activity import register_activity_routes
from trusted_router.routes.auth import register_auth_routes
from trusted_router.routes.billing import register_billing_routes
from trusted_router.routes.broadcast import register_broadcast_routes
from trusted_router.routes.byok import register_byok_routes
from trusted_router.routes.catalog import register_catalog_routes
from trusted_router.routes.compat import register_compat_stub_routes
from trusted_router.routes.console import register_console_routes
from trusted_router.routes.email_verify import register_email_verify_routes
from trusted_router.routes.inference import register_inference_routes
from trusted_router.routes.internal import register_internal_routes
from trusted_router.routes.keys import register_key_routes
from trusted_router.routes.oauth import register_oauth_routes
from trusted_router.routes.oauth_keys import register_oauth_key_routes
from trusted_router.routes.public import register_public_routes
from trusted_router.routes.ses_notifications import register_ses_notification_routes
from trusted_router.routes.signup import register_signup_routes
from trusted_router.routes.wallet_oauth import register_wallet_oauth_routes
from trusted_router.routes.workspaces import register_workspace_routes
from trusted_router.sentry_config import init_sentry
from trusted_router.storage import configure_store, create_store
from trusted_router.types import ErrorType


def create_app(
    settings: Settings | None = None,
    *,
    configure_store_arg: bool = True,
    init_observability: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    if configure_store_arg:
        configure_store(create_store(settings))
    if init_observability:
        init_sentry(settings)
        init_axiom(settings)
    app = FastAPI(title="TrustedRouter", version="0.1.0")
    app.state.settings = settings

    register_http_middleware(app, settings)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> Response:
        # Redirect-shaped HTTPExceptions (e.g. console gating raising 302
        # to /?reason=signin) need to stay redirects, not become JSON
        # error envelopes.
        if exc.status_code in {301, 302, 303, 307, 308}:
            location = (exc.headers or {}).get("Location")
            if location:
                return RedirectResponse(url=location, status_code=exc.status_code)
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(
                exc.detail, status_code=exc.status_code, headers=exc.headers
            )
        return error_response(exc.status_code, str(exc.detail), ErrorType.HTTP_ERROR)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = (
            ".".join(str(part) for part in first.get("loc", []) if part != "body")
            or "body"
        )
        message = first.get("msg") or "Invalid request body"
        return error_response(400, f"{loc}: {message}", ErrorType.BAD_REQUEST)

    register_public_routes(app, settings)
    api = _make_api_router(settings)
    register_oauth_routes(app, api)
    register_console_routes(app)
    app.include_router(api)
    app.include_router(api, prefix="/v1")
    return app


def _make_api_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    inference_router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/coverage/openrouter")
    async def coverage() -> dict[str, Any]:
        from trusted_router.openrouter_coverage import ROUTE_COVERAGE

        return {
            "data": [
                {"path": item.path, "method": item.method, "kind": item.kind, "note": item.note}
                for item in ROUTE_COVERAGE
            ]
        }

    register_inference_routes(inference_router)

    register_catalog_routes(router)
    register_auth_routes(router)
    register_byok_routes(router)
    register_billing_routes(router)
    register_broadcast_routes(router)
    register_key_routes(router)
    register_oauth_key_routes(router)
    register_activity_routes(router)
    register_workspace_routes(router)

    if _control_plane_inference_enabled(settings):
        router.include_router(inference_router)
    register_compat_stub_routes(router)
    register_signup_routes(router)
    register_email_verify_routes(router)
    register_wallet_oauth_routes(router)
    register_ses_notification_routes(router)
    register_internal_routes(router)
    return router


def _control_plane_inference_enabled(settings: Settings) -> bool:
    """Inference handlers run in the *control plane* only in local/test
    environments — production inference goes through the attested
    enclave (api.quillrouter.com) and never touches this service. This
    gate keeps the local dev loop fast (no enclave dependency) while
    making it impossible to accidentally serve prompts from the non-
    attested control plane in production."""
    return settings.environment.lower() in {"local", "test"}


app = create_app()
