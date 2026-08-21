from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import ManagementPrincipal, SettingsDep, principal_from_request
from trusted_router.config import Settings
from trusted_router.domains import request_control_origin
from trusted_router.errors import api_error
from trusted_router.money import (
    dollars_to_microdollars,
    format_money_display,
    microdollars_to_decimal,
)
from trusted_router.request_limits import (
    fingerprint_subject,
    normalized_client_identity,
)
from trusted_router.routes.helpers import json_body
from trusted_router.schemas import CheckoutRequest
from trusted_router.serialization import key_shape
from trusted_router.services.stripe_billing import create_checkout_session
from trusted_router.storage import STORE, OAuthAuthorizationCode
from trusted_router.storage_oauth_codes import (
    OAuthCodeMethodMismatch,
    OAuthCodeVerifierMismatch,
    OAuthCodeVerifierNotAscii,
    OAuthCodeVerifierRequired,
    OAuthWorkspaceBillingPaused,
    OAuthWorkspaceUnavailable,
)
from trusted_router.storage_rate_limits import InMemoryRateLimits
from trusted_router.typed_balance import live_credit_summary
from trusted_router.types import ErrorType
from trusted_router.views import render_template

PKCE_METHODS = {"S256", "plain"}
RESET_INTERVALS = {"daily", "weekly", "monthly"}
OAUTH_FUNDING_AMOUNTS = {"5", "20", "100"}
OAUTH_DEFAULT_FUNDING_AMOUNT = "20"
OAUTH_DEFAULT_KEY_LIMIT = "20"
NATIVE_CALLBACK_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]{1,63}$")
UNSAFE_CALLBACK_SCHEMES = {"data", "file", "javascript"}
OAUTH_AUTHORIZATION_FIELDS = (
    "callback_url",
    "code_challenge",
    "code_challenge_method",
    "key_label",
    "limit",
    "usage_limit_type",
    "expires_at",
    "spawn_agent",
    "spawn_cloud",
)
OAUTH_CODE_PATTERN = re.compile(r"^auth_code-[A-Za-z0-9_-]{43}$")
_OAUTH_EXCHANGE_SLOTS = threading.BoundedSemaphore(4)
_OAUTH_EXCHANGE_RATE_LIMITS = InMemoryRateLimits(
    lock=threading.RLock(),
    max_buckets=10_000,
)
_OAUTH_EXCHANGE_GLOBAL_RATE_LIMITS = InMemoryRateLimits(
    lock=threading.RLock(),
    max_buckets=1,
)
_OAUTH_EXCHANGE_PER_SOURCE_LIMIT = 120
# One source can consume at most one eighth of this process-local backstop.
# That keeps a bounded aggregate ceiling without letting a single attacker
# reserve every exchange slot for the rest of the minute.
_OAUTH_EXCHANGE_GLOBAL_LIMIT = 960


