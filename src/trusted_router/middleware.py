"""HTTP middleware shared across the FastAPI app.

HTTP middleware executes from outermost to innermost as security headers,
trusted-source rate admission, read-only mode, narrow OAuth CORS, public
pageview accounting, canonical-host redirects, request-id handling, request
body admission, and response gzip. Route authentication adds one validated
credential bucket after that source admission.

Starlette's GZipMiddleware wraps compressible responses larger than 1 KiB.
It explicitly excludes ``text/event-stream``, so inference streaming remains
unbuffered while catalog, documentation, and public SEO pages compress.

Splitting these out of main.py keeps the app factory readable. The
middleware here has no FastAPI dependencies beyond Request/Response;
it could be reused by other ASGI services in the same project.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from trusted_router.acquisition import (
    acquisition_request_is_automated,
    pageview_attribution_fields,
    prepare_request_attribution,
    set_attribution_cookie,
)
from trusted_router.auth import get_authorization_bearer
from trusted_router.config import Settings, parse_settlement_inbound_tokens
from trusted_router.domains import (
    control_domain_for_hostname,
    is_status_hostname,
    is_www_hostname,
    request_hostname,
)
from trusted_router.errors import error_response
from trusted_router.markdown_negotiation import (
    MARKDOWN_CONTENT_TYPE,
    MARKDOWN_VARY,
    html_to_markdown,
    is_public_page_path,
    prefers_markdown,
)
from trusted_router.request_body_limit import (
    RequestBodyLimitMiddleware,
    UnreadRequestBodyCloseMiddleware,
)
from trusted_router.request_limits import (
    AUTHENTICATED_LIMITER_STATE,
    fingerprint_subject,
    normalized_client_identity,
    rate_limit_headers,
)
from trusted_router.security import constant_time_equal
from trusted_router.storage_rate_limits import InMemoryRateLimits
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)

OAUTH_KEY_EXCHANGE_PATHS = frozenset({"/auth/keys", "/v1/auth/keys"})
OAUTH_KEY_EXCHANGE_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Max-Age": "600",
}
STATUS_HOST_EXACT_PATHS = frozenset(
    {
        "/",
        "/favicon.ico",
        "/health",
        "/healthz",
        "/ready",
        "/og.png",
        "/robots.txt",
        "/status",
        "/status.json",
        "/v1/health",
        "/v1/healthz",
        "/v1/ready",
    }
)
STATUS_HOST_PATH_PREFIXES = ("/static/", "/status/")
COOKIE_FREE_PUBLIC_ANALYTICS_PATHS = frozenset(
    {
        "/leaderboard",
        "/leaderboard/video",
        "/leaderboard/video.json",
        "/status",
        "/status.json",
        "/status/history",
    }
)


def register_http_middleware(app: FastAPI, settings: Settings) -> None:
    """Wire all HTTP middlewares onto `app` in the right order.

    Starlette prepends each newly registered middleware, so the final
    registration is the outermost wrapper.
    """

    @app.middleware("http")
    async def markdown_negotiation_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Serve public pages as markdown when the client asks for markdown.

        Registered BEFORE GZipMiddleware on purpose. Starlette prepends, so the
        later registration is the outer wrapper: gzip ends up outside this, sees
        the markdown body, and compresses that. Register it after gzip instead
        and this middleware would be handed already-compressed bytes and would
        either corrupt them or have to decompress and recompress every page.

        `Vary` is set on the HTML variant too, not only the markdown one. That
        is the half that breaks caches when it is skipped: a CDN holding an HTML
        variant under a key that ignores Accept will serve it to the next agent
        asking for markdown, and whichever representation missed cache first
        wins for everybody.
        """
        response = await call_next(request)
        if request.method not in {"GET", "HEAD"}:
            return response
        if not is_public_page_path(request.url.path):
            return response
        if not response.headers.get("content-type", "").lower().startswith("text/html"):
            return response

        response.headers["vary"] = MARKDOWN_VARY
        if not prefers_markdown(request.headers.get("accept")):
            return response

        body = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body += bytes(chunk)

        markdown = html_to_markdown(body.decode("utf-8", "replace"))
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-type", "content-length", "vary"}
        }
        headers["vary"] = MARKDOWN_VARY
        return Response(
            content=markdown,
            status_code=response.status_code,
            headers=headers,
            media_type=MARKDOWN_CONTENT_TYPE,
        )

    app.add_middleware(GZipMiddleware, minimum_size=1_000, compresslevel=6)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=settings.max_request_body_bytes,
        max_in_flight_bytes=settings.max_in_flight_request_body_bytes,
        max_concurrent_bodies=settings.max_concurrent_request_bodies,
        read_timeout_seconds=settings.request_body_read_timeout_seconds,
    )
    ingress_rate_limits = InMemoryRateLimits(lock=threading.RLock())
    federation_settlement_tokens = tuple(
        parse_settlement_inbound_tokens(settings.federation_settlement_inbound_tokens)
    )
    # Authenticated buckets are intentionally process-local. Fleet-wide source
    # control belongs at the trusted front door; keeping counters out of
    # Spanner prevents the limiter from becoming a transactional hot row.
    setattr(
        app.state,
        AUTHENTICATED_LIMITER_STATE,
        InMemoryRateLimits(lock=threading.RLock()),
    )

    @app.middleware("http")
    async def internal_auth_before_body_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        denied = _internal_auth_before_body(request, settings)
        if denied is not None:
            return denied
        return await call_next(request)

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
        if upstream and len(upstream) <= 64 and all(c.isalnum() or c in "-_" for c in upstream):
            request_id = upstream
        else:
            request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers.setdefault("X-TrustedRouter-Request-Id", request_id)
        return response

    @app.middleware("http")
    async def canonical_public_host_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Keep crawlable pages on one host.

        ``www`` is only an alias. The status hostname intentionally serves a
        small status surface, but relative links in that page previously let
        crawlers discover the entire marketing site under the status host.
        Redirecting those escaped paths prevents duplicate pages and
        status-host 404s without changing the status API or static assets.
        """
        hostname = request_hostname(request)
        path = request.url.path
        if is_www_hostname(settings, hostname):
            return RedirectResponse(
                url=_apex_public_url(settings, request, hostname),
                status_code=308,
            )
        if is_status_hostname(settings, hostname) and not _status_host_path(path):
            return RedirectResponse(
                url=_apex_public_url(settings, request, hostname),
                status_code=308,
            )
        return await call_next(request)

    @app.middleware("http")
    async def public_pageview_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if is_status_hostname(
            settings, request_hostname(request)
        ) or _cookie_free_public_analytics_path(request.url.path):
            # Public operational analytics are not acquisition pages. Keeping
            # them cookie-free also lets Cloud CDN cache one shared response
            # instead of forcing an origin request for every anonymous viewer.
            request.state.acquisition_attribution = None
            attribution, attribution_changed = None, False
        else:
            attribution, attribution_changed = prepare_request_attribution(request, settings)
        start = time.perf_counter()
        response = await call_next(request)
        if attribution is not None and attribution_changed:
            set_attribution_cookie(response, attribution, settings)
        _log_public_page_view(request, response, latency_ms=(time.perf_counter() - start) * 1000)
        return response

    @app.middleware("http")
    async def oauth_key_exchange_cors_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Allow static browser apps to complete OAuth/PKCE key delegation.

        The authorization page is a top-level navigation and does not need
        CORS. The callback page does need to exchange a one-time code for a
        delegated key without sending the app's existing bearer key through a
        Lore-owned server. Restrict CORS to the unauthenticated code-exchange
        endpoint; inference and management APIs remain non-CORS surfaces.
        """
        if request.url.path in OAUTH_KEY_EXCHANGE_PATHS and request.method.upper() == "OPTIONS":
            return Response(status_code=204, headers=OAUTH_KEY_EXCHANGE_CORS_HEADERS)
        response = await call_next(request)
        if request.url.path in OAUTH_KEY_EXCHANGE_PATHS:
            for name, value in OAUTH_KEY_EXCHANGE_CORS_HEADERS.items():
                response.headers.setdefault(name, value)
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
        # Admission is process-local and performs no storage writes, so it can
        # remain active during a Spanner read-only cutover. This closes the old
        # maintenance-window bypass without changing the write-block response.
        limited = await _rate_limit_request(
            request,
            settings,
            ingress_rate_limits=ingress_rate_limits,
            federation_settlement_tokens=federation_settlement_tokens,
        )
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
        nonce = secrets.token_urlsafe(16)
        token = _CSP_NONCE.set(nonce)
        try:
            response = await call_next(request)
        finally:
            _CSP_NONCE.reset(token)
        response.headers.setdefault(
            "strict-transport-security",
            "max-age=63072000; includeSubDomains",
        )
        # Cheap, universal, and safe on every response including JSON.
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("referrer-policy", "strict-origin-when-cross-origin")
        # SAMEORIGIN, not DENY: /choose embeds /static/choose-app.html in a
        # same-origin iframe, and DENY would blank it. SAMEORIGIN still blocks
        # the clickjacking case, which is a THIRD party framing a page where
        # somebody is creating an API key.
        response.headers.setdefault("x-frame-options", "SAMEORIGIN")
        if response.headers.get("content-type", "").startswith("text/html"):
            # REPORT-ONLY on purpose. 14 templates carry inline <script> and 27
            # carry inline style=, so an enforcing policy would have to ship
            # with 'unsafe-inline' (which buys almost nothing) or with nonces
            # threaded through every template (which is the real fix and a
            # separate change). Report-only breaks nothing while the violation
            # list is gathered.
            #
            # It has NO report-uri, so nothing collects these automatically:
            # they appear in the browser console and nowhere else. That is a
            # deliberate first step, not a monitoring claim -- see the PR.
            # REPORT-ONLY first, deliberately. The nonces are already correct
            # (verified in a browser under enforcement on /, /choose, /docs and
            # /chat), but /console/* needs a session and the Adyen checkout page
            # loads a third-party SDK that injects its own iframes -- neither
            # was exercised. Report-only lets real traffic prove those paths
            # before a blocked script can break a money path. Flip this to
            # "content-security-policy" once the violation reports are clean.
            response.headers.setdefault(
                "content-security-policy-report-only",
                content_security_policy(nonce),
            )
        return response

    # Registered last, therefore outermost in Starlette. It never rejects or
    # admits a request itself: the source limiter still counts framing errors.
    # It only prevents early auth/rate/read-only/404 responses from leaving an
    # unread request body on a reusable HTTP/1 backend connection.
    app.add_middleware(UnreadRequestBodyCloseMiddleware)


