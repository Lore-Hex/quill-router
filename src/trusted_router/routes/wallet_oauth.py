"""MetaMask SIWE sign-in.

Wallet users authenticate fully with a signed SIWE challenge. We do not
require email before creating an active console session: crypto-native
users expect wallet-only auth. Wallet-only accounts start at $0; the starter
credit is reserved for email and OAuth signups.

`/auth/wallet/email` lets a wallet session (active, or a legacy
`pending_email` one) attach an email. Wallet-only accounts need a verified
email to unlock non-stablecoin billing and BYOK, so this is the path the
console points them to. The address is NOT written to the account when the
form is submitted: the emailed link carries it, the one-shot token is bound
to it, and only a click on that link attaches it, already verified. Writing
an unverified address first would reserve it in the email index, and a later
OAuth sign-in with that address would land in the wallet account.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from trusted_router.acquisition import (
    record_signup_attribution,
    request_attribution,
    same_site_referer_path,
)
from trusted_router.auth import (
    SESSION_COOKIE_NAME,
    SettingsDep,
    set_session_cookie,
)
from trusted_router.billing_policy import WALLET_EMAIL_ATTACH_PATH
from trusted_router.domains import request_control_domain
from trusted_router.errors import api_error
from trusted_router.services.email import build_verification_email, get_email_service
from trusted_router.signup_gate import require_new_account_creation
from trusted_router.storage import STORE, AuthSession, User
from trusted_router.types import ErrorType
from trusted_router.views import render_template
from trusted_router.wallet_auth import (
    ADDRESS_RE,
    build_siwe_message,
    recover_address,
)

CHALLENGE_TTL_SECONDS = 300  # 5 minutes
VERIFICATION_TTL_SECONDS = 60 * 60 * 24  # 24 hours
WALLET_EMAIL_VERIFY_PATH = "/auth/wallet/email/verify"
_ATTACH_SESSION_STATES = frozenset({"active", "pending_email"})
_ATTACH_PURPOSE_PREFIX = "wallet_email_attach:"
# Verification sends per user per hour. Wallet accounts are free to create,
# so the form must not become an open relay for mail to arbitrary addresses.
# Per process: a soft cap, like the other in-memory limiters here.
_ATTACH_SENDS_PER_WINDOW = 5
_ATTACH_WINDOW_SECONDS = 60 * 60
_ATTACH_MAX_TRACKED_USERS = 4096
_attach_sends: OrderedDict[str, list[float]] = OrderedDict()
_attach_sends_lock = threading.Lock()


class WalletChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address: str = Field(min_length=42, max_length=42)


class WalletVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address: str = Field(min_length=42, max_length=42)
    signature: str = Field(min_length=1)
    nonce: str = Field(min_length=1)


def register_wallet_oauth_routes(router: APIRouter) -> None:
    @router.post("/auth/wallet/challenge")
    def wallet_challenge(
        request: Request,
        body: WalletChallengeRequest,
        settings: SettingsDep,
    ) -> JSONResponse:
        if not ADDRESS_RE.match(body.address):
            raise api_error(400, "Invalid Ethereum address", ErrorType.BAD_REQUEST)
        if not settings.new_signups_enabled and STORE.find_user_by_wallet(body.address) is None:
            # The emergency signup brake must stop durable challenge growth as
            # well as account creation. Returning wallets still receive a
            # challenge; unknown addresses fail after one bounded read and
            # before nonce generation or either challenge write.
            require_new_account_creation(settings)
        request_domain = request_control_domain(request, settings)
        domain = (
            settings.siwe_domain
            if request_domain == settings.trusted_domain and settings.siwe_domain
            else request_domain
        )
        nonce = secrets.token_urlsafe(32)
        message, _ = build_siwe_message(
            domain=domain,
            address=body.address,
            nonce=nonce,
            issued_at=dt.datetime.now(dt.UTC),
            expiration_seconds=CHALLENGE_TTL_SECONDS,
        )
        nonce, record = STORE.create_wallet_challenge(
            address=body.address,
            message=message,
            ttl_seconds=CHALLENGE_TTL_SECONDS,
            raw_nonce=nonce,
        )
        return JSONResponse(
            {
                "data": {
                    "message": record.message,
                    "nonce": nonce,
                    "expires_at": record.expires_at,
                }
            }
        )

    @router.post("/auth/wallet/verify")
    def wallet_verify(
        request: Request,
        body: WalletVerifyRequest,
        settings: SettingsDep,
    ) -> Response:
        if not ADDRESS_RE.match(body.address):
            raise api_error(400, "Invalid Ethereum address", ErrorType.BAD_REQUEST)
        challenge = STORE.consume_wallet_challenge(body.nonce)
        if challenge is None:
            raise api_error(400, "Invalid or expired challenge", ErrorType.BAD_REQUEST)
        if challenge.address != body.address.strip().lower():
            raise api_error(400, "Address does not match challenge", ErrorType.BAD_REQUEST)

        try:
            recovered = recover_address(message=challenge.message, signature=body.signature)
        except Exception as exc:  # noqa: BLE001 - eth-account raises various exceptions.
            raise api_error(400, "Signature verification failed", ErrorType.BAD_REQUEST) from exc
        if recovered != body.address.strip().lower():
            raise api_error(400, "Signature does not match address", ErrorType.BAD_REQUEST)

        existing_user = STORE.find_user_by_wallet(body.address)
        if existing_user is None:
            require_new_account_creation(settings)
        user = existing_user or STORE.create_wallet_user(body.address)
        if user.disabled:
            raise api_error(403, "This account is closed. Contact security@trustedrouter.com for assistance.", ErrorType.FORBIDDEN)
        workspaces = STORE.list_workspaces_for_user(user.id)
        workspace = workspaces[0] if workspaces else STORE.create_workspace(
            user.id,
            "Personal Workspace",
            # Only repair a missing workspace here. Existing accounts do not
            # receive a retroactive or repeat starter grant.
            trial_credit_microdollars=0,
        )
        if existing_user is None:
            record_signup_attribution(
                request,
                workspace_id=workspace.id,
                signup_provider="metamask",
                starter_credit_microdollars=0,
                user_id=user.id,
                signup_path=same_site_referer_path(request),
            )

        raw_token, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="metamask",
            label=body.address.lower(),
            ttl_seconds=settings.auth_session_ttl_seconds,
            workspace_id=workspace.id,
            state="active",
        )
        response = JSONResponse(
            {
                "data": {
                    "redirect": "/console/api-keys",
                    "state": "active",
                    "email_required": False,
                    "workspace_id": workspace.id,
                }
            }
        )
        set_session_cookie(response, raw_token, settings)
        return response

    @router.get(WALLET_EMAIL_ATTACH_PATH)
    def wallet_email_form(request: Request, settings: SettingsDep) -> Response:
        session, user = _require_wallet_session(request)
        if _has_verified_email(user):
            return _already_verified_response(session)
        return HTMLResponse(_form_page(session=session, error=None))

    @router.post(WALLET_EMAIL_ATTACH_PATH)
    def wallet_email_submit(
        request: Request,
        settings: SettingsDep,
        email: str = Form(..., min_length=3, max_length=320),
        csrf_token: str = Form("", max_length=128),
    ) -> Response:
        session, user = _require_wallet_session(request)
        if not hmac.compare_digest(csrf_token.encode(), _attach_csrf_token(session).encode()):
            return HTMLResponse(
                _form_page(session=session, error="This form expired. Try again."),
                status_code=403,
            )
        if _has_verified_email(user):
            return _already_verified_response(session)
        email_normalized = email.strip().lower()
        if not _plausible_email(email_normalized):
            return HTMLResponse(
                _form_page(session=session, error="Enter a valid email address."),
                status_code=400,
            )
        existing = STORE.find_user_by_email(email_normalized)
        if existing is not None and existing.id != user.id:
            return HTMLResponse(
                _form_page(
                    session=session,
                    error="That email already has an account — sign in with that provider.",
                ),
                status_code=409,
            )
        if not _attach_send_allowed(user.id):
            return HTMLResponse(
                _form_page(
                    session=session,
                    error="Too many verification emails. Try again in an hour.",
                ),
                status_code=429,
            )

        # The token is bound to this exact address through its purpose, so a
        # link minted for one address can never verify a different one.
        raw_token, _ = STORE.create_verification_token(
            user_id=user.id,
            purpose=_attach_purpose(email_normalized),
            ttl_seconds=VERIFICATION_TTL_SECONDS,
        )
        verify_url = _verify_url(request, raw_token, email_normalized)
        attribution = request_attribution(request)
        touch = attribution.last_touch if attribution is not None else {}
        message = build_verification_email(
            to=email_normalized,
            verification_url=verify_url,
            from_name=settings.ses_from_name,
            acquisition_source=touch.get("utm_source"),
            acquisition_medium=touch.get("utm_medium"),
            acquisition_campaign=touch.get("utm_campaign"),
        )
        sent = get_email_service(settings).send(message)
        return HTMLResponse(render_template(
            "auth/wallet_email_sent.html",
            page_title="Check your email",
            email=email_normalized,
            dev_link=None if sent else verify_url,
        ))

    @router.get(WALLET_EMAIL_VERIFY_PATH)
    def wallet_email_verify(
        request: Request,
        settings: SettingsDep,
        token: str = "",
        email: str = "",
    ) -> Response:
        email_normalized = email.strip().lower()
        consumed = (
            STORE.consume_verification_token(token, purpose=_attach_purpose(email_normalized))
            if token and email_normalized
            else None
        )
        if consumed is None:
            return HTMLResponse(_invalid_page(), status_code=400)
        user = STORE.get_user(consumed.user_id)
        if user is None or user.disabled:
            return HTMLResponse(_invalid_page(), status_code=400)
        if _has_verified_email(user):
            if (user.email or "").strip().lower() != email_normalized:
                return HTMLResponse(
                    _conflict_page("This account already has a verified email."),
                    status_code=409,
                )
        else:
            # set_user_email refuses (None) an address another user owns, so
            # a verified click still cannot take over someone else's email.
            if STORE.set_user_email(user.id, email_normalized) is None:
                return HTMLResponse(
                    _conflict_page(
                        "That email already has an account — sign in with that provider."
                    ),
                    status_code=409,
                )
            if STORE.mark_user_email_verified(user.id) is None:
                return HTMLResponse(_invalid_page(), status_code=400)

        done = HTMLResponse(
            render_template("auth/verify_email_done.html", page_title="Email verified"),
            status_code=200,
        )
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
        session = STORE.get_auth_session_by_raw(cookie_token) if cookie_token else None
        if cookie_token is None or session is None or session.user_id != user.id:
            return done
        if session.state == "pending_email":
            if STORE.upgrade_auth_session(cookie_token, state="active") is None:
                return done
            response = RedirectResponse(url="/console/welcome?first=1", status_code=302)
            set_session_cookie(response, cookie_token, settings)
            return response
        if session.state != "active":
            return done
        return RedirectResponse(url="/console/credits", status_code=302)


def _require_wallet_session(request: Request) -> tuple[AuthSession, User]:
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_token:
        raise api_error(401, "Wallet sign-in required", ErrorType.UNAUTHORIZED)
    session = STORE.get_auth_session_by_raw(cookie_token)
    if (
        session is None
        or session.provider != "metamask"
        or session.state not in _ATTACH_SESSION_STATES
    ):
        raise api_error(401, "Wallet sign-in required", ErrorType.UNAUTHORIZED)
    user = STORE.get_user(session.user_id)
    if user is None or user.disabled or not user.wallet_address:
        raise api_error(401, "Wallet sign-in required", ErrorType.UNAUTHORIZED)
    return session, user


def _has_verified_email(user: User) -> bool:
    return bool(user.email and user.email_verified)


def _already_verified_response(session: AuthSession) -> Response:
    if session.state == "active":
        return RedirectResponse(url="/console/credits", status_code=303)
    return HTMLResponse(
        render_template("auth/verify_email_done.html", page_title="Email verified"),
        status_code=200,
    )


def _attach_csrf_token(session: AuthSession) -> str:
    return hmac.new(
        session.secret_hash.encode(),
        f"wallet-email-attach:{session.hash}:{session.user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _attach_purpose(email_normalized: str) -> str:
    digest = hashlib.sha256(email_normalized.encode()).hexdigest()
    return f"{_ATTACH_PURPOSE_PREFIX}{digest}"


def _plausible_email(email: str) -> bool:
    local, sep, domain = email.partition("@")
    return bool(
        sep
        and local
        and "." in domain
        and "@" not in domain
        and not domain.startswith(".")
        and not domain.endswith(".")
        and not any(ch.isspace() for ch in email)
    )


def _attach_send_allowed(user_id: str, *, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    cutoff = now - _ATTACH_WINDOW_SECONDS
    with _attach_sends_lock:
        hits = [hit for hit in _attach_sends.get(user_id, ()) if hit > cutoff]
        allowed = len(hits) < _ATTACH_SENDS_PER_WINDOW
        if allowed:
            hits.append(now)
        _attach_sends[user_id] = hits
        _attach_sends.move_to_end(user_id)
        while len(_attach_sends) > _ATTACH_MAX_TRACKED_USERS:
            _attach_sends.popitem(last=False)
        return allowed


def reset_attach_send_limits() -> None:
    """Test hook: clear the per-process send counters."""
    with _attach_sends_lock:
        _attach_sends.clear()


def _verify_url(request: Request, raw_token: str, email_normalized: str) -> str:
    base = str(request.base_url).rstrip("/")
    query = urlencode({"token": raw_token, "email": email_normalized})
    return f"{base}{WALLET_EMAIL_VERIFY_PATH}?{query}"


def _form_page(*, session: AuthSession, error: str | None) -> str:
    return render_template(
        "auth/wallet_email_form.html",
        page_title="Verify your email",
        address=session.label,
        csrf_token=_attach_csrf_token(session),
        pending=session.state == "pending_email",
        error=error,
    )


def _invalid_page() -> str:
    return render_template("auth/verify_email_invalid.html", page_title="Link expired")


def _conflict_page(message: str) -> str:
    return render_template(
        "auth/wallet_email_conflict.html", page_title="Email not added", message=message
    )