def register_oauth_key_routes(router: APIRouter) -> None:
    @router.get("/auth")
    async def oauth_authorize_page(request: Request, settings: SettingsDep) -> Response:
        params = _oauth_params_from_query(request)
        try:
            principal = principal_from_request(request, settings)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return HTMLResponse(_signin_html(request, settings, params), status_code=401)
        if not principal.is_management:
            raise api_error(403, "Only management users can delegate credits", ErrorType.FORBIDDEN)
        return HTMLResponse(
            _consent_html(
                params,
                workspace_name=principal.workspace.name,
                workspace_id=principal.workspace.id,
            )
        )

    @router.post("/auth/fund")
    async def oauth_authorize_fund(request: Request, settings: SettingsDep) -> Response:
        form = dict(await request.form())
        params = _authorization_params(form)
        _validate_code_request(params)
        try:
            principal = principal_from_request(request, settings)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            raise api_error(401, "Sign in is required", ErrorType.UNAUTHORIZED) from exc
        if not principal.is_management:
            raise api_error(403, "Only management users can fund credits", ErrorType.FORBIDDEN)

        amount = str(form.get("fund_amount") or "")
        if amount not in OAUTH_FUNDING_AMOUNTS:
            raise api_error(400, "fund_amount must be 5, 20, or 100", ErrorType.BAD_REQUEST)
        origin = request_control_origin(request, settings)
        try:
            body = CheckoutRequest(
                amount=amount,
                workspace_id=principal.workspace.id,
                payment_method="card",
                success_url=_authorization_return_url(origin, params, checkout="success"),
                cancel_url=_authorization_return_url(origin, params, checkout="cancel"),
            )
        except ValidationError as exc:
            raise api_error(400, "Invalid checkout request", ErrorType.BAD_REQUEST) from exc
        credit = STORE.get_credit_account(principal.workspace.id)
        data = create_checkout_session(
            body=body,
            workspace_id=principal.workspace.id,
            initiating_user_id=_principal_user_id(principal),
            customer_email=(
                principal.user.email
                if principal.user and principal.user.email and "@" in principal.user.email
                else None
            ),
            customer_id=credit.stripe_customer_id if credit else None,
            settings=settings,
        )
        if str(data.get("mode") or "").startswith("mock"):
            return RedirectResponse(
                url=_authorization_return_url(origin, params, checkout="mock"),
                status_code=303,
            )
        return RedirectResponse(url=str(data["url"]), status_code=303)

    @router.post("/auth/approve")
    async def oauth_authorize_approve(request: Request, settings: SettingsDep) -> Response:
        params = _oauth_params_from_form(await request.form())
        try:
            principal = principal_from_request(request, settings)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            raise api_error(401, "Sign in is required", ErrorType.UNAUTHORIZED) from exc
        if not principal.is_management:
            raise api_error(403, "Only management users can delegate credits", ErrorType.FORBIDDEN)
        raw_code, code = _create_code(params, principal, settings)
        return RedirectResponse(
            url=_callback_with_code(code.callback_url, raw_code, code.user_id), status_code=302
        )

    @router.post("/auth/keys/code")
    async def auth_keys_code(
        request: Request,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        params = await _oauth_params_from_json(request)
        raw_code, code = _create_code(params, principal, settings)
        return JSONResponse(
            {
                "data": {
                    "id": raw_code,
                    "app_id": code.app_id,
                    "created_at": code.created_at,
                }
            }
        )

    @router.post("/auth/keys")
    async def auth_keys(request: Request, settings: SettingsDep) -> JSONResponse:
        body = await json_body(request)
        raw_value = body.get("code")
        raw_code = raw_value if isinstance(raw_value, str) else ""
        if not raw_code:
            raise api_error(400, "code is required", ErrorType.BAD_REQUEST)
        if not OAUTH_CODE_PATTERN.fullmatch(raw_code):
            raise api_error(
                403,
                "Invalid or expired authorization code",
                ErrorType.FORBIDDEN,
            )
        if not _OAUTH_EXCHANGE_SLOTS.acquire(blocking=False):
            raise api_error(
                429,
                "Authorization-code exchange is busy",
                ErrorType.RATE_LIMITED,
                headers={"Retry-After": "1"},
            )
        try:
            if settings.rate_limit_enabled:
                source = fingerprint_subject(
                    normalized_client_identity(request, settings)
                )
                hit = _OAUTH_EXCHANGE_RATE_LIMITS.hit(
                    namespace="oauth_code_exchange_source",
                    subject=source,
                    limit=_OAUTH_EXCHANGE_PER_SOURCE_LIMIT,
                    window_seconds=60,
                )
                if not hit.allowed:
                    raise api_error(
                        429,
                        "Authorization-code exchange rate exceeded",
                        ErrorType.RATE_LIMITED,
                        headers={"Retry-After": str(hit.retry_after_seconds)},
                    )
                global_hit = _OAUTH_EXCHANGE_GLOBAL_RATE_LIMITS.hit(
                    namespace="oauth_code_exchange_global",
                    subject="all",
                    limit=_OAUTH_EXCHANGE_GLOBAL_LIMIT,
                    window_seconds=60,
                )
                if not global_hit.allowed:
                    raise api_error(
                        429,
                        "Authorization-code exchange is busy",
                        ErrorType.RATE_LIMITED,
                        headers={"Retry-After": str(global_hit.retry_after_seconds)},
                    )
            payload = await run_in_threadpool(_exchange_oauth_code, raw_code, body)
        finally:
            _OAUTH_EXCHANGE_SLOTS.release()
        return JSONResponse(payload)


def _exchange_oauth_code(raw_code: str, body: dict[str, Any]) -> dict[str, Any]:
    """Run one atomic Store exchange outside the ASGI event loop."""
    verifier_value = body.get("code_verifier")
    method_value = body.get("code_challenge_method")
    code_verifier = str(verifier_value or "") or None
    code_challenge_method = str(method_value or "") or None
    try:
        exchange = STORE.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=code_verifier,
            code_challenge_method=code_challenge_method,
        )
    except OAuthCodeMethodMismatch as exc:
        raise api_error(
            400,
            "code_challenge_method does not match authorization code",
            ErrorType.BAD_REQUEST,
        ) from exc
    except OAuthCodeVerifierRequired as exc:
        raise api_error(400, "code_verifier is required", ErrorType.BAD_REQUEST) from exc
    except OAuthCodeVerifierNotAscii as exc:
        raise api_error(400, "code_verifier must be ASCII", ErrorType.BAD_REQUEST) from exc
    except OAuthCodeVerifierMismatch as exc:
        raise api_error(403, "Invalid code_verifier", ErrorType.FORBIDDEN) from exc
    except OAuthWorkspaceBillingPaused as exc:
        raise api_error(
            503,
            "Workspace billing is paused",
            ErrorType.SERVICE_UNAVAILABLE,
            headers={"Retry-After": "30"},
        ) from exc
    except OAuthWorkspaceUnavailable as exc:
        raise api_error(
            503,
            "Authorization workspace is unavailable",
            ErrorType.SERVICE_UNAVAILABLE,
            headers={"Retry-After": "30"},
        ) from exc
    if exchange is None:
        raise api_error(403, "Invalid or expired authorization code", ErrorType.FORBIDDEN)
    return {
        "key": exchange.raw_key,
        "user_id": exchange.authorization_code.user_id,
        "identity": _identity_payload(exchange.user),
        "data": key_shape(exchange.api_key),
    }


