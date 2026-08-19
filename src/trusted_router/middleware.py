"""HTTP middleware shared across the FastAPI app.

HTTP middleware registers in order from outermost to innermost:
  1. request_id  — mints/accepts a per-request id, echoes in response
                   header, makes it available as request.state.request_id.
  2. canonical_public_host — keeps marketing URLs off www/status aliases.
  3. public_pageview — captures signed first-party attribution and emits
                       metadata-only public pageview events.
  4. rate_limit  — enforces process-local limits for anonymous safe reads and
                   internal traffic, plus shared per-(key|ip) limits for other
                   traffic; logs structured 429s with the request_id from (1).
  5. security_headers — sets HSTS so browsers remember to skip http://
                        on subsequent visits.

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
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from trusted_router.acquisition import (
    acquisition_request_is_automated,
    pageview_attribution_fields,
    prepare_request_attribution,
    set_attribution_cookie,
)
from trusted_router.auth import get_authorization_bearer
from trusted_router.config import Settings
from trusted_router.domains import (
    control_domain_for_hostname,
    is_status_hostname,
    is_www_hostname,
    request_hostname,
)
from trusted_router.errors import error_response
from trusted_router.security import constant_time_equal
from trusted_router.storage import STORE
from trusted_router.storage_models import RateLimitHit
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

    Starlette wraps middleware in reverse-add order: the FIRST one
    registered runs first on the way in (outermost wrap). We want
    request_id to mint the id before pageview/rate-limit logs use it,
    so request_id is registered first.
    """

    app.add_middleware(GZipMiddleware, minimum_size=1_000, compresslevel=6)
    public_read_rate_limits = InMemoryRateLimits(lock=threading.RLock())

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
        limited = await _rate_limit_request(
            request,
            settings,
            public_read_rate_limits=public_read_rate_limits,
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
            response.headers.setdefault(
                "content-security-policy",
                content_security_policy(nonce),
            )
        return response


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
    public_read_rate_limits: InMemoryRateLimits,
) -> JSONResponse | None:
    if not settings.rate_limit_enabled:
        return None
    path = request.url.path
    if path in {"/health", "/v1/health", "/ready", "/v1/ready"} or path.startswith(
        ("/docs", "/openapi.json")
    ):
        return None

    bearer = get_authorization_bearer(request)
    internal_token = request.headers.get("x-trustedrouter-internal-token")
    user = request.headers.get("x-trustedrouter-user")
    ip = _client_ip(request)
    # Public catalog and marketing reads are cacheable. A durable
    # read-modify-write counter here turns one crawler into a single-row
    # Spanner hotspot before the application can return the page. Use a
    # process-local guard for safe anonymous reads; authenticated and
    # state-changing requests remain on the shared application limiter.
    public_read = (
        request.method.upper() in {"GET", "HEAD", "OPTIONS"}
        and not bearer
        and not internal_token
        and not user
    )
    local_only_surface = settings.service_surface in {"public", "actions", "observer"}
    hit_rate_limit: Callable[..., RateLimitHit]
    if local_only_surface:
        # Anonymous and observer processes must never let attacker-controlled
        # methods or credential-shaped headers turn an edge request into a
        # durable Store write. Fleet-wide enforcement belongs at the trusted
        # load balancer; this bounded process-local bucket is defense in depth.
        namespace = "surface_ip"
        subject = _fingerprint(ip)
        limit = settings.rate_limit_ip_per_window
        hit_rate_limit = public_read_rate_limits.hit
        durable = False
    elif public_read:
        namespace = "public_ip"
        subject = _fingerprint(ip)
        limit = settings.rate_limit_ip_per_window
        hit_rate_limit = public_read_rate_limits.hit
        durable = False
    elif path.startswith(("/internal/", "/v1/internal/")):
        namespace = "internal"
        # Subject cardinality must be BOUNDED against unauthenticated input:
        # bucketing by the raw supplied credential would let an attacker mint
        # one fresh in-memory bucket per guessed token (bypassing the
        # per-subject limit and growing the process map without cap). Only a
        # credential that matches the configured internal secret earns the
        # shared fleet bucket; everything else counts against the caller's IP,
        # the same bounded identity the anonymous namespace uses.
        # Credential precedence MUST mirror require_internal_gateway (bearer
        # first, then header): a valid bearer plus a stale header must land in
        # the fleet bucket exactly like it authenticates at the route, or
        # legitimate NAT'd fleet traffic gets throttled per-IP.
        supplied = bearer or internal_token or ""
        if settings.internal_gateway_token and constant_time_equal(
            supplied, settings.internal_gateway_token
        ):
            subject = _fingerprint(supplied)
        else:
            subject = _fingerprint(ip)
        limit = settings.rate_limit_internal_per_window
        # Authenticated fleet-internal calls share one token. A globally
        # consistent Spanner counter serialized every billing call on one row's
        # write lock (issue #399; prod lock stats 2026-08-01). A per-instance
        # bucket keeps the backstop off the money path; fleet capacity is limit
        # times the number of instances.
        hit_rate_limit = public_read_rate_limits.hit
        durable = False
    elif bearer:
        namespace = "key"
        subject = _fingerprint(bearer)
        limit = settings.rate_limit_key_per_window
        hit_rate_limit = STORE.hit_rate_limit
        durable = True
    else:
        namespace = "ip"
        subject = _fingerprint(user or ip)
        limit = settings.rate_limit_ip_per_window
        hit_rate_limit = STORE.hit_rate_limit
        durable = True

    try:
        if durable:
            hit = await run_in_threadpool(
                hit_rate_limit,
                namespace=namespace,
                subject=subject,
                limit=limit,
                window_seconds=settings.rate_limit_window_seconds,
            )
        else:
            hit = hit_rate_limit(
                namespace=namespace,
                subject=subject,
                limit=limit,
                window_seconds=settings.rate_limit_window_seconds,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort guard, must not 500
        # Rate limiting is a best-effort guard, not core request logic. The
        # Spanner read-modify-write on the (namespace#subject#bucket) counter
        # ABORTS under hot-row contention — e.g. a bot bursting junk GETs from
        # one IP all increment the same row, deadlocking the transaction
        # ("Aborted: Deadlock with higher priority transaction", observed
        # 2026-06-08 on scanner traffic). Never crash a request because the
        # limiter is contended or unavailable: fail OPEN (allow) and log.
        log.warning(
            "rate_limit.store_error",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "namespace": namespace,
                "path": path,
                "error": type(exc).__name__,
            },
        )
        return None
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
