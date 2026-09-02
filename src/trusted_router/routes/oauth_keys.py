from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from trusted_router.auth import (
    ManagementPrincipal,
    Principal,
    SettingsDep,
    principal_from_request,
)
from trusted_router.config import Settings
from trusted_router.domains import request_control_origin
from trusted_router.errors import api_error, assert_workspace_billing_active
from trusted_router.money import (
    dollars_to_microdollars,
    format_money_display,
    microdollars_to_decimal,
)
from trusted_router.routes.helpers import enforce_rate_limit, json_body
from trusted_router.schemas import CheckoutRequest
from trusted_router.scopes import DEFAULT_DELEGATED_SCOPES, KNOWN_SCOPES
from trusted_router.serialization import key_shape
from trusted_router.services.stripe_billing import create_checkout_session
from trusted_router.storage import STORE, ConsentRequest, OAuthApp, OAuthAuthorizationCode
from trusted_router.typed_balance import live_credit_summary
from trusted_router.types import ErrorType
from trusted_router.verification import identity_payload
from trusted_router.views import render_template

PKCE_METHODS = {"S256", "plain"}
OAUTH_CONFORMANT_PKCE_METHODS = {"S256"}
OAUTH_AUTHORIZATION_ENDPOINT_PATH = "/auth"
OAUTH_KEY_EXCHANGE_ENDPOINT_PATH = "/auth/keys"
RESET_INTERVALS = {"daily", "weekly", "monthly"}
OAUTH_FUNDING_AMOUNTS = {"5", "20", "100"}
OAUTH_DEFAULT_FUNDING_AMOUNT = "20"
OAUTH_DEFAULT_KEY_LIMIT = "20"
OAUTH_TOKEN_RATE_LIMIT = 30
CODE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
NATIVE_CALLBACK_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]{1,63}$")
UNSAFE_CALLBACK_SCHEMES = {"data", "file", "javascript"}
OAUTH_AUTHORIZATION_FIELDS = (
    "client_id",
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


def register_oauth_key_routes(router: APIRouter) -> None:

    @router.get(OAUTH_AUTHORIZATION_ENDPOINT_PATH)
    async def oauth_authorize_page(request: Request, settings: SettingsDep) -> Response:
        if request.query_params.get("consent"):
            return _resume_consent(request, settings)
        params = _oauth_params_from_query(request)
        oauth_app = _registered_oauth_app(params)
        try:
            principal = principal_from_request(request, settings)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return HTMLResponse(
                _signin_html(request, settings, params, oauth_app=oauth_app),
                status_code=401,
            )
        if not principal.is_management:
            raise api_error(403, "Only management users can delegate credits", ErrorType.FORBIDDEN)
        _deny_scoped_delegator(principal)
        consent = _create_consent(params, principal, settings, oauth_app=oauth_app)
        return HTMLResponse(_consent_html_for_record(consent, principal.workspace.name, oauth_app))

    @router.get("/oauth/authorize")
    async def oauth_authorize(request: Request, settings: SettingsDep) -> Response:
        params, app, error = _conformant_authorize_params(request)
        if error is not None:
            return error
        try:
            principal = principal_from_request(request, settings)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return HTMLResponse(_signin_html(request, settings, params, oauth_app=app), status_code=401)
        if not principal.is_management:
            raise api_error(403, "Only management users can delegate credits", ErrorType.FORBIDDEN)
        _deny_scoped_delegator(principal)
        consent = _create_consent(params, principal, settings, oauth_app=app, rfc_conformant=True)
        return HTMLResponse(_consent_html_for_record(consent, principal.workspace.name, app))

    @router.post("/auth/fund")
    async def oauth_authorize_fund(request: Request, settings: SettingsDep) -> Response:
        form = dict(await request.form())
        try:
            principal = principal_from_request(request, settings)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            raise api_error(401, "Sign in is required", ErrorType.UNAUTHORIZED) from exc
        if not principal.is_management:
            raise api_error(403, "Only management users can fund credits", ErrorType.FORBIDDEN)
        _deny_scoped_delegator(principal)
        consent = _owned_consent(str(form.get("consent") or ""), principal)

        amount = str(form.get("fund_amount") or "")
        if amount not in OAUTH_FUNDING_AMOUNTS:
            raise api_error(400, "fund_amount must be 5, 20, or 100", ErrorType.BAD_REQUEST)
        origin = request_control_origin(request, settings)
        try:
            body = CheckoutRequest(
                amount=amount,
                workspace_id=principal.workspace.id,
                payment_method="card",
                success_url=_consent_return_url(origin, consent.id, checkout="success"),
                cancel_url=_consent_return_url(origin, consent.id, checkout="cancel"),
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
                url=_consent_return_url(origin, consent.id, checkout="mock"),
                status_code=303,
            )
        return RedirectResponse(url=str(data["url"]), status_code=303)

    @router.post("/auth/approve")
    async def oauth_authorize_approve(request: Request, settings: SettingsDep) -> Response:
        form = dict(await request.form())
        try:
            principal = principal_from_request(request, settings)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            raise api_error(401, "Sign in is required", ErrorType.UNAUTHORIZED) from exc
        if not principal.is_management:
            raise api_error(403, "Only management users can delegate credits", ErrorType.FORBIDDEN)
        _deny_scoped_delegator(principal)
        consent = _owned_consent(str(form.get("consent") or ""), principal)
        if not hmac.compare_digest(str(form.get("csrf_token") or ""), consent.csrf_token):
            raise api_error(403, "Invalid consent CSRF token", ErrorType.FORBIDDEN)
        chosen = str(form.get("monthly_budget") or "20")
        if consent.client_app_id and chosen not in {"5", "20", "100", "none"}:
            raise api_error(400, "Invalid monthly budget", ErrorType.BAD_REQUEST)
        posted_limit_present = "limit" in form
        posted_limit = (
            _limit_microdollars(
                form.get("limit"),
                maximum_microdollars=CONSENT_FORM_LIMIT_MAX_DOLLARS * 1_000_000,
            )
            if posted_limit_present
            else None
        )
        posted_reset_present = "usage_limit_type" in form
        posted_reset = _limit_reset(form.get("usage_limit_type")) if posted_reset_present else None
        consumed = STORE.consume_consent_request(
            consent.id,
            user_id=str(_principal_user_id(principal) or ""),
            workspace_id=principal.workspace.id,
            csrf_token=str(form.get("csrf_token") or ""),
        )
        if consumed is None:
            raise api_error(400, "Consent request is expired or already used", ErrorType.BAD_REQUEST)
        if consumed.client_app_id:
            consumed.limit_microdollars = None if chosen == "none" else dollars_to_microdollars(chosen)
            consumed.limit_reset = None if chosen == "none" else "monthly"
        else:
            # The legacy consent page renders "Maximum spend (USD)" and "Limit
            # resets" as editable fields, but only the app's query-suggested
            # limit ever reached the key: the posted values were discarded, so
            # an app that suggested nothing (the Cowork desktop flow) minted an
            # UNCAPPED key while the page showed a $20 maximum, and a user who
            # edited a suggested limit was silently overridden. Honor what the
            # form actually posted (validated above, before the consent was
            # consumed); absent fields keep the consent's stored values so
            # programmatic approvals are unchanged.
            if posted_limit_present:
                consumed.limit_microdollars = posted_limit
            if posted_reset_present:
                consumed.limit_reset = posted_reset
        raw_code, code = _create_code_from_consent(consumed, settings)
        callback_url = (
            _conformant_callback_with_code(code.callback_url, raw_code, state=consumed.state)
            if consumed.rfc_conformant
            else _callback_with_code(code.callback_url, raw_code, code.user_id, state=consumed.state)
        )
        return RedirectResponse(url=callback_url, status_code=302)

    @router.post("/auth/keys/code")
    async def auth_keys_code(
        request: Request,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        params = await _oauth_params_from_json(request)
        _require_programmatic_app_ownership(params, principal)
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

    @router.post(OAUTH_KEY_EXCHANGE_ENDPOINT_PATH)
    async def auth_keys(request: Request) -> JSONResponse:
        limited = _rate_limit_exchange(request, rfc=False)
        if limited is not None:
            return limited
        body = await json_body(request)
        raw_code = str(body.get("code") or "")
        if not raw_code:
            raise api_error(400, "code is required", ErrorType.BAD_REQUEST)
        code = STORE.consume_oauth_authorization_code(raw_code)
        if code is None:
            raise api_error(403, "Invalid or expired authorization code", ErrorType.FORBIDDEN)
        _verify_pkce(code, body)
        if code.client_app_id:
            app = STORE.get_oauth_app(code.client_app_id)
            if app is None or app.suspended:
                raise api_error(403, "OAuth app is unavailable", ErrorType.FORBIDDEN)
        # Quiesce: a pre-pause OAuth code must not mint a key during pause.
        assert_workspace_billing_active(STORE.get_workspace(code.workspace_id))
        raw_key, key = STORE.create_api_key(
            workspace_id=code.workspace_id,
            name=code.key_label,
            creator_user_id=code.user_id,
            management=False,
            limit_microdollars=code.limit_microdollars,
            limit_reset=code.limit_reset,
            expires_at=code.expires_at,
            scopes=DEFAULT_DELEGATED_SCOPES,
            app_id=code.client_app_id,
        )
        # Return the signed-in user's identity alongside the key so the app
        # knows WHO signed in without a second /auth/userinfo round-trip
        # ("Sign in with TrustedRouter" = key + identity).
        user = STORE.get_user(code.user_id) if code.user_id else None
        return JSONResponse(
            {
                "key": raw_key,
                "user_id": code.user_id,
                "identity": identity_payload(user, code.workspace_id),
                "data": key_shape(key),
            }
        )

    @router.post("/oauth/token")
    async def oauth_token(request: Request) -> JSONResponse:
        limited = _rate_limit_exchange(request, rfc=True)
        if limited is not None:
            return limited
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            return _oauth_error("invalid_request", "Content-Type must be application/x-www-form-urlencoded")
        form = dict(await request.form())
        if str(form.get("grant_type") or "") != "authorization_code":
            return _oauth_error("invalid_request", "grant_type must be authorization_code")
        required = ("code", "code_verifier", "client_id", "redirect_uri")
        if any(not form.get(field) for field in required):
            return _oauth_error("invalid_request", "code, code_verifier, client_id, and redirect_uri are required")
        verifier = str(form["code_verifier"])
        if not CODE_VERIFIER_RE.fullmatch(verifier):
            return _oauth_error("invalid_request", "code_verifier is not valid RFC 7636 syntax")
        code = STORE.consume_oauth_authorization_code(str(form["code"]))
        if code is None:
            return _oauth_error("invalid_grant", "Authorization code is invalid, expired, or already used")
        if code.client_app_id != str(form["client_id"]) or code.callback_url != str(form["redirect_uri"]):
            return _oauth_error("invalid_grant", "Authorization code binding does not match")
        if code.code_challenge_method != "S256" or not _pkce_matches(code, verifier):
            return _oauth_error("invalid_grant", "code_verifier does not match")
        app = STORE.get_oauth_app(code.client_app_id)
        if app is None or app.suspended:
            return _oauth_error("invalid_grant", "OAuth app is unavailable")
        assert_workspace_billing_active(STORE.get_workspace(code.workspace_id))
        raw_key, _key = STORE.create_api_key(
            workspace_id=code.workspace_id, name=code.key_label,
            creator_user_id=code.user_id, management=False,
            limit_microdollars=code.limit_microdollars, limit_reset=code.limit_reset,
            expires_at=code.expires_at, scopes=code.scopes, app_id=code.client_app_id,
        )
        user = STORE.get_user(code.user_id) if code.user_id else None
        identity = identity_payload(user, code.workspace_id) or {"verification_level": "none"}
        return JSONResponse({"access_token": raw_key, "token_type": "bearer", "scope": " ".join(code.scopes), "trustedrouter": {"verification_level": identity["verification_level"], "app_id": code.client_app_id, "workspace_id": code.workspace_id}}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


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
    oauth_app = _registered_oauth_app(params)
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
        client_app_id=oauth_app.id if oauth_app is not None else "",
        scopes=DEFAULT_DELEGATED_SCOPES,
    )


def _create_code_from_consent(
    consent: ConsentRequest, settings: Settings
) -> tuple[str, OAuthAuthorizationCode]:
    return STORE.create_oauth_authorization_code(
        workspace_id=consent.workspace_id,
        user_id=consent.user_id,
        callback_url=consent.callback_url,
        key_label=consent.key_label,
        ttl_seconds=settings.oauth_authorization_code_ttl_seconds,
        app_id=_app_id(consent.callback_url),
        limit_microdollars=consent.limit_microdollars,
        limit_reset=consent.limit_reset,
        expires_at=consent.expires_at,
        code_challenge=consent.code_challenge,
        code_challenge_method=consent.code_challenge_method,
        client_app_id=consent.client_app_id,
        scopes=consent.scopes,
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
    _registered_oauth_app(params)


def _registered_oauth_app(params: dict[str, Any]) -> OAuthApp | None:
    client_id = _optional_str(params.get("client_id"))
    if client_id is None:
        return None
    app = STORE.get_oauth_app(client_id)
    if app is None or app.suspended:
        raise api_error(400, "client_id is unknown or suspended", ErrorType.BAD_REQUEST)
    callback_url = _validate_callback_url(str(params.get("callback_url") or ""))
    if callback_url not in app.redirect_uris:
        raise api_error(
            400,
            "callback_url is not registered for client_id",
            ErrorType.BAD_REQUEST,
        )
    return app


def _require_programmatic_app_ownership(
    params: dict[str, Any], principal: Principal
) -> None:
    """Prevent management programs from forging another app's attribution."""
    client_id = _optional_str(params.get("client_id"))
    if client_id is None:
        return
    app = STORE.get_oauth_app(client_id)
    actor_user_id = (
        principal.user.id
        if principal.user is not None
        else (
            principal.api_key.creator_user_id
            if principal.api_key is not None
            else None
        )
    )
    if app is None or actor_user_id != app.owner_user_id:
        raise api_error(
            403,
            "Only the OAuth app owner can mint a code with this client_id",
            ErrorType.FORBIDDEN,
        )


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


#: The consent page prints this straight after a "$", so it is app-supplied
#: text on our own page. An app that sends microdollars (or anything else)
#: must not be able to render "$20000000/month" as if the user were agreeing
#: to it. Anything not a plain dollar amount within the same bound as the
#: limit input is dropped rather than shown.
SUGGESTED_BUDGET_MAX_DOLLARS = 10_000


#: Digits, optionally a decimal point and one or two more digits. Deliberately
#: stricter than Decimal, which accepts "1e1", "+25" and " 25 " -- all of which
#: would render on the consent page as a figure the app never plainly wrote.
_PLAIN_DOLLARS = re.compile(r"^(0|[1-9][0-9]{0,4})(\.[0-9]{1,2})?$")


def _suggested_monthly_budget(raw: Any) -> str:
    """Normalise an app's budget hint to dollars, or drop it."""
    if raw in {None, ""}:
        return ""
    text = str(raw)
    if not _PLAIN_DOLLARS.match(text):
        return ""
    whole, _, cents = text.partition(".")
    dollars = int(whole)
    if dollars == 0 and int(cents.ljust(2, "0") or 0) == 0:
        return ""
    if dollars > SUGGESTED_BUDGET_MAX_DOLLARS:
        return ""
    # Match the preset labels ($5, $20, $100) for whole dollars; pad cents so a
    # fractional hint reads as money, not "$25.5". The regex bounds the cents
    # to two digits, so there is no rounding to carry.
    return str(dollars) if not cents else f"{dollars}.{cents.ljust(2, '0')}"


#: Ceiling for a limit typed into the consent page, matching the maximum the
#: input advertises client-side. UI policy only -- the programmatic paths keep
#: their documented no-maximum contract, bounded solely by storage safety.
CONSENT_FORM_LIMIT_MAX_DOLLARS = 10_000

#: Storage-safety ceiling for every path: the largest microdollar value the
#: BIGINT/INT64 limit columns can hold. Decimal happily parses 1e400 into an
#: integer that previously approved, burned the consent, and then failed (or
#: 500d outright via decimal.Overflow) when the key was serialized.
LIMIT_MAX_STORABLE_MICRODOLLARS = 2**63 - 1


def _limit_microdollars(
    raw: Any,
    *,
    maximum_microdollars: int = LIMIT_MAX_STORABLE_MICRODOLLARS,
) -> int | None:
    if raw in {None, ""}:
        return None
    try:
        value = dollars_to_microdollars(raw)
    except (ValueError, ArithmeticError) as exc:
        raise api_error(400, "limit must be a dollar amount", ErrorType.BAD_REQUEST) from exc
    if value < 0:
        raise api_error(400, "limit must be non-negative", ErrorType.BAD_REQUEST)
    if value > maximum_microdollars:
        raise api_error(
            400,
            f"limit must be at most ${maximum_microdollars // 1_000_000}",
            ErrorType.BAD_REQUEST,
        )
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


def _verify_pkce(code: OAuthAuthorizationCode, body: dict[str, Any]) -> None:
    if not code.code_challenge:
        return
    supplied_method = body.get("code_challenge_method")
    if supplied_method not in {None, ""} and str(supplied_method) != code.code_challenge_method:
        raise api_error(
            400, "code_challenge_method does not match authorization code", ErrorType.BAD_REQUEST
        )
    verifier = str(body.get("code_verifier") or "")
    if not verifier:
        raise api_error(400, "code_verifier is required", ErrorType.BAD_REQUEST)
    if code.code_challenge_method == "plain":
        expected = verifier
    else:
        try:
            verifier_bytes = verifier.encode("ascii")
        except UnicodeEncodeError as exc:
            raise api_error(400, "code_verifier must be ASCII", ErrorType.BAD_REQUEST) from exc
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier_bytes).digest())
            .decode("ascii")
            .rstrip("=")
        )
    if expected != code.code_challenge:
        raise api_error(403, "Invalid code_verifier", ErrorType.FORBIDDEN)


