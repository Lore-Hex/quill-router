"""Request-rate-limit identity and authenticated defense-in-depth controls.

Deployed source identity has one contract: every trusted front door overwrites
``X-TrustedRouter-Client-IP`` with one normalized client address and the origin
is unreachable except through those front doors. The application deliberately
does not consult forwarding headers or the socket peer in deployed
environments. A missing or malformed trusted header collapses into one
conservative bucket so a front-door configuration mistake cannot turn caller
input into identities.

Fleet-wide coarse limiting belongs at the front door (Cloud Armor or the cloud
equivalent).  These application counters are bounded and process-local
defense-in-depth; their allowance is per instance, not a distributed quota.
"""

from __future__ import annotations

import logging
from hashlib import sha256
from ipaddress import ip_address

from fastapi import Request

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.storage_models import RateLimitHit
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)

TRUSTED_CLIENT_IP_HEADER = "x-trustedrouter-client-ip"
UNTRUSTED_LOAD_BALANCER_SUBJECT = "untrusted_lb"
AUTHENTICATED_LIMITER_STATE = "authenticated_rate_limits"
_AUTHENTICATED_LIMIT_APPLIED_STATE = "authenticated_rate_limit_applied"


def normalized_client_identity(request: Request, settings: Settings) -> str:
    """Return the only identity that an ingress limiter may use.

    Every deployed environment trusts the dedicated LB-overwritten header only.
    Local/test use the ASGI peer so TestClient and direct developer servers work
    without load balancer setup, while still ignoring caller-supplied forwarding
    headers.
    """

    if settings.environment.casefold() not in {"local", "test"}:
        # The edge contract is exactly one overwritten header. Treat duplicates
        # as a broken trust boundary instead of accepting whichever value the
        # framework happens to return first.
        values = request.headers.getlist(TRUSTED_CLIENT_IP_HEADER)
        if len(values) != 1:
            return UNTRUSTED_LOAD_BALANCER_SUBJECT
        raw = values[0].strip()
        if not raw:
            return UNTRUSTED_LOAD_BALANCER_SUBJECT
        try:
            parsed = ip_address(raw)
        except ValueError:
            return UNTRUSTED_LOAD_BALANCER_SUBJECT
        # Zone identifiers are meaningful only on the receiver's local link;
        # they are not part of an Internet client identity and would create
        # attacker-variable subjects if an edge ever forwarded one.
        if getattr(parsed, "scope_id", None) is not None:
            return UNTRUSTED_LOAD_BALANCER_SUBJECT
        return parsed.compressed

    peer = request.client.host.strip() if request.client and request.client.host else "unknown"
    try:
        return ip_address(peer).compressed
    except ValueError:
        # TestClient uses the stable literal ``testclient`` rather than an IP.
        return peer or "unknown"


def fingerprint_subject(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def rate_limit_headers(hit: RateLimitHit) -> dict[str, str]:
    return {
        "Retry-After": str(hit.retry_after_seconds),
        "X-RateLimit-Limit": str(hit.limit),
        "X-RateLimit-Remaining": str(hit.remaining),
        "X-RateLimit-Reset": hit.reset_at,
    }


def enforce_authenticated_rate_limit(
    request: Request,
    settings: Settings,
    *,
    credential_kind: str,
    stable_subject: str,
) -> None:
    """Apply one local credential bucket after normal authentication succeeds.

    Callers provide only a store-validated, stable identifier (never raw
    unverified credential material). The request-scoped marker prevents direct
    callers and overlapping FastAPI dependencies from counting one request
    twice. If this defense-in-depth limiter itself breaks, the already-applied
    source ingress guard remains in force and valid authentication semantics
    fail open.
    """

    if not settings.rate_limit_enabled:
        return
    if getattr(request.state, _AUTHENTICATED_LIMIT_APPLIED_STATE, False):
        return
    limiter = getattr(request.app.state, AUTHENTICATED_LIMITER_STATE, None)
    if limiter is None:
        # Direct unit calls to principal_from_request do not construct the app
        # middleware. Authentication must retain its standalone semantics.
        return

    setattr(request.state, _AUTHENTICATED_LIMIT_APPLIED_STATE, True)
    namespace = f"authenticated_{credential_kind}"
    try:
        hit = limiter.hit(
            namespace=namespace,
            subject=fingerprint_subject(f"{credential_kind}:{stable_subject}"),
            limit=settings.rate_limit_key_per_window,
            window_seconds=settings.rate_limit_window_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - defense-in-depth must not break valid auth
        log.warning(
            "rate_limit.authenticated_local_error",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "namespace": namespace,
                "path": request.url.path,
                "error": type(exc).__name__,
            },
        )
        return
    if hit.allowed:
        return
    log.info(
        "rate_limit.exceeded",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "namespace": namespace,
            "path": request.url.path,
            "limit": hit.limit,
            "retry_after_seconds": hit.retry_after_seconds,
        },
    )
    raise api_error(
        429,
        "Rate limit exceeded",
        ErrorType.RATE_LIMITED,
        headers=rate_limit_headers(hit),
    )