def _create_code(
    params: dict[str, Any], principal: Any, settings: Settings
) -> tuple[str, OAuthAuthorizationCode]:
    callback_url = _validate_callback_url(str(params.get("callback_url") or ""))
    code_challenge = _optional_str(params.get("code_challenge"))
    code_challenge_method = _pkce_method(
        params.get("code_challenge_method"), has_challenge=bool(code_challenge)
    )
    limit_microdollars = _limit_microdollars(params.get("limit"))
    limit_reset = _limit_reset(params.get("usage_limit_type"))
    expires_at = _expires_at(params.get("expires_at"))
    key_label = _key_label(params.get("key_label"), callback_url)
    user_id = _principal_user_id(principal)
    return STORE.create_oauth_authorization_code(
        workspace_id=principal.workspace.id,
        user_id=user_id,
        callback_url=callback_url,
        key_label=key_label,
        ttl_seconds=settings.oauth_authorization_code_ttl_seconds,
        app_id=_app_id(callback_url),
        limit_microdollars=limit_microdollars,
        limit_reset=limit_reset,
        expires_at=expires_at,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        spawn_agent=_optional_str(params.get("spawn_agent")),
        spawn_cloud=_optional_str(params.get("spawn_cloud")),
    )


async def _oauth_params_from_json(request: Request) -> dict[str, Any]:
    body = await json_body(request)
    _validate_code_request(body)
    return body


def _oauth_params_from_query(request: Request) -> dict[str, Any]:
    params = dict(request.query_params)
    _validate_code_request(params)
    return params


def _oauth_params_from_form(form: Any) -> dict[str, Any]:
    params = dict(form)
    _validate_code_request(params)
    return params