def _principal_user_id(principal: Any) -> str | None:
    if principal.user is not None:
        return principal.user.id
    if principal.api_key is not None and principal.api_key.creator_user_id:
        return principal.api_key.creator_user_id
    return principal.workspace.owner_user_id


def _app_id(callback_url: str) -> int:
    digest = hashlib.sha256(callback_url.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _callback_with_code(callback_url: str, raw_code: str, user_id: str | None, *, state: str = "") -> str:
    parsed = urlsplit(callback_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("code", raw_code))
    if user_id:
        query.append(("user_id", user_id))
    if state:
        query.append(("state", state))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _conformant_callback_with_code(callback_url: str, raw_code: str, *, state: str = "") -> str:
    parsed = urlsplit(callback_url)
    query = [("code", raw_code)]
    if state:
        query.append(("state", state))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _optional_str(raw: Any) -> str | None:
    if raw in {None, ""}:
        return None
    return str(raw)


def _deny_scoped_delegator(principal: Any) -> None:
    if principal.api_key is not None and principal.scopes:
        raise api_error(
            403,
            "A delegated API key cannot mint delegation codes",
            ErrorType.INSUFFICIENT_SCOPE,
        )


def _create_consent(
    params: dict[str, Any],
    principal: Any,
    settings: Settings,
    *,
    oauth_app: OAuthApp | None,
    rfc_conformant: bool = False,
) -> ConsentRequest:
    callback_url = _validate_callback_url(str(params.get("callback_url") or ""))
    suggested = _suggested_monthly_budget(params.get("suggested_monthly_budget"))
    consent = ConsentRequest(
        id=f"consent_{secrets.token_urlsafe(24)}",
        csrf_token=secrets.token_urlsafe(32),
        user_id=str(_principal_user_id(principal) or ""),
        workspace_id=principal.workspace.id,
        client_app_id=oauth_app.id if oauth_app else "",
        callback_url=callback_url,
        scopes=list(params.get("scopes") or DEFAULT_DELEGATED_SCOPES),
        code_challenge=_optional_str(params.get("code_challenge")),
        code_challenge_method=_pkce_method(params.get("code_challenge_method"), has_challenge=bool(params.get("code_challenge"))),
        key_label=_key_label(params.get("key_label"), callback_url),
        limit_microdollars=(dollars_to_microdollars(OAUTH_DEFAULT_KEY_LIMIT) if oauth_app else _limit_microdollars(params.get("limit"))),
        limit_reset=("monthly" if oauth_app else _limit_reset(params.get("usage_limit_type"))),
        expires_at=_expires_at(params.get("expires_at")),
        state=str(params.get("state") or ""),
        rfc_conformant=rfc_conformant,
        suggested_monthly_budget=suggested,
        consent_expires_at=(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=settings.oauth_authorization_code_ttl_seconds)).isoformat().replace("+00:00", "Z"),
    )
    return STORE.create_consent_request(consent)