#: Per-request CSP nonce. A ContextVar rather than a template global because
#: both Jinja environments are ``lru_cache``d and therefore shared across
#: requests -- a nonce stored on the env would be reused, which is exactly as
#: good as no nonce at all.
_CSP_NONCE: ContextVar[str] = ContextVar("csp_nonce", default="")


def current_csp_nonce() -> str:
    """The nonce for the request being rendered, or "" outside a request.

    Registered as the ``csp_nonce`` template global. Empty outside a request
    so template rendering in tests and scripts does not explode; an empty
    nonce attribute simply fails the policy rather than silently passing it.
    """
    return _CSP_NONCE.get()


#: The policy the site would have to satisfy before CSP can be enforced.
#: Derived from what the templates actually reference today (jsdelivr for a
#: couple of scripts, the trust and status subdomains, GitHub links), not from
#: a generic template -- a policy nobody measured is one that gets switched to
#: report-only forever.
#: Script origins the templates actually load today: our own static files,
#: four jsdelivr libraries, and Adyen's checkout SDK on the credits page.
CSP_SCRIPT_ORIGINS = (
    "https://cdn.jsdelivr.net",
    "https://checkoutshopper-live.cdn.adyen.com",
    "https://checkoutshopper-test.cdn.adyen.com",
)