def _validate_code_request(params: dict[str, Any]) -> None:
    callback_url = _validate_callback_url(str(params.get("callback_url") or ""))
    method = _pkce_method(
        params.get("code_challenge_method"), has_challenge=bool(params.get("code_challenge"))
    )
    if _is_native_callback(callback_url) and method != "S256":
        raise api_error(
            400,
            "native callback_url requires PKCE S256",
            ErrorType.BAD_REQUEST,
        )
    _limit_microdollars(params.get("limit"))
    _limit_reset(params.get("usage_limit_type"))
    _expires_at(params.get("expires_at"))
    _key_label(params.get("key_label"), str(params.get("callback_url") or ""))


def _validate_callback_url(callback_url: str) -> str:
    if not callback_url:
        raise api_error(400, "callback_url is required", ErrorType.BAD_REQUEST)
    parsed = urlsplit(callback_url)
    if not parsed.hostname:
        raise api_error(400, "callback_url must be an https URL", ErrorType.BAD_REQUEST)
    localhost_callback = parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if parsed.username or parsed.password:
        raise api_error(400, "callback_url cannot contain credentials", ErrorType.BAD_REQUEST)
    native_callback = _is_native_callback(callback_url)
    if parsed.scheme != "https" and not localhost_callback and not native_callback:
        raise api_error(400, "callback_url must be an https URL", ErrorType.BAD_REQUEST)
    try:
        port = parsed.port
    except ValueError as exc:
        raise api_error(400, "callback_url has an invalid port", ErrorType.BAD_REQUEST) from exc
    if native_callback:
        if port is not None:
            raise api_error(400, "native callback_url cannot contain a port", ErrorType.BAD_REQUEST)
        return callback_url
    if localhost_callback and port == 3000:
        return callback_url
    if port not in {None, 443, 3000}:
        raise api_error(400, "callback_url port must be 443 or 3000", ErrorType.BAD_REQUEST)
    return callback_url


def _is_native_callback(callback_url: str) -> bool:
    scheme = urlsplit(callback_url).scheme.lower()
    return bool(
        scheme
        and scheme not in {"http", "https", *UNSAFE_CALLBACK_SCHEMES}
        and NATIVE_CALLBACK_SCHEME.fullmatch(scheme)
    )


def _pkce_method(raw: Any, *, has_challenge: bool) -> str | None:
    if raw in {None, ""}:
        return "S256" if has_challenge else None
    method = str(raw)
    if method not in PKCE_METHODS:
        raise api_error(400, "code_challenge_method must be S256 or plain", ErrorType.BAD_REQUEST)
    if method and not has_challenge:
        raise api_error(
            400,
            "code_challenge is required when code_challenge_method is set",
            ErrorType.BAD_REQUEST,
        )
    return method


def _limit_microdollars(raw: Any) -> int | None:
    if raw in {None, ""}:
        return None
    try:
        value = dollars_to_microdollars(raw)
    except ValueError as exc:
        raise api_error(400, "limit must be a dollar amount", ErrorType.BAD_REQUEST) from exc
    if value < 0:
        raise api_error(400, "limit must be non-negative", ErrorType.BAD_REQUEST)
    return value


def _limit_reset(raw: Any) -> str | None:
    if raw in {None, ""}:
        return None
    value = str(raw)
    if value not in RESET_INTERVALS:
        raise api_error(
            400, "usage_limit_type must be daily, weekly, or monthly", ErrorType.BAD_REQUEST
        )
    return value