def _owned_consent(consent_id: str, principal: Any) -> ConsentRequest:
    consent = STORE.get_consent_request(consent_id)
    if consent is None:
        raise api_error(400, "Consent request is invalid", ErrorType.BAD_REQUEST)
    if consent.user_id != str(_principal_user_id(principal) or "") or consent.workspace_id != principal.workspace.id:
        raise api_error(403, "Consent request belongs to another user", ErrorType.FORBIDDEN)
    if consent.consumed_at is not None or _consent_expired(consent):
        raise api_error(400, "Consent request is expired or already used", ErrorType.BAD_REQUEST)
    return consent


def _consent_expired(consent: ConsentRequest) -> bool:
    if not consent.consent_expires_at:
        return True
    return dt.datetime.fromisoformat(consent.consent_expires_at.replace("Z", "+00:00")) <= dt.datetime.now(dt.UTC)


def _resume_consent(request: Request, settings: Settings) -> Response:
    try:
        principal = principal_from_request(request, settings)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        raise api_error(401, "Sign in is required", ErrorType.UNAUTHORIZED) from exc
    consent = _owned_consent(str(request.query_params.get("consent") or ""), principal)
    app = STORE.get_oauth_app(consent.client_app_id) if consent.client_app_id else None
    return HTMLResponse(_consent_html_for_record(consent, principal.workspace.name, app, checkout=str(request.query_params.get("checkout") or "")))


