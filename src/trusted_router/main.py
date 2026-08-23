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

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from trusted_router.analytics_sink import create_analytics_sink
from trusted_router.axiom_config import init_axiom
from trusted_router.catalog import validate_auto_model_order
from trusted_router.config import Settings, get_settings
from trusted_router.dashboard import public_not_found_html
from trusted_router.errors import error_response
from trusted_router.middleware import register_http_middleware
from trusted_router.public_openapi import (
    load_public_openapi_payload,
    public_openapi_response,
)
from trusted_router.routes.acquisition import register_acquisition_routes
from trusted_router.routes.activity import register_activity_routes
from trusted_router.routes.auth import register_auth_routes
from trusted_router.routes.bedrock_group_buy import (
    register_bedrock_group_buy_control_routes,
    register_bedrock_group_buy_public_routes,
)
from trusted_router.routes.billing import register_billing_routes
from trusted_router.routes.broadcast import register_broadcast_routes
from trusted_router.routes.byok import register_byok_routes
from trusted_router.routes.catalog import (
    register_authenticated_catalog_routes,
    register_catalog_routes,
)
from trusted_router.routes.chat_proxy import register_chat_proxy_routes
from trusted_router.routes.client_events import register_client_events_routes
from trusted_router.routes.compat import (
    register_compat_stub_routes,
    register_versioned_compat_stub_routes,
)
from trusted_router.routes.console import register_console_routes
from trusted_router.routes.custom_models import register_custom_model_routes
from trusted_router.routes.email_verify import register_email_verify_routes
from trusted_router.routes.identity_verify import register_identity_verify_routes
from trusted_router.routes.inference import register_inference_routes
from trusted_router.routes.internal import (
    register_control_internal_routes,
    register_external_webhook_routes,
    register_gateway_internal_routes,
    register_internal_routes,
    register_observer_internal_routes,
)
from trusted_router.routes.keys import register_key_routes
from trusted_router.routes.mcp import register_mcp_routes
from trusted_router.routes.notify import register_notify_public_routes, register_notify_routes
from trusted_router.routes.oauth import register_oauth_routes
from trusted_router.routes.oauth_keys import register_oauth_key_routes
from trusted_router.routes.provider_portal import register_provider_portal_routes
from trusted_router.routes.public import register_public_action_routes, register_public_routes
from trusted_router.routes.ses_notifications import register_ses_notification_routes
from trusted_router.routes.signup import register_signup_routes
from trusted_router.routes.user_models import register_user_model_routes
from trusted_router.routes.user_models_public import register_user_model_public_routes
from trusted_router.routes.verification_status import register_verification_status_routes
from trusted_router.routes.wallet_oauth import register_wallet_oauth_routes
from trusted_router.routes.workspaces import register_workspace_routes
from trusted_router.sentry_config import init_sentry
from trusted_router.storage import (
    STORE,
    configure_analytics_sink,
    configure_store,
    create_store,
)
from trusted_router.storage_errors import (
    conflict_store_error_types,
    transient_store_error_types,
)
from trusted_router.types import ErrorType


