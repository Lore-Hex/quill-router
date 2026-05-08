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