def _consent_html_for_record(consent: ConsentRequest, workspace_name: str, app: OAuthApp | None, *, checkout: str = "") -> str:
    params: dict[str, Any] = {
        "callback_url": consent.callback_url, "client_id": consent.client_app_id,
        "key_label": consent.key_label, "checkout": checkout,
        "limit": (microdollars_to_decimal(consent.limit_microdollars) if consent.limit_microdollars is not None else ""),
        "usage_limit_type": consent.limit_reset or "",
    }
    return _consent_html(params, workspace_name=workspace_name, workspace_id=consent.workspace_id, oauth_app=app, consent=consent)


def _conformant_authorize_params(request: Request) -> tuple[dict[str, Any], OAuthApp | None, Response | None]:
    raw = dict(request.query_params)
    client_id = str(raw.get("client_id") or "")
    redirect_uri = str(raw.get("redirect_uri") or "")
    app = STORE.get_oauth_app(client_id) if client_id else None
    if app is None or app.suspended:
        return {}, None, _oauth_error("invalid_request", "client_id is unknown or suspended")
    if not redirect_uri or redirect_uri not in app.redirect_uris:
        return {}, None, _oauth_error("invalid_request", "redirect_uri is not registered")
    state = str(raw.get("state") or "")
    def redirected(error: str, description: str) -> tuple[dict[str, Any], OAuthApp | None, Response]:
        return {}, app, RedirectResponse(_oauth_error_redirect(redirect_uri, error, description, state), status_code=302)
    if raw.get("response_type") != "code":
        return redirected("unsupported_response_type", "response_type must be code")
    requested = str(raw.get("scope") or " ").split() if "scope" in raw else list(DEFAULT_DELEGATED_SCOPES)
    if not requested or set(requested) - KNOWN_SCOPES:
        return redirected("invalid_scope", "scope contains an unknown value")
    if not raw.get("code_challenge"):
        return redirected("invalid_request", "code_challenge is required")
    if raw.get("code_challenge_method") != "S256":
        return redirected("invalid_request", "code_challenge_method must be S256")
    return {"client_id": client_id, "callback_url": redirect_uri, "scopes": requested, "state": state, "code_challenge": str(raw["code_challenge"]), "code_challenge_method": "S256", "suggested_monthly_budget": _suggested_monthly_budget(raw.get("suggested_monthly_budget"))}, app, None