def create_app(
    settings: Settings | None = None,
    *,
    configure_store_arg: bool = True,
    init_observability: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    surface = settings.service_surface
    if (
        surface == "combined"
        and settings.environment.lower() not in {"local", "test"}
        and not settings.allow_deployed_combined_surface
    ):
        raise ValueError(
            "TR_SERVICE_SURFACE=combined is restricted to local/test unless the "
            "temporary TR_ALLOW_DEPLOYED_COMBINED_SURFACE=true migration bridge is "
            "explicitly enabled"
        )
    validate_auto_model_order(settings.auto_model_order)
    if configure_store_arg:
        configure_store(create_store(settings))
        # No-op unless TR_CLICKHOUSE_URL is set; Bigtable stays authoritative.
        configure_analytics_sink(create_analytics_sink(settings))
    if init_observability:
        init_sentry(settings)
        init_axiom(settings)
    # Swagger UI moves to /api/reference so the public docs hub can own
    # /docs (the marketing nav points "Docs" there). ReDoc stays at /redoc.
    app = FastAPI(
        title="TrustedRouter",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        # A combined local/test app keeps FastAPI's dynamic schema. The public
        # service installs a pre-serialized, comprehensive customer schema
        # after route registration; protected and regional observer services
        # expose no local schema endpoint.
        openapi_url="/openapi.json" if surface == "combined" else None,
    )
    app.state.settings = settings

    # Deferred settlement's forwarder + reaper. In-process, gated on the peer
    # config actually being set — no external scheduler is a hard dependency
    # on a plane whose entire purpose is fewer dependencies. Jittered so a
    # fleet of instances doesn't synchronize; single-flight inside the
    # forwarder collapses overlaps; daemon threads die with the process.
    if (
        surface in {"combined", "internal"}
        and settings.federation_deferred_settlement_enabled
        and settings.federation_settlement_home_token
    ):

        @app.on_event("startup")
        async def _start_home_settlement_loop() -> None:  # pragma: no cover - thread wiring
            import logging as _logging
            import random  # noqa: S311 - jitter, not cryptography
            import threading
            import time

            from trusted_router.services.home_settlement import drain_home_settlements
            from trusted_router.storage import STORE

            def loop() -> None:
                from trusted_router.synthetic.fleet import record_heartbeat

                passes = 0
                while True:
                    time.sleep(random.uniform(20, 45))  # noqa: S311
                    try:
                        drain_home_settlements(settings, limit=50)
                    except Exception:
                        _logging.getLogger(__name__).exception("home settlement pass failed")
                    # Liveness, not success: the heartbeat says the loop is
                    # running; a failing pass already logs its own exception.
                    record_heartbeat("scheduler:home-settlement", settings=settings)
                    passes += 1
                    # Reap roughly every tenth pass (~5 min): abandoned
                    # authorizations accumulate on deploy restarts, not
                    # per-second, so this cadence is generous.
                    if passes % 10 == 0:
                        reap = getattr(STORE, "reap_expired_deferred_authorizations", None)
                        if reap is not None:
                            try:
                                reap(limit=100)
                            except Exception:
                                _logging.getLogger(__name__).exception("deferred reap failed")

            threading.Thread(target=loop, name="home-settlement", daemon=True).start()

    # Split-surface auto-refill handoff. The internal settle path durably
    # attaches a request to its settle-outbox row but owns no Stripe secret;
    # only a payment-owning process claims that work. Today's control plane is
    # still the legacy combined surface; it both preserves its historical
    # in-process trigger and drains requests emitted by split internal nodes.
    if surface in {"combined", "control"} and settings.settle_outbox_enabled:

        @app.on_event("startup")
        async def _start_auto_refill_outbox_loop() -> None:  # pragma: no cover - task wiring
            import asyncio as _asyncio
            import logging as _logging
            import random as _random

            from trusted_router.services.auto_refill_outbox_drain import (
                drain_auto_refill_outbox,
            )
            from trusted_router.synthetic.fleet import record_heartbeat

            async def loop() -> None:
                await _asyncio.sleep(_random.uniform(5, 30))  # noqa: S311
                while True:
                    try:
                        await _asyncio.to_thread(
                            drain_auto_refill_outbox,
                            settings,
                            limit=100,
                        )
                    except Exception:
                        _logging.getLogger(__name__).exception(
                            "auto-refill outbox pass failed"
                        )
                    await _asyncio.to_thread(
                        record_heartbeat,
                        "scheduler:auto-refill-outbox",
                        settings=settings,
                    )
                    await _asyncio.sleep(30)

            _asyncio.create_task(loop())  # noqa: RUF006 - lifetime is the process

    if (
        surface in {"combined", "control"}
        and settings.activation_reminder_interval_seconds > 0
    ):

        @app.on_event("startup")
        async def _start_activation_reminder_loop() -> None:  # pragma: no cover - thread wiring
            import asyncio as _asyncio
            import logging as _logging
            import random as _random

            from trusted_router.services.activation_reminders import (
                run_activation_reminder_pass,
            )

            interval = max(30, settings.activation_reminder_interval_seconds)
            log = _logging.getLogger(__name__)

            async def loop() -> None:
                await _asyncio.sleep(_random.uniform(5, min(30, interval)))  # noqa: S311
                while True:
                    try:
                        await _asyncio.to_thread(
                            run_activation_reminder_pass,
                            settings,
                            limit=200,
                        )
                    except Exception:
                        log.exception("activation reminder pass failed")
                    await _asyncio.sleep(interval)

            _asyncio.create_task(loop())  # noqa: RUF006 - lifetime is the process

    # In-process synthetic monitor. See the settings docstring for why the
    # trigger lives here rather than in each cloud's own scheduler. Deployments
    # that use this observer owner must pin the service to one replica; the AWS
    # observer instead disables it and uses its existing EventBridge rule.
    if (
        surface in {"combined", "internal", "observer"}
        and settings.synthetic_scheduler_interval_seconds > 0
    ):

        @app.on_event("startup")
        async def _start_synthetic_loop() -> None:  # pragma: no cover - thread wiring
            import asyncio as _asyncio
            import logging as _logging
            import random as _random

            from trusted_router.routes.internal.synthetic import run_synthetic_pass
            from trusted_router.synthetic.fleet import record_heartbeat

            interval = max(60, settings.synthetic_scheduler_interval_seconds)
            log = _logging.getLogger(__name__)

            async def loop() -> None:
                # Jitter the FIRST tick only: several replicas starting from
                # one deploy would otherwise probe in lockstep forever, and
                # simultaneous passes multiply the inference spend without
                # improving coverage.
                await _asyncio.sleep(_random.uniform(5, min(60, interval)))  # noqa: S311
                while True:
                    try:
                        await run_synthetic_pass(
                            settings,
                            rotation_count=settings.synthetic_scheduler_rotation_count,
                        )
                    except Exception:
                        # A failed pass must never kill the loop: the status
                        # page going permanently stale is a worse outcome than
                        # one bad sample, and staleness is exactly the failure
                        # that hides an outage.
                        log.exception("synthetic pass failed")
                    # Liveness, not success: a dead loop is the failure shape
                    # this heartbeat exists to expose on /fleet.
                    await _asyncio.to_thread(
                        record_heartbeat, "scheduler:synthetic", settings=settings
                    )
                    await _asyncio.sleep(interval)

            _asyncio.create_task(loop())  # noqa: RUF006 - lifetime is the process

    # The standing remediator (command-center Inc 3): detect -> decide ->
    # record on a fixed cadence, on every control plane, outside deploy
    # windows. Observe mode by default; see synthetic/remediator.py. As with
    # the synthetic loop, an observer deployment that retains this owner must
    # be pinned to one replica.
    if (
        surface in {"combined", "internal", "observer"}
        and settings.remediator_mode != "off"
        and settings.remediator_in_process_enabled
    ):

        @app.on_event("startup")
        async def _start_remediator_loop() -> None:  # pragma: no cover - thread wiring
            import asyncio as _asyncio
            import logging as _logging
            import random as _random

            from trusted_router.synthetic.fleet import record_heartbeat
            from trusted_router.synthetic.remediator import run_remediator_pass

            interval = max(60, settings.remediator_interval_seconds)
            log = _logging.getLogger(__name__)

            async def loop() -> None:
                await _asyncio.sleep(_random.uniform(5, min(60, interval)))  # noqa: S311
                while True:
                    # Publish liveness before reading the eventually
                    # consistent analytics copy. During a rolling deploy a
                    # fresh instance must not page on the retired instance's
                    # heartbeat before announcing itself.
                    await _asyncio.to_thread(
                        record_heartbeat, "scheduler:remediator", settings=settings
                    )
                    try:
                        await _asyncio.to_thread(run_remediator_pass, settings)
                    except Exception:
                        # The watcher must be the last thing to die quietly.
                        log.exception("remediator pass failed")
                    # A pass can cross a five-minute heartbeat bucket. The
                    # completion write records that progress without adding a
                    # duplicate row when it stayed in the same bucket.
                    await _asyncio.to_thread(
                        record_heartbeat, "scheduler:remediator", settings=settings
                    )
                    await _asyncio.sleep(interval)

            _asyncio.create_task(loop())  # noqa: RUF006 - lifetime is the process

    register_http_middleware(app, settings)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        # Redirect-shaped HTTPExceptions (e.g. console gating raising 302
        # to /?reason=signin) need to stay redirects, not become JSON
        # error envelopes.
        if exc.status_code in {301, 302, 303, 307, 308}:
            location = (exc.headers or {}).get("Location")
            if location:
                return RedirectResponse(url=location, status_code=exc.status_code)
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(exc.detail, status_code=exc.status_code, headers=exc.headers)
        if exc.status_code == 404 and _is_public_html_request(request):
            return HTMLResponse(
                public_not_found_html(settings, request.url.path),
                status_code=404,
                headers=exc.headers,
            )
        response = error_response(
            exc.status_code,
            str(exc.detail),
            ErrorType.HTTP_ERROR,
        )
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", []) if part != "body") or "body"
        message = first.get("msg") or "Invalid request body"
        return error_response(400, f"{loc}: {message}", ErrorType.BAD_REQUEST)

    async def aborted_exception_handler(_request: Request, _exc: Exception) -> Response:
        # A storage transaction exhausted its retry budget under contention.
        # This is transient, so signal the caller to retry rather than
        # surfacing a 500.
        response = error_response(
            503,
            "The request was aborted due to transient database contention; retry.",
            ErrorType.SERVICE_UNAVAILABLE,
        )
        response.headers["Retry-After"] = "1"
        return response

    # Registered per concrete type rather than via a decorator: Starlette
    # dispatches exception handlers by class, and which classes represent
    # "a concurrent writer won" is backend-specific (Spanner's Aborted today,
    # a serialization failure on a SQL backend). storage_errors owns that
    # mapping so this module never imports a cloud SDK.
    for conflict_type in conflict_store_error_types():
        app.add_exception_handler(conflict_type, aborted_exception_handler)

    async def unavailable_exception_handler(_request: Request, _exc: Exception) -> Response:
        response = error_response(
            503,
            "Persistent storage is temporarily unavailable; retry.",
            ErrorType.SERVICE_UNAVAILABLE,
        )
        response.headers["Retry-After"] = "1"
        return response

    conflict_types = set(conflict_store_error_types())
    for transient_type in transient_store_error_types():
        if transient_type not in conflict_types:
            app.add_exception_handler(transient_type, unavailable_exception_handler)

    if surface in {"combined", "public", "observer"}:
        register_public_routes(app, settings)
    if surface in {"combined", "public"}:
        register_bedrock_group_buy_public_routes(app, settings)
    if surface in {"combined", "actions"}:
        register_public_action_routes(app, settings)
    if surface in {"combined", "control"}:
        register_bedrock_group_buy_control_routes(app, settings)
    api = _make_api_router(settings, surface)
    if surface in {"combined", "control"}:
        register_oauth_routes(app, api)
        register_console_routes(app)
        register_provider_portal_routes(app)
        register_mcp_routes(app, settings)
        # Same-origin streaming pipe for the authenticated chat playground.
        # It is deliberately absent from anonymous and billing processes.
        register_chat_proxy_routes(app)
    app.include_router(api)
    app.include_router(api, prefix="/v1")
    if surface in {"combined", "control"}:
        versioned_compat = APIRouter()
        register_versioned_compat_stub_routes(versioned_compat)
        app.include_router(versioned_compat, prefix="/v1")
    if surface == "public":
        # The reference lives with the anonymous site, while account and API
        # routes live on the protected control service. Load deterministic,
        # pre-serialized bytes so a cold-start flood cannot make every public
        # instance construct the protected route inventory or serialize a
        # multi-megabyte schema per request.
        payload = load_public_openapi_payload()

        async def public_openapi(request: Request) -> Response:
            return public_openapi_response(request, payload)

        # Use a Starlette Route rather than an APIRoute: this transport endpoint
        # serves the already-built contract and is not itself part of that
        # contract or the application surface-ownership inventory.
        app.add_route(
            "/openapi.json",
            public_openapi,
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
    return app


def _is_public_html_request(request: Request) -> bool:
    if request.method not in {"GET", "HEAD"}:
        return False
    if "text/html" not in request.headers.get("accept", "").lower():
        return False
    path = request.url.path
    non_public_prefixes = (
        "/v1",
        "/internal",
        "/api",
        "/auth",
        "/oauth",
        "/console",
        "/static",
        "/webhooks",
        "/.well-known",
    )
    return not any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in non_public_prefixes
    )


def _make_api_router(settings: Settings, surface: str) -> APIRouter:
    router = APIRouter()
    inference_router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/ready")
    def ready() -> Response:
        if surface in {"public", "actions"}:
            return JSONResponse(
                status_code=200,
                content={
                    "status": "ready",
                    "checks": {"http_surface": "ready"},
                },
            )
        try:
            STORE.readiness_check()
        except Exception:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "checks": {"billing_store": "unavailable"},
                },
                headers={"Retry-After": "5"},
            )
        return JSONResponse(
            status_code=200,
            content={
                "status": "ready",
                "checks": {"billing_store": "ready"},
            },
        )

    if surface in {"combined", "public", "observer"}:

        @router.get("/coverage/openrouter")
        async def coverage() -> dict[str, Any]:
            from trusted_router.openrouter_coverage import ROUTE_COVERAGE

            return {
                "data": [
                    {
                        "path": item.path,
                        "method": item.method,
                        "kind": item.kind,
                        "note": item.note,
                    }
                    for item in ROUTE_COVERAGE
                ]
            }

        register_acquisition_routes(router)
        register_catalog_routes(router)

    if surface in {"combined", "public"}:
        register_notify_public_routes(router)

    if surface in {"combined", "public"}:
        # Regional observer deployments use Postgres, whose deliberately
        # narrow observer projection does not implement the cross-tenant
        # published-model listing. Keep these anonymous Store-backed reads on
        # the dedicated public surface instead of turning observer traffic
        # into 500s (or widening the observer database role).
        register_user_model_public_routes(router)

    if surface in {"combined", "control"}:
        register_authenticated_catalog_routes(router)
        register_auth_routes(router)
        register_byok_routes(router)
        register_billing_routes(router)
        register_broadcast_routes(router)
        register_custom_model_routes(router)
        register_user_model_routes(router)
        register_key_routes(router)
        register_oauth_key_routes(router)
        register_activity_routes(router)
        register_client_events_routes(router)
        register_workspace_routes(router)
        if _control_plane_inference_enabled(settings):
            register_inference_routes(inference_router)
            router.include_router(inference_router)
        register_compat_stub_routes(router)
        register_signup_routes(router)
        register_email_verify_routes(router)
        register_verification_status_routes(router)
        register_identity_verify_routes(router)
        register_notify_routes(router)
        register_wallet_oauth_routes(router)
        register_ses_notification_routes(router, settings)

    if surface == "combined":
        register_internal_routes(router)
    elif surface == "control":
        register_external_webhook_routes(router)
        register_control_internal_routes(router)
    elif surface == "internal":
        register_gateway_internal_routes(router)
        register_observer_internal_routes(router)
    elif surface == "observer":
        register_observer_internal_routes(router)
    return router


def _control_plane_inference_enabled(settings: Settings) -> bool:
    """Inference handlers run in the *control plane* only in local/test
    environments — production inference goes through the attested
    enclave (api.trustedrouter.com) and never touches this service. This
    gate keeps the local dev loop fast (no enclave dependency) while
    making it impossible to accidentally serve prompts from the non-
    attested control plane in production."""
    return settings.environment.lower() in {"local", "test"}


app = create_app()