def _expires_at(raw: Any) -> str | None:
    if raw in {None, ""}:
        return None
    value = str(raw)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise api_error(
            400, "expires_at must be an ISO 8601 timestamp", ErrorType.BAD_REQUEST
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    if parsed <= dt.datetime.now(dt.UTC):
        raise api_error(400, "expires_at must be in the future", ErrorType.BAD_REQUEST)
    return value


def _key_label(raw: Any, callback_url: str) -> str:
    value = str(raw or "").strip()
    if not value:
        host = urlsplit(callback_url).hostname or "Delegated app"
        value = f"{host} delegated key"
    if len(value) > 100:
        raise api_error(400, "key_label must be at most 100 characters", ErrorType.BAD_REQUEST)
    return value


def _principal_user_id(principal: Any) -> str | None:
    if principal.user is not None:
        return principal.user.id
    if principal.api_key is not None and principal.api_key.creator_user_id:
        return principal.api_key.creator_user_id
    return principal.workspace.owner_user_id


def _app_id(callback_url: str) -> int:
    digest = hashlib.sha256(callback_url.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _callback_with_code(callback_url: str, raw_code: str, user_id: str | None) -> str:
    parsed = urlsplit(callback_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("code", raw_code))
    if user_id:
        query.append(("user_id", user_id))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _optional_str(raw: Any) -> str | None:
    if raw in {None, ""}:
        return None
    return str(raw)


def _identity_payload(user: Any) -> dict[str, Any] | None:
    """The signed-in user's identity returned with the delegated key. None
    when the approving user can't be resolved (e.g. legacy code rows)."""
    if user is None:
        return None
    return {
        "sub": user.id,
        "email": user.email,
        "email_verified": user.email_verified,
        "phone_verified": user.phone_verified,
        "identity_verified": user.identity_verified,
        "wallet_address": user.wallet_address,
    }


def _signin_html(request: Request, settings: Settings, params: dict[str, Any]) -> str:
    next_path = str(request.url.path) + ("?" + str(request.url.query) if request.url.query else "")
    return render_template(
        "auth/oauth_signin.html",
        page_title="Authorize TrustedRouter",
        app_name=_key_label(params.get("key_label"), str(params.get("callback_url") or "")),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        # Use the same urllib.parse.quote shape the test asserts against —
        # Jinja's `urlencode` filter has different `safe=` defaults.
        next_path_encoded=urlencode({"next": next_path})[len("next=") :],
    )


def _consent_html(
    params: dict[str, Any],
    *,
    workspace_name: str,
    workspace_id: str,
) -> str:
    callback_url = _validate_callback_url(str(params.get("callback_url") or ""))
    key_label = _key_label(params.get("key_label"), callback_url)
    limit = _limit_microdollars(params.get("limit"))
    effective_limit = OAUTH_DEFAULT_KEY_LIMIT if limit is None else microdollars_to_decimal(limit)
    reset = _limit_reset(params.get("usage_limit_type")) or ""
    summary = live_credit_summary(workspace_id)
    available = summary["available"] if summary else 0
    hidden_fields = _hidden_authorization_fields(params, exclude={"limit", "usage_limit_type"})
    funding_hidden_fields = _hidden_authorization_fields(
        {
            **params,
            "limit": effective_limit,
            "usage_limit_type": reset,
        }
    )
    checkout_status = str(params.get("checkout") or "")
    if checkout_status not in {"success", "cancel", "mock"}:
        checkout_status = ""
    return render_template(
        "auth/oauth_consent.html",
        page_title=f"Authorize {key_label}",
        key_label=key_label,
        callback_host=urlsplit(callback_url).hostname or callback_url,
        cancel_url=_callback_with_error(callback_url, "access_denied"),
        workspace_name=workspace_name,
        available_display=format_money_display(available),
        needs_funding=available <= 0,
        checkout_status=checkout_status,
        effective_limit=effective_limit,
        effective_reset=reset,
        hidden_fields=hidden_fields,
        funding_hidden_fields=funding_hidden_fields,
        funding_amounts=("5", "20", "100"),
        default_funding_amount=OAUTH_DEFAULT_FUNDING_AMOUNT,
    )


def _authorization_params(values: dict[str, Any]) -> dict[str, Any]:
    return {
        name: values[name]
        for name in OAUTH_AUTHORIZATION_FIELDS
        if name in values and values[name] not in {None, ""}
    }


def _hidden_authorization_fields(
    values: dict[str, Any],
    *,
    exclude: set[str] | None = None,
) -> list[tuple[str, str]]:
    excluded = exclude or set()
    return [
        (name, str(values[name]))
        for name in OAUTH_AUTHORIZATION_FIELDS
        if name not in excluded and name in values and values[name] not in {None, ""}
    ]


def _authorization_return_url(origin: str, params: dict[str, Any], *, checkout: str) -> str:
    query = urlencode([*params.items(), ("checkout", checkout)])
    return f"{origin}/auth?{query}"


def _callback_with_error(callback_url: str, error: str) -> str:
    parsed = urlsplit(callback_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("error", error))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