def content_security_policy(nonce: str) -> str:
    """Build the policy for one request.

    ``script-src`` carries a nonce, which makes browsers IGNORE any
    ``'unsafe-inline'`` there -- that is the whole point, and why every inline
    <script> in the templates now carries ``nonce="{{ csp_nonce() }}"``.

    ``style-src`` keeps ``'unsafe-inline'`` and is not a mistake: a nonce
    cannot authorise an inline ``style="..."`` ATTRIBUTE, only a <style>
    block, and 27 templates use style attributes. Removing them is a real
    change with no security payoff next to script-src, so it is not bundled
    here. Said plainly rather than left looking like an oversight.
    """
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}' " + " ".join(CSP_SCRIPT_ORIGINS),
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            "img-src 'self' data: https:",
            "font-src 'self' data: https://cdn.jsdelivr.net",
            "connect-src 'self' https://api.trustedrouter.com "
            "https://trust.trustedrouter.com https://status.trustedrouter.com",
            "frame-src 'self' https://checkoutshopper-live.cdn.adyen.com "
            "https://checkoutshopper-test.cdn.adyen.com",
            "frame-ancestors 'self'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        )
    )


def _status_host_path(path: str) -> bool:
    return path in STATUS_HOST_EXACT_PATHS or path.startswith(STATUS_HOST_PATH_PREFIXES)


