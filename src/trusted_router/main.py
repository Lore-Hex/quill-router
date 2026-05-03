from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from trusted_router.adapter import messages_to_chat_body, responses_to_chat_body
from trusted_router.auth import (
    InferencePrincipal,
    Principal,
    SettingsDep,
    get_authorization_bearer,
)
from trusted_router.catalog import AUTO_MODEL_ID, MODELS, Model
from trusted_router.config import Settings, get_settings
from trusted_router.errors import api_error, error_response, not_supported
from trusted_router.routes.activity import register_activity_routes
from trusted_router.routes.auth import register_auth_routes
from trusted_router.routes.billing import register_billing_routes
from trusted_router.routes.byok import register_byok_routes
from trusted_router.routes.catalog import register_catalog_routes
from trusted_router.routes.compat import register_compat_stub_routes
from trusted_router.routes.console import register_console_routes
from trusted_router.routes.email_verify import register_email_verify_routes
from trusted_router.routes.helpers import json_body
from trusted_router.routes.internal import register_internal_routes
from trusted_router.routes.keys import register_key_routes
from trusted_router.routes.oauth import register_oauth_routes
from trusted_router.routes.oauth_keys import register_oauth_key_routes
from trusted_router.routes.public import register_public_routes
from trusted_router.routes.ses_notifications import register_ses_notification_routes
from trusted_router.routes.signup import register_signup_routes
from trusted_router.routes.wallet_oauth import register_wallet_oauth_routes
from trusted_router.routes.workspaces import register_workspace_routes
from trusted_router.routing import (
    chat_route_candidates,
    chat_route_endpoint_candidates,
    provider_route_preferences,
)
from trusted_router.sentry_config import init_sentry
from trusted_router.services.inference import (
    run_chat,
    run_chat_candidates,
    run_chat_candidates_stream,
    run_chat_stream,
    run_messages_stream,
)
from trusted_router.storage import STORE, configure_store, create_store
from trusted_router.types import ErrorType, UsageType


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
    app = FastAPI(title="TrustedRouter", version="0.1.0")
    app.state.settings = settings

    @app.middleware("http")
    async def rate_limit_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        limited = _rate_limit_request(request, settings)
        if limited is not None:
            return limited
        return await call_next(request)

    @app.middleware("http")
    async def security_headers_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Set HSTS so browsers remember to skip http:// on subsequent
        visits. The HTTP→HTTPS redirect at the LB still handles the
        first visit; HSTS protects every visit after that. We use a
        2-year max-age (the HSTS preload list minimum) and includeSubDomains
        so future subdomains (`www`, `console`, `docs`...) inherit the
        guarantee. Set conservatively — no `preload` directive yet
        because submitting to the Chrome preload list is a one-way
        commitment."""
        response = await call_next(request)
        response.headers.setdefault(
            "strict-transport-security",
            "max-age=63072000; includeSubDomains",
        )
        return response

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
            return JSONResponse(exc.detail, status_code=exc.status_code, headers=exc.headers)
        return error_response(exc.status_code, str(exc.detail), ErrorType.HTTP_ERROR)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", []) if part != "body") or "body"
        message = first.get("msg") or "Invalid request body"
        return error_response(400, f"{loc}: {message}", ErrorType.BAD_REQUEST)

    register_public_routes(app, settings)
    api = _make_api_router(settings)
    register_oauth_routes(app, api)
    register_console_routes(app)
    app.include_router(api)
    app.include_router(api, prefix="/v1")
    return app


def _rate_limit_request(request: Request, settings: Settings) -> JSONResponse | None:
    if not settings.rate_limit_enabled:
        return None
    path = request.url.path
    if path in {"/health", "/v1/health"} or path.startswith(("/docs", "/openapi.json")):
        return None

    bearer = get_authorization_bearer(request)
    internal_token = request.headers.get("x-trustedrouter-internal-token")
    user = request.headers.get("x-trustedrouter-user")
    ip = _client_ip(request)
    if path.startswith(("/internal/", "/v1/internal/")):
        namespace = "internal"
        subject = _fingerprint(internal_token or bearer or ip)
        limit = settings.rate_limit_internal_per_window
    elif bearer:
        namespace = "key"
        subject = _fingerprint(bearer)
        limit = settings.rate_limit_key_per_window
    else:
        namespace = "ip"
        subject = _fingerprint(user or ip)
        limit = settings.rate_limit_ip_per_window

    hit = STORE.hit_rate_limit(
        namespace=namespace,
        subject=subject,
        limit=limit,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if hit.allowed:
        return None
    response = error_response(429, "Rate limit exceeded", ErrorType.RATE_LIMITED)
    response.headers["Retry-After"] = str(hit.retry_after_seconds)
    response.headers["X-RateLimit-Limit"] = str(hit.limit)
    response.headers["X-RateLimit-Remaining"] = str(hit.remaining)
    response.headers["X-RateLimit-Reset"] = hit.reset_at
    return response


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    return request.client.host if request.client else "unknown"


def _fingerprint(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


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

    @inference_router.post("/chat/completions")
    async def chat_completions(
        request: Request,
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> Any:
        body = await json_body(request)
        _validate_chat_messages(body)
        provider_prefs = provider_route_preferences(body)
        usage_type = UsageType.coerce(provider_prefs.usage_type) if provider_prefs.usage_type else None
        if usage_type is None:
            candidates = chat_route_candidates(body, settings)
        else:
            candidates = [model for model, _endpoint in chat_route_endpoint_candidates(body, settings)]
        requested_model = str(body.get("model") or (body.get("models") or [""])[0])
        if len(candidates) > 1 or requested_model == AUTO_MODEL_ID:
            if body.get("stream") is True:
                return StreamingResponse(
                    _candidate_stream_bytes(
                        body,
                        candidates,
                        requested_model=requested_model,
                        principal=principal,
                        settings=settings,
                        app_name=_app_name(request),
                        usage_type=usage_type,
                    ),
                    media_type="text/event-stream",
                    headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
                )
            result, generation, selected_model, failures = await run_chat_candidates(
                body,
                candidates,
                principal,
                settings,
                app_name=_app_name(request),
                usage_type=usage_type,
            )
            return JSONResponse(
                {
                    "id": result.request_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": selected_model.id,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": result.text},
                            "finish_reason": result.finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": result.input_tokens,
                        "completion_tokens": result.output_tokens,
                        "total_tokens": result.input_tokens + result.output_tokens,
                    },
                    "trustedrouter": {
                        "generation_id": generation.id,
                        "content_stored": False,
                        "requested_model": requested_model,
                        "selected_model": selected_model.id,
                        "rollover_failures": failures,
                    },
                }
            )
        model = candidates[0]
        if body.get("stream") is True:
            return StreamingResponse(
                run_chat_stream(
                    body,
                    model,
                    principal,
                    settings,
                    app_name=_app_name(request),
                    usage_type=usage_type,
                ),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        result, generation = await run_chat(
            body,
            model,
            principal,
            settings,
            app_name=_app_name(request),
            usage_type=usage_type,
        )
        return JSONResponse(
            {
                "id": result.request_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model.id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.text},
                        "finish_reason": result.finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": result.input_tokens,
                    "completion_tokens": result.output_tokens,
                    "total_tokens": result.input_tokens + result.output_tokens,
                },
                "trustedrouter": {"generation_id": generation.id, "content_stored": False},
            }
        )

    @inference_router.post("/messages")
    async def messages(
        request: Request,
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> Response:
        body = await json_body(request)
        model = _require_messages_model(body)
        chat_body = messages_to_chat_body(body, model_id=model.id)
        if body.get("stream") is True:
            return StreamingResponse(
                run_messages_stream(
                    chat_body,
                    model,
                    principal,
                    settings,
                    app_name=_app_name(request),
                ),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        result, generation = await run_chat(chat_body, model, principal, settings, app_name=_app_name(request))
        return JSONResponse(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": model.id,
                "content": [{"type": "text", "text": result.text}],
                "stop_reason": result.finish_reason,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
                "trustedrouter": {"generation_id": generation.id, "content_stored": False},
            }
        )

    @inference_router.post("/embeddings")
    async def embeddings() -> JSONResponse:
        return not_supported()

    register_catalog_routes(router)
    register_auth_routes(router)
    register_byok_routes(router)
    register_billing_routes(router)
    register_key_routes(router)
    register_oauth_key_routes(router)
    register_activity_routes(router)
    register_workspace_routes(router)

    @inference_router.post("/responses")
    async def responses(
        request: Request,
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        body = await json_body(request)
        chat_body = responses_to_chat_body(body)
        model = _require_chat_model(chat_body)
        result, generation = await run_chat(
            chat_body,
            model,
            principal,
            settings,
            app_name=_app_name(request),
        )
        return JSONResponse(
            {
                "id": f"resp_{uuid.uuid4().hex}",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": model.id,
                "output": [
                    {
                        "id": f"msg_{uuid.uuid4().hex}",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": result.text,
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "total_tokens": result.input_tokens + result.output_tokens,
                },
                "trustedrouter": {"generation_id": generation.id, "content_stored": False},
            }
        )

    if _control_plane_inference_enabled(settings):
        router.include_router(inference_router)
    register_compat_stub_routes(router)
    register_signup_routes(router)
    register_email_verify_routes(router)
    register_wallet_oauth_routes(router)
    register_ses_notification_routes(router)
    register_internal_routes(router)
    return router


def _require_chat_model(body: dict[str, Any]) -> Model:
    model_id = str(body.get("model") or "")
    if not model_id:
        raise api_error(400, "model is required", ErrorType.BAD_REQUEST)
    model = MODELS.get(model_id)
    if model is None or not model.supports_chat:
        raise api_error(400, "Model does not support chat completions", ErrorType.MODEL_NOT_SUPPORTED)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise api_error(400, "messages must contain at least one item", ErrorType.BAD_REQUEST)
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise api_error(400, f"messages[{index}] must be an object", ErrorType.BAD_REQUEST)
        if message.get("role") not in {"system", "user", "assistant", "tool", "developer"}:
            raise api_error(400, f"messages[{index}].role is unsupported", ErrorType.BAD_REQUEST)
        if "content" not in message:
            raise api_error(400, f"messages[{index}].content is required", ErrorType.BAD_REQUEST)
    return model


def _validate_chat_messages(body: dict[str, Any]) -> None:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise api_error(400, "messages must contain at least one item", ErrorType.BAD_REQUEST)
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise api_error(400, f"messages[{index}] must be an object", ErrorType.BAD_REQUEST)
        if message.get("role") not in {"system", "user", "assistant", "tool", "developer"}:
            raise api_error(400, f"messages[{index}].role is unsupported", ErrorType.BAD_REQUEST)
        if "content" not in message:
            raise api_error(400, f"messages[{index}].content is required", ErrorType.BAD_REQUEST)


def _require_messages_model(body: dict[str, Any]) -> Model:
    model_id = str(body.get("model") or "")
    if not model_id:
        raise api_error(400, "model is required", ErrorType.BAD_REQUEST)
    model = MODELS.get(model_id)
    if model is None or not model.supports_messages:
        raise api_error(400, "Model does not support Anthropic Messages", ErrorType.MODEL_NOT_SUPPORTED)
    return model


def _control_plane_inference_enabled(settings: Settings) -> bool:
    return settings.environment.lower() in {"local", "test"}


def _app_name(request: Request) -> str:
    return (
        request.headers.get("x-title")
        or request.headers.get("http-referer")
        or request.headers.get("referer")
        or "TrustedRouter"
    )


async def _candidate_stream_bytes(
    body: dict[str, Any],
    candidates: list[Model],
    *,
    requested_model: str,
    principal: Principal,
    settings: Settings,
    app_name: str,
    usage_type: UsageType | None = None,
) -> AsyncIterator[bytes]:
    selected: str | None = None
    async for model, chunk in run_chat_candidates_stream(
        body,
        candidates,
        principal,
        settings,
        app_name=app_name,
        usage_type=usage_type,
    ):
        if selected is None:
            selected = model.id
            yield (
                "event: trustedrouter.route\n"
                f'data: {{"requested_model":"{requested_model}","selected_model":"{selected}"}}\n\n'
            ).encode()
        yield chunk




app = create_app()