def _oauth_error_redirect(uri: str, error: str, description: str, state: str) -> str:
    parsed = urlsplit(uri)
    query = parse_qsl(parsed.query, keep_blank_values=True) + [("error", error), ("error_description", description)]
    if state:
        query.append(("state", state))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _oauth_error(error: str, description: str, *, status: int = 400, headers: dict[str, str] | None = None) -> JSONResponse:
    response_headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers=response_headers,
    )


def _pkce_matches(code: OAuthAuthorizationCode, verifier: str) -> bool:
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return hmac.compare_digest(expected, str(code.code_challenge or ""))


def _rate_limit_exchange(request: Request, *, rfc: bool) -> JSONResponse | None:
    ip = request.client.host if request.client else "unknown"
    # App identity keeps independently-created TestClient applications from
    # sharing a bucket; production has one long-lived app per process.
    subject = f"{id(request.app)}:{ip}"
    hit = enforce_rate_limit("oauth_token", subject, OAUTH_TOKEN_RATE_LIMIT, window_seconds=60)
    if hit is None or hit.allowed:
        return None
    headers = {"Retry-After": str(hit.retry_after_seconds)}
    if rfc:
        return _oauth_error("temporarily_unavailable", "Token exchange rate limit exceeded", status=429, headers=headers)
    return JSONResponse({"error": {"message": "Token exchange rate limit exceeded", "type": "rate_limited", "status": 429}}, status_code=429, headers=headers)