def _internal_auth_before_body(
    request: Request,
    settings: Settings,
) -> JSONResponse | None:
    """Authenticate split internal surfaces before FastAPI parses a body."""
    if settings.service_surface not in {"internal", "observer"}:
        return None
    path = request.url.path
    if path.startswith("/v1/internal/"):
        path = path.removeprefix("/v1")
    if not path.startswith("/internal/"):
        return None

    # Imports remain local so ordinary public/control processes never import
    # billing/federation route modules just to install generic middleware.
    from trusted_router.routes.internal._shared import require_internal_gateway
    from trusted_router.routes.internal.federation import (
        require_federation_credit_peer,
        require_federation_peer,
        require_federation_settlement_peer,
    )

    try:
        if path == "/internal/federation/resolve-key":
            require_federation_peer(request, settings)
        elif path == "/internal/federation/apply-usage":
            require_federation_settlement_peer(request, settings)
        elif path == "/internal/federation/credit-transfer":
            require_federation_credit_peer(request, settings)
        else:
            require_internal_gateway(request, settings)
    except StarletteHTTPException as exc:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(
                exc.detail,
                status_code=exc.status_code,
                headers=exc.headers,
            )
        return error_response(
            exc.status_code,
            str(exc.detail),
            ErrorType.HTTP_ERROR,
        )
    return None


def _cookie_free_public_analytics_path(path: str) -> bool:
    return path in COOKIE_FREE_PUBLIC_ANALYTICS_PATHS


def _apex_public_url(settings: Settings, request: Request, hostname: str) -> str:
    query = request.url.query
    suffix = f"?{query}" if query else ""
    domain = control_domain_for_hostname(settings, hostname)
    return f"https://{domain}{request.url.path}{suffix}"


async def _rate_limit_request(
    request: Request,
    settings: Settings,
    *,
    ingress_rate_limits: InMemoryRateLimits,
    federation_settlement_tokens: tuple[str, ...],
) -> JSONResponse | None:
    if not settings.rate_limit_enabled:
        return None
    path = request.url.path

    bearer = get_authorization_bearer(request)
    internal_token = request.headers.get("x-trustedrouter-internal-token")
    source = normalized_client_identity(request, settings)
    internal_path = path.startswith(("/internal/", "/v1/internal/"))
    trusted_internal = False
    if internal_path:
        trusted_credential = _trusted_internal_credential(
            request,
            settings,
            path=path,
            bearer=bearer,
            internal_token=internal_token,
            federation_settlement_tokens=federation_settlement_tokens,
        )
        if trusted_credential is not None:
            credential_kind, supplied = trusted_credential
            trusted_internal = True
            namespace = f"internal_{credential_kind}"
            subject = fingerprint_subject(f"{credential_kind}:{supplied}")
            limit = settings.rate_limit_internal_per_window
        else:
            namespace = "internal_ip"
            subject = fingerprint_subject(source)
            limit = settings.rate_limit_ip_per_window
    else:
        namespace = "ip"
        subject = fingerprint_subject(source)
        limit = settings.rate_limit_ip_per_window

    try:
        hit = ingress_rate_limits.hit(
            namespace=namespace,
            subject=subject,
            limit=limit,
            window_seconds=settings.rate_limit_window_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - explicit fail policy below
        log.warning(
            "rate_limit.ingress_local_error",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "namespace": namespace,
                "path": path,
                "error": type(exc).__name__,
            },
        )
        if trusted_internal:
            # The caller already presented the exact configured credential for
            # this internal route. Preserve internal-path availability if this
            # local backstop has an implementation failure.
            return None
        response = error_response(
            503,
            "Request admission is temporarily unavailable",
            ErrorType.SERVICE_UNAVAILABLE,
        )
        response.headers["Retry-After"] = "1"
        return response
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
    response.headers.update(rate_limit_headers(hit))
    if request_id:
        response.headers.setdefault("X-TrustedRouter-Request-Id", request_id)
    return response


