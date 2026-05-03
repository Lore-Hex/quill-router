from __future__ import annotations

import base64
import datetime as dt
import hashlib
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from trusted_router.auth import ManagementPrincipal, SettingsDep, principal_from_request
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import dollars_to_microdollars
from trusted_router.routes.helpers import json_body
from trusted_router.serialization import key_shape
from trusted_router.storage import STORE, OAuthAuthorizationCode
from trusted_router.types import ErrorType
from trusted_router.views import render_template

PKCE_METHODS = {"S256", "plain"}
RESET_INTERVALS = {"daily", "weekly", "monthly"}


def register_oauth_key_routes(router: APIRouter) -> None:
    @router.get("/auth")
    async def oauth_authorize_page(request: Request, settings: SettingsDep) -> Response:
        params = _oauth_params_from_query(request)
        try:
            principal = principal_from_request(request, settings)
        except HTTPException:
            return HTMLResponse(_signin_html(request, settings, params), status_code=401)
        if not principal.is_management:
            raise api_error(403, "Only management users can delegate credits", ErrorType.FORBIDDEN)
        return HTMLResponse(_consent_html(params, principal.workspace.name))

    @router.post("/auth/approve")
    async def oauth_authorize_approve(request: Request, settings: SettingsDep) -> Response:
        params = _oauth_params_from_form(await request.form())
        try:
            principal = principal_from_request(request, settings)
        except HTTPException as exc:
            raise api_error(401, "Sign in is required", ErrorType.UNAUTHORIZED) from exc
        if not principal.is_management:
            raise api_error(403, "Only management users can delegate credits", ErrorType.FORBIDDEN)
        raw_code, code = _create_code(params, principal, settings)
        return RedirectResponse(url=_callback_with_code(code.callback_url, raw_code, code.user_id), status_code=302)

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
    async def auth_keys(request: Request) -> JSONResponse:
        body = await json_body(request)
        raw_code = str(body.get("code") or "")
        if not raw_code:
            raise api_error(400, "code is required", ErrorType.BAD_REQUEST)
        code = STORE.consume_oauth_authorization_code(raw_code)
        if code is None:
            raise api_error(403, "Invalid or expired authorization code", ErrorType.FORBIDDEN)
        _verify_pkce(code, body)
        raw_key, key = STORE.create_api_key(
            workspace_id=code.workspace_id,
            name=code.key_label,
            creator_user_id=code.user_id,
            management=False,
            limit_microdollars=code.limit_microdollars,
            limit_reset=code.limit_reset,
            expires_at=code.expires_at,
        )
        return JSONResponse(
            {
                "key": raw_key,
                "user_id": code.user_id,
                "data": key_shape(key),
            }
        )


def _create_code(params: dict[str, Any], principal: Any, settings: Settings) -> tuple[str, OAuthAuthorizationCode]:
    callback_url = _validate_callback_url(str(params.get("callback_url") or ""))
    code_challenge = _optional_str(params.get("code_challenge"))
    code_challenge_method = _pkce_method(params.get("code_challenge_method"), has_challenge=bool(code_challenge))
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
    _validate_callback_url(str(params.get("callback_url") or ""))
    _pkce_method(params.get("code_challenge_method"), has_challenge=bool(params.get("code_challenge")))
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
    localhost_callback = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not localhost_callback:
        raise api_error(400, "callback_url must be an https URL", ErrorType.BAD_REQUEST)
    port = parsed.port
    if parsed.username or parsed.password:
        raise api_error(400, "callback_url cannot contain credentials", ErrorType.BAD_REQUEST)
    if localhost_callback and port == 3000:
        return callback_url
    if port not in {None, 443, 3000}:
        raise api_error(400, "callback_url port must be 443 or 3000", ErrorType.BAD_REQUEST)
    return callback_url


def _pkce_method(raw: Any, *, has_challenge: bool) -> str | None:
    if raw in {None, ""}:
        return "S256" if has_challenge else None
    method = str(raw)
    if method not in PKCE_METHODS:
        raise api_error(400, "code_challenge_method must be S256 or plain", ErrorType.BAD_REQUEST)
    if method and not has_challenge:
        raise api_error(400, "code_challenge is required when code_challenge_method is set", ErrorType.BAD_REQUEST)
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
        raise api_error(400, "usage_limit_type must be daily, weekly, or monthly", ErrorType.BAD_REQUEST)
    return value


def _expires_at(raw: Any) -> str | None:
    if raw in {None, ""}:
        return None
    value = str(raw)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise api_error(400, "expires_at must be an ISO 8601 timestamp", ErrorType.BAD_REQUEST) from exc
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
        raise api_error(400, "code_challenge_method does not match authorization code", ErrorType.BAD_REQUEST)
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
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier_bytes).digest()).decode("ascii").rstrip("=")
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


def _callback_with_code(callback_url: str, raw_code: str, user_id: str | None) -> str:
    parsed = urlsplit(callback_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("code", raw_code))
    if user_id:
        query.append(("user_id", user_id))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _optional_str(raw: Any) -> str | None:
    if raw in {None, ""}:
        return None
    return str(raw)


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
        next_path_encoded=urlencode({"next": next_path})[len("next="):],
    )


def _consent_html(params: dict[str, Any], workspace_name: str) -> str:
    callback_url = _validate_callback_url(str(params.get("callback_url") or ""))
    key_label = _key_label(params.get("key_label"), callback_url)
    limit = _limit_microdollars(params.get("limit"))
    limit_text = "No key limit" if limit is None else f"${limit / 1_000_000:g} key limit"
    return render_template(
        "auth/oauth_consent.html",
        page_title=f"Authorize {key_label}",
        key_label=key_label,
        callback_host=urlsplit(callback_url).hostname or callback_url,
        workspace_name=workspace_name,
        limit_text=limit_text,
        # Pass through every non-empty param as a hidden field — the
        # template handles autoescape.
        hidden_fields=[
            (str(name), str(value))
            for name, value in params.items()
            if value not in {None, ""}
        ],
    )


