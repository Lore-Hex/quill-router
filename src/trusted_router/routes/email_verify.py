"""Email verification: the session carries the challenge; only the mailbox receives the code."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from trusted_router.acquisition import request_attribution
from trusted_router.auth import SESSION_COOKIE_NAME, SettingsDep, set_session_cookie
from trusted_router.billing_policy import WALLET_EMAIL_ATTACH_PATH
from trusted_router.errors import api_error
from trusted_router.request_limits import fingerprint_subject, rate_limit_headers
from trusted_router.services.email import (
    build_email_in_use_notice,
    build_verification_email,
    get_email_service,
)
from trusted_router.signup_gate import is_disposable_email
from trusted_router.storage import STORE, AuthSession, User
from trusted_router.storage_models import (
    EMAIL_ATTACH_ALREADY_VERIFIED,
    EMAIL_ATTACH_ATTACHED,
    EMAIL_ATTACH_TAKEN,
)
from trusted_router.types import ErrorType
from trusted_router.views import render_template

VERIFY_PATH = "/auth/verify-email"
WALLET_EMAIL_VERIFY_PATH = "/auth/wallet/email/verify"
VERIFICATION_TTL_SECONDS = 10 * 60
_SENDS_PER_HOUR = 5
_TRIES_PER_WINDOW = 5


def _require_session(request: Request) -> tuple[AuthSession, User]:
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    session = STORE.get_auth_session_by_raw(cookie_token) if cookie_token else None
    if session is not None and session.state in {"active", "pending_email"}:
        user = STORE.get_user(session.user_id)
        if user is not None and not user.disabled and (session.provider != "metamask" or user.wallet_address):
            return session, user
    raise api_error(401, "Sign in to verify an email address.", ErrorType.UNAUTHORIZED)


AccountDep = Annotated[tuple[AuthSession, User], Depends(_require_session)]


def _wallet_only(account: AccountDep) -> None:
    if account[0].provider != "metamask":
        raise api_error(401, "Wallet sign-in required", ErrorType.UNAUTHORIZED)


def _eligible_address(session: AuthSession, user: User, email: str) -> bool:
    # The general flow verifies the address on file; it is not an email-change or recovery route.
    return session.provider == "metamask" or bool(user.email and email == user.email.strip().lower())


def register_email_verify_routes(router: APIRouter) -> None:
    @router.get(VERIFY_PATH)
    def verify_email(request: Request, token: str | None = None) -> Response:
        if token is not None:
            return _retired_link()
        return _start_form(*_require_session(request))

    @router.get(WALLET_EMAIL_ATTACH_PATH, dependencies=[Depends(_wallet_only)])
    def wallet_email_form(account: AccountDep) -> Response:
        return _start_form(*account)

    @router.post(VERIFY_PATH + "/request")
    @router.post(WALLET_EMAIL_ATTACH_PATH, dependencies=[Depends(_wallet_only)])
    def request_email_code(
        request: Request, account: AccountDep, settings: SettingsDep,
        email: str = Form(..., min_length=3, max_length=320),
        csrf_token: str = Form("", max_length=128),
    ) -> Response:
        session, user = account
        if not _valid_attach_csrf(session, csrf_token):
            return HTMLResponse(
                _form_page(session=session, user=user, error="This form expired. Try again."),
                status_code=403,
            )
        if _has_verified_email(user):
            return _already_verified_response(session)
        email_normalized = email.strip().lower()
        if (
            not _eligible_address(session, user, email_normalized)
            or not _plausible_email(email_normalized)
            or is_disposable_email(email_normalized)
        ):
            return HTMLResponse(
                _form_page(session=session, user=user, error="Enter a valid, non-disposable email address."),
                status_code=400,
            )
        # Counted before the ownership check, so a taken address costs the
        # same send as a free one and the cap cannot tell them apart.
        if not STORE.hit_rate_limit(
            namespace="email_verify_send_user", subject=fingerprint_subject(user.id),
            limit=_SENDS_PER_HOUR, window_seconds=3600,
        ).allowed:
            return HTMLResponse(
                _form_page(
                    session=session, user=user,
                    error="Too many verification emails. Try again in an hour.",
                ),
                status_code=429,
            )

        # Recent requests by other accounts must not be visible to the requester.
        destination = STORE.hit_rate_limit(
            namespace="email_verify_send_recipient", subject=fingerprint_subject(email_normalized),
            limit=_SENDS_PER_HOUR, window_seconds=3600,
        )
        if not destination.allowed:
            return _code_form(session, email_normalized, secrets.token_urlsafe(32))
        existing = STORE.find_user_by_email(email_normalized)
        if existing is not None and existing.id != user.id:
            challenge = secrets.token_urlsafe(32)
            # Return the same form without minting a usable challenge.
            message = build_email_in_use_notice(
                to=email_normalized,
                from_name=settings.ses_from_name,
            )
        else:
            code = f"{secrets.randbelow(100_000_000):08d}"
            # The browser gets the opaque token; the mailbox gets only the code.
            challenge, _ = STORE.create_verification_token(
                user_id=user.id,
                purpose=_code_purpose(session, email_normalized, code),
                ttl_seconds=VERIFICATION_TTL_SECONDS,
            )
            attribution = request_attribution(request)
            touch = attribution.last_touch if attribution is not None else {}
            message = build_verification_email(
                to=email_normalized,
                code=code,
                ttl_minutes=VERIFICATION_TTL_SECONDS // 60,
                from_name=settings.ses_from_name,
                acquisition_source=touch.get("utm_source"),
                acquisition_medium=touch.get("utm_medium"),
                acquisition_campaign=touch.get("utm_campaign"),
            )
        if not get_email_service(settings).send(message):
            return HTMLResponse(
                _form_page(
                    session=session, user=user,
                    error="We could not send a verification email. Try again later.",
                ),
                status_code=503,
            )
        return _code_form(session, email_normalized, challenge)

    @router.get(WALLET_EMAIL_VERIFY_PATH)
    def wallet_email_legacy_link() -> Response:
        return _retired_link()

    @router.post(VERIFY_PATH)
    @router.post(WALLET_EMAIL_VERIFY_PATH, dependencies=[Depends(_wallet_only)])
    def confirm_email_code(
        request: Request, account: AccountDep, settings: SettingsDep,
        email: str = Form(..., min_length=3, max_length=320),
        challenge: str = Form(..., min_length=1, max_length=128),
        code: str = Form(..., min_length=1, max_length=32),
        csrf_token: str = Form("", max_length=128),
    ) -> Response:
        session, user = account
        if not _valid_attach_csrf(session, csrf_token):
            return HTMLResponse(_form_page(session=session, user=user, error="This form expired. Try again."), status_code=403)
        email_normalized = email.strip().lower()
        quota = STORE.hit_rate_limit(
            namespace="email_verify_attempt", subject=fingerprint_subject(user.id),
            limit=_TRIES_PER_WINDOW, window_seconds=VERIFICATION_TTL_SECONDS,
        )
        if not quota.allowed:
            raise api_error(429, "Too many verification attempts. Try again later.",
                            ErrorType.RATE_LIMITED, headers=rate_limit_headers(quota))
        code = code.strip()
        consumed = (
            STORE.consume_verification_token(challenge, purpose=_code_purpose(session, email_normalized, code))
            if _eligible_address(session, user, email_normalized)
            and _plausible_email(email_normalized) and not is_disposable_email(email_normalized)
            and len(code) == 8 and code.isascii() and code.isdecimal() else None
        )
        if consumed is None or consumed.user_id != user.id:
            return _code_form(session, email_normalized, challenge, error="Invalid or expired verification code.")
        # One conditional transaction: refuses an address another user owns
        # and never replaces an email this account has already verified
        # (for example via a concurrently confirmed code for another address).
        outcome = STORE.attach_verified_email(user.id, email_normalized)
        if outcome == EMAIL_ATTACH_TAKEN:
            return HTMLResponse(
                _conflict_page("That email already has an account — sign in with that provider."),
                status_code=409,
            )
        if outcome == EMAIL_ATTACH_ALREADY_VERIFIED:
            return HTMLResponse(
                _conflict_page("This account already has a verified email."),
                status_code=409,
            )
        if outcome != EMAIL_ATTACH_ATTACHED:
            return HTMLResponse(_conflict_page("Unable to verify this email for this account."), status_code=400)

        if session.state == "pending_email":
            cookie_token = request.cookies[SESSION_COOKIE_NAME]
            if STORE.upgrade_auth_session(cookie_token, state="active") is None:
                return HTMLResponse(render_template("auth/verify_email_done.html", page_title="Email verified"))
            response = RedirectResponse(url="/console/welcome?first=1", status_code=302)
            set_session_cookie(response, cookie_token, settings)
            return response
        return _verified_redirect(session)


def _code_form(session: AuthSession, email: str, challenge: str, *, error: str | None = None) -> HTMLResponse:
    return HTMLResponse(render_template(
        "auth/email_code.html", page_title="Verify your email",
        email=email, challenge=challenge, csrf_token=_attach_csrf_token(session), error=error,
    ), status_code=400 if error else 200, headers={"Cache-Control": "no-store"})


def _retired_link() -> HTMLResponse:
    # Mail previews and old signup/wallet links cannot consume a token or upgrade a session.
    return HTMLResponse(
        render_template("auth/email_verification_conflict.html", page_title="Request a code",
                        message="This link can no longer verify an address. Sign in and request a code."),
        status_code=410, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _start_form(session: AuthSession, user: User) -> Response:
    if _has_verified_email(user):
        return _already_verified_response(session)
    return HTMLResponse(_form_page(session=session, user=user, error=None), headers={"Cache-Control": "no-store"})


def _has_verified_email(user: User) -> bool:
    return bool(user.email and user.email_verified)


def _verified_redirect(session: AuthSession) -> RedirectResponse:
    destination = "/console/credits" if session.provider == "metamask" else "/console/account/preferences"
    return RedirectResponse(destination, status_code=303)


def _already_verified_response(session: AuthSession) -> Response:
    if session.state == "active":
        return _verified_redirect(session)
    return HTMLResponse(
        render_template("auth/verify_email_done.html", page_title="Email verified"),
        status_code=200,
    )


def _attach_csrf_token(session: AuthSession) -> str:
    return hmac.new(
        session.secret_hash.encode(),
        f"email-verification:{session.hash}:{session.user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _valid_attach_csrf(session: AuthSession, token: str) -> bool:
    return hmac.compare_digest(token.encode(), _attach_csrf_token(session).encode())


def _code_purpose(session: AuthSession, email: str, code: str) -> str:
    digest = hmac.new(
        session.secret_hash.encode(), f"{session.hash}:{session.user_id}:{email}:{code}".encode(), hashlib.sha256,
    ).hexdigest()
    return f"email_verify_code:{digest}"


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


def _form_page(*, session: AuthSession, user: User, error: str | None) -> str:
    return render_template(
        "auth/email_verification_form.html",
        page_title="Verify your email",
        wallet=session.label if session.provider == "metamask" else None,
        email=user.email or "",
        csrf_token=_attach_csrf_token(session),
        error=error,
    )


def _conflict_page(message: str) -> str:
    return render_template(
        "auth/email_verification_conflict.html", page_title="Email not added", message=message
    )