def _trusted_internal_credential(
    request: Request,
    settings: Settings,
    *,
    path: str,
    bearer: str | None,
    internal_token: str | None,
    federation_settlement_tokens: tuple[str, ...],
) -> tuple[str, str] | None:
    """Match only the credential that the exact internal route accepts.

    This performs no storage or body reads. Caller-supplied values become a
    limiter subject only after a constant-time match to configured secrets, so
    rotating guesses cannot mint buckets. Federation credentials stay scoped
    to their distinct powers instead of gaining a generic internal allowance.
    """

    route_path = path[3:] if path.startswith("/v1/") else path
    if route_path == "/internal/federation/resolve-key":
        supplied = request.headers.get("x-trustedrouter-federation-token") or ""
        expected = settings.federation_peer_token
        if expected and constant_time_equal(supplied, expected):
            return "federation_peer", supplied
        return None
    if route_path == "/internal/federation/apply-usage":
        supplied = (
            request.headers.get("x-trustedrouter-federation-settlement-token") or ""
        )
        matched = False
        for expected in federation_settlement_tokens:
            matched |= constant_time_equal(supplied, expected)
        if matched:
            return "federation_settlement", supplied
        return None
    if route_path == "/internal/federation/credit-transfer":
        supplied = request.headers.get("x-trustedrouter-federation-credit-token") or ""
        expected = settings.federation_credit_inbound_token
        if expected and constant_time_equal(supplied, expected):
            return "federation_credit", supplied
        return None

    # Credential precedence mirrors require_internal_gateway exactly: bearer
    # first, then the dedicated header. Observer and billing tokens are
    # intentionally route-scoped and disjoint, and neither generic token
    # grants a higher allowance on the three federation routes above.
    supplied = bearer or internal_token or ""
    from trusted_router.routes.internal._shared import internal_service_credential

    kind, internal_expected = internal_service_credential(settings, route_path)
    if internal_expected and constant_time_equal(supplied, internal_expected):
        return kind, supplied
    return None


def _log_public_page_view(request: Request, response: Response, *, latency_ms: float) -> None:
    """Emit privacy-bounded public page analytics through the app logger.

    The Axiom integration subscribes to Python log records. We only log
    metadata needed for public-site analytics and deliberately avoid raw IPs,
    cookies, auth headers, full query strings, or user-agent strings.
    """
    if request.method.upper() != "GET":
        return
    path = request.url.path
    if not _is_public_html_response(path, response):
        return
    slug = path.removeprefix("/blog/") if path.startswith("/blog/") else ""
    extra: dict[str, object] = {
        "event": "public.page_view",
        "request_id": getattr(request.state, "request_id", None),
        "page_kind": _page_kind(path),
        "path": path,
        "blog_slug": slug or None,
        "status_code": response.status_code,
        "latency_ms": round(latency_ms, 2),
        "referer_host": _referer_host(request),
        "user_agent_family": _user_agent_family(request.headers.get("user-agent", "")),
        "automated_request": acquisition_request_is_automated(request),
        "measurement_tier": "server_request",
    }
    extra.update(_utm_fields(request))
    extra.update(pageview_attribution_fields(request))
    log.info("public.page_view", extra=extra)


def _is_public_html_response(path: str, response: Response) -> bool:
    excluded = (
        "/auth",
        "/console",
        "/internal",
        "/v1",
        "/static",
        "/health",
        "/ready",
        "/openapi",
    )
    if path.startswith(excluded) or path.endswith("_oauth_callback"):
        return False
    return "text/html" in response.headers.get("content-type", "").lower()


def _page_kind(path: str) -> str:
    if path == "/":
        return "homepage"
    if path == "/blog":
        return "blog_index"
    if path.startswith("/blog/"):
        return "blog_post"
    if path.startswith("/docs"):
        return "docs"
    if path.startswith("/models/"):
        return "model"
    if path.startswith("/providers/"):
        return "provider"
    return "marketing"


def _referer_host(request: Request) -> str | None:
    referer = request.headers.get("referer", "").strip()
    if not referer:
        return None
    try:
        return urlsplit(referer).netloc[:128] or None
    except ValueError:
        return None


def _utm_fields(request: Request) -> dict[str, str]:
    values = parse_qs(request.url.query, keep_blank_values=False)
    fields: dict[str, str] = {}
    for key in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"):
        value = values.get(key, [""])[0].strip()
        if value:
            fields[key] = value[:128]
    return fields


def _user_agent_family(user_agent: str) -> str | None:
    normalized = user_agent.lower()
    if not normalized:
        return None
    if "googlebot" in normalized:
        return "googlebot"
    if "bingbot" in normalized:
        return "bingbot"
    if "claudebot" in normalized or "anthropic-ai" in normalized:
        return "claude"
    if "gptbot" in normalized or "chatgpt-user" in normalized or "oai-searchbot" in normalized:
        return "openai"
    if "firefox" in normalized:
        return "firefox"
    if "chrome" in normalized or "chromium" in normalized:
        return "chrome"
    if "safari" in normalized:
        return "safari"
    if "bot" in normalized or "crawler" in normalized or "spider" in normalized:
        return "bot"
    return "other"
