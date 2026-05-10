"""HTTP middleware shared across the FastAPI app.

Three middlewares register in order from outermost to innermost:
  1. request_id  — mints/accepts a per-request id, echoes in response
                   header, makes it available as request.state.request_id.
  2. rate_limit  — enforces per-(key|ip|internal-token) windowed limits
                   via STORE.hit_rate_limit; logs structured 429s with
                   the request_id from (1).
  3. security_headers — sets HSTS so browsers remember to skip http://
                        on subsequent visits.

Splitting these out of main.py keeps the app factory readable. The
middleware here has no FastAPI dependencies beyond Request/Response;
it could be reused by other ASGI services in the same project.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from hashlib import sha256

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from trusted_router.auth import get_authorization_bearer
from trusted_router.config import Settings
from trusted_router.errors import error_response
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def register_http_middleware(app: FastAPI, settings: Settings) -> None:
    """Wire all three middlewares onto `app` in the right order.

    Starlette wraps middleware in reverse-add order: the FIRST one
    registered runs first on the way in (outermost wrap). We want
    request_id to mint the id before rate_limit logs a 429 against it,
    so request_id is registered first.
    """

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Mint (or accept from upstream) a per-request id; stash on
        `request.state.request_id`. Echoed in every response as
        `X-TrustedRouter-Request-Id` and surfaced to all downstream
        handlers + log extras for correlation across middleware,
        rate-limit decisions, inference, and Bigtable write failures.

        Accepts an upstream-provided id (`X-Request-Id`, common LB
        header) if it looks safe (alnum + dashes/underscores, ≤64
        chars); else mints one. This means traces survive the LB hop
        without the LB being able to inject log-injection payloads."""
        upstream = request.headers.get("x-request-id", "").strip()
        if (
            upstream
            and len(upstream) <= 64
            and all(c.isalnum() or c in "-_" for c in upstream)
        ):
            request_id = upstream
        else:
            request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers.setdefault("X-TrustedRouter-Request-Id", request_id)
        return response

    @app.middleware("http")
    async def read_only_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Operational read-only mode. When `settings.read_only` is True,
        every write-style request (POST/PUT/PATCH/DELETE) returns 503
        with a `Retry-After` header. GET/HEAD/OPTIONS pass through.

        Used during the Spanner regional → nam6 cutover (Stage 1 of the
        multi-region expansion plan) to pause all writes for the ~30 min
        backup→restore→env-var-flip window. Reads keep working off the
        old instance; writes 503 with `Retry-After: 1800` so SDKs back
        off and retry on the new instance after the cutover.

        We deliberately allow the OPTIONS method (CORS preflight)
        through so browsers don't fail their preflight before they even
        try the real request — that produces confusing CORS errors in
        the console instead of a clean 503 with a retry hint.

        Health checks (`/health`, `/v1/health`) bypass too — the LB and
        watchdog need to keep seeing the service as up so they don't
        rip the region out of rotation while we're just doing
        maintenance.
        """
        if not settings.read_only:
            return await call_next(request)
        method = request.method.upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            return await call_next(request)
        path = request.url.path
        if path in {"/health", "/v1/health", "/healthz", "/v1/healthz"}:
            return await call_next(request)
        log.info(
            "read_only.write_blocked method=%s path=%s",
            method,
            path,
            extra={"request_id": getattr(request.state, "request_id", "")},
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": 503,
                    "message": "Service temporarily in read-only mode for planned maintenance. Retry in 30 minutes.",
                    "type": ErrorType.SERVICE_UNAVAILABLE.value,
                }
            },
            headers={"Retry-After": "1800"},
        )

    @app.middleware("http")
    async def rate_limit_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Read-only mode bypasses rate-limiting entirely: STORE.hit_rate_limit
        # writes to the Spanner rate_limit table on every request (it's a
        # windowed-counter increment), and during a Stage-1 cutover we
        # need ALL writes silent so the snapshot we exported on the
        # source matches the snapshot we imported on nam6. Without this
        # bypass GETs continue rate-limit-writing through the read-only
        # window — we observed ~9 rate_limit rows landing on source after
        # Phase B during the 2026-05-10 cutover, missed by Phase A's
        # export. Skipping the limiter for the cutover window is safe
        # because the window is short (~30min) and traffic is bounded by
        # LB capacity anyway; rate limits resume the moment Phase E
        # drops the flag.
        if settings.read_only:
            return await call_next(request)
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
        2-year max-age (the HSTS preload list minimum) and
        includeSubDomains so future subdomains (`www`, `console`,
        `docs`...) inherit the guarantee. Set conservatively — no
        `preload` directive yet because submitting to the Chrome
        preload list is a one-way commitment."""
        response = await call_next(request)
        response.headers.setdefault(
            "strict-transport-security",
            "max-age=63072000; includeSubDomains",
        )
        return response


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
    request_id = getattr(request.state, "request_id", None)
    log.info(
        "rate_limit.exceeded",
        extra={
            "request_id": request_id,
            "namespace": namespace,
            "subject_fingerprint": subject,
            "path": path,
            "limit": hit.limit,
            "retry_after_seconds": hit.retry_after_seconds,
        },
    )
    response = error_response(429, "Rate limit exceeded", ErrorType.RATE_LIMITED)
    response.headers["Retry-After"] = str(hit.retry_after_seconds)
    response.headers["X-RateLimit-Limit"] = str(hit.limit)
    response.headers["X-RateLimit-Remaining"] = str(hit.remaining)
    response.headers["X-RateLimit-Reset"] = hit.reset_at
    if request_id:
        response.headers.setdefault("X-TrustedRouter-Request-Id", request_id)
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