def _signin_html(
    request: Request,
    settings: Settings,
    params: dict[str, Any],
    *,
    oauth_app: OAuthApp | None,
) -> str:
    next_path = str(request.url.path) + ("?" + str(request.url.query) if request.url.query else "")
    return render_template(
        "auth/oauth_signin.html",
        page_title="Authorize TrustedRouter",
        app_name=(
            oauth_app.name
            if oauth_app is not None
            else _key_label(params.get("key_label"), str(params.get("callback_url") or ""))
        ),
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
    oauth_app: OAuthApp | None,
    consent: ConsentRequest | None = None,
) -> str:
    app_owner_line = ""
    if oauth_app is not None:
        owner = STORE.get_user(oauth_app.owner_user_id)
        verified_name = (owner.identity_verified_name or "").strip() if owner else ""
        if not verified_name:
            raise api_error(
                403,
                "This app cannot be presented because its owner's verified name is "
                "unavailable; the owner must re-verify.",
                ErrorType.VERIFICATION_REQUIRED,
            )
        app_owner_line = f"by {verified_name} (identity-verified)"

    callback_url = _validate_callback_url(str(params.get("callback_url") or ""))
    key_label = _key_label(params.get("key_label"), callback_url)
    consent_app_name = (
        f"{oauth_app.name} · {oauth_app.id}" if oauth_app is not None else key_label
    )
    limit = _limit_microdollars(params.get("limit"))
    # The consent input advertises max=10000 and the approve handler enforces
    # the same ceiling on posted values, so render any larger app suggestion
    # clamped: the user sees, edits, and approves the number that will actually
    # bind. (Un-clamped, a $20,000 suggestion pre-filled an unsubmittable field
    # -- and before posted values were honored at all, the browser forced the
    # user to type a smaller number and then minted the app's larger one.)
    # Headless callers who need more use the JSON path, which has no ceiling.
    effective_limit = OAUTH_DEFAULT_KEY_LIMIT if limit is None else microdollars_to_decimal(limit)
    if limit is not None and limit > CONSENT_FORM_LIMIT_MAX_DOLLARS * 1_000_000:
        effective_limit = microdollars_to_decimal(CONSENT_FORM_LIMIT_MAX_DOLLARS * 1_000_000)
    reset = _limit_reset(params.get("usage_limit_type")) or ""
    summary = live_credit_summary(workspace_id)
    available = summary["available"] if summary else 0
    hidden_fields = [("consent", consent.id), ("csrf_token", consent.csrf_token)] if consent else _hidden_authorization_fields(params, exclude={"limit", "usage_limit_type"})
    funding_hidden_fields = [("consent", consent.id)] if consent else _hidden_authorization_fields({**params, "limit": effective_limit, "usage_limit_type": reset})
    checkout_status = str(params.get("checkout") or "")
    if checkout_status not in {"success", "cancel", "mock"}:
        checkout_status = ""
    template_name = (
        "auth/oauth_consent_registered.html"
        if oauth_app is not None
        else "auth/oauth_consent.html"
    )
    return render_template(
        template_name,
        page_title=f"Authorize {consent_app_name}",
        key_label=consent_app_name,
        callback_host=urlsplit(callback_url).hostname or callback_url,
        app_logo_url=oauth_app.logo_url if oauth_app is not None else None,
        app_owner_line=app_owner_line,
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
        consent=consent,
        suggested_monthly_budget=consent.suggested_monthly_budget if consent else "",
        markup_percent=(f"{oauth_app.markup_basis_points / 100:g}" if oauth_app and oauth_app.markup_basis_points else ""),
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


def _consent_return_url(origin: str, consent_id: str, *, checkout: str) -> str:
    return f"{origin}/auth?{urlencode({'consent': consent_id, 'checkout': checkout})}"


def _callback_with_error(callback_url: str, error: str) -> str:
    parsed = urlsplit(callback_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("error", error))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
