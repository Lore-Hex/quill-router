"""MetaMask SIWE sign-in.

Wallet users authenticate fully with a signed SIWE challenge. We do not
require email before creating an active console session: crypto-native
users expect wallet-only auth. Wallet-only accounts start at $0; the starter
credit is reserved for email and OAuth signups.

`/auth/wallet/email` lets a wallet session (active, or a legacy
`pending_email` one) attach an email. Wallet-only accounts need a verified
email to unlock non-stablecoin billing and BYOK, so this is the path the
console points them to. The address is NOT written to the account when the
form is submitted. Attaching it requires the emailed code in that same
wallet session. If another account owns the address, the mailbox gets a
notice rather than a code, with the same response to the requesting wallet.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import threading

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
from trusted_router.config import Settings
from trusted_router.domains import request_control_domain
from trusted_router.errors import api_error
from trusted_router.request_limits import (
    UNTRUSTED_LOAD_BALANCER_SUBJECT,
    fingerprint_subject,
    normalized_client_identity,
    rate_limit_headers,
)
from trusted_router.services.email import (
    build_email_in_use_notice,
    build_wallet_email_code,
    get_email_service,
)
from trusted_router.signup_gate import (
    is_disposable_email,
    require_wallet_account_creation,
)
from trusted_router.storage import STORE, AuthSession, User
from trusted_router.storage_models import (
    EMAIL_ATTACH_ALREADY_VERIFIED,
    EMAIL_ATTACH_ATTACHED,
    EMAIL_ATTACH_TAKEN,
    RateLimitHit,
    WalletChallenge,
)
from trusted_router.storage_rate_limits import InMemoryRateLimits
from trusted_router.storage_wallet_challenges import parse_siwe_challenge_scope
from trusted_router.types import ErrorType
from trusted_router.views import render_template
from trusted_router.wallet_auth import (
    ADDRESS_RE,
    build_siwe_message,
    recover_address,
)

CHALLENGE_TTL_SECONDS = 300  # 5 minutes
VERIFICATION_TTL_SECONDS = 10 * 60
WALLET_EMAIL_VERIFY_PATH = "/auth/wallet/email/verify"
_ATTACH_SESSION_STATES = frozenset({"active", "pending_email"})
_ATTACH_SENDS_PER_HOUR = 5
_ATTACH_TRIES_PER_WINDOW = 5


class WalletChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address: str = Field(min_length=42, max_length=42)


class WalletVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address: str = Field(min_length=42, max_length=42)
    signature: str = Field(min_length=1, max_length=132)
    nonce: str = Field(min_length=1, max_length=128)


def register_wallet_oauth_routes(router: APIRouter) -> None:
    challenges = InMemoryRateLimits(lock=threading.RLock())

    def require_capacity(hit: RateLimitHit) -> None:
        if not hit.allowed:
            raise api_error(
                429, "Too many wallet requests. Please try again later.",
                ErrorType.RATE_LIMITED, headers=rate_limit_headers(hit),
            )

    def wallet_domain(request: Request, settings: Settings) -> str:
        domain = request_control_domain(request, settings)
        origin = request.headers.get("origin")
        if origin and origin not in {f"https://{domain}", f"https://www.{domain}"} and not (
            str(settings.environment).lower() in {"local", "test"} and origin == str(request.base_url).rstrip("/")
        ):
            raise api_error(403, "Wallet sign-in requires a first-party origin", ErrorType.FORBIDDEN)
        if domain == settings.trusted_domain and settings.siwe_domain:
            return settings.siwe_domain
        return domain

    @router.post("/auth/wallet/challenge")
    def wallet_challenge(
        request: Request,
        body: WalletChallengeRequest,
        settings: SettingsDep,
    ) -> JSONResponse:
        if not ADDRESS_RE.match(body.address):
            raise api_error(400, "Invalid Ethereum address", ErrorType.BAD_REQUEST)
        domain = wallet_domain(request, settings)
        if STORE.find_user_by_wallet(body.address) is None:
            require_wallet_account_creation(settings)
            # Bound durable writes even when the edge supplies no usable IP.
            source = normalized_client_identity(request, settings)
            if source != UNTRUSTED_LOAD_BALANCER_SUBJECT:
                require_capacity(challenges.hit(
                    namespace="wallet_new_challenge", subject=fingerprint_subject(source),
                    limit=6, window_seconds=60,
                ))
            require_capacity(challenges.hit(
                namespace="wallet_new_challenge_all", subject="all", limit=30, window_seconds=60,
            ))
        nonce = secrets.token_hex(32)
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
        domain = wallet_domain(request, settings)
        address = body.address.lower()

        def accepts(record: WalletChallenge) -> bool:
            if record.address != address or parse_siwe_challenge_scope(record.message) != (domain, body.nonce):
                return False
            try:
                return recover_address(message=record.message, signature=body.signature) == address
            except Exception:  # eth-account raises several malformed-signature exceptions.
                return False

        challenge = STORE.consume_wallet_challenge(body.nonce, accepts=accepts)
        if challenge is None:
            raise api_error(400, "Invalid signature or expired challenge", ErrorType.BAD_REQUEST)

        existing_user = STORE.find_user_by_wallet(body.address)
        if existing_user is None:
            require_wallet_account_creation(settings)
            # Count only proofs of possession. Store-backed quotas span replicas.
            source = normalized_client_identity(request, settings)
            if source != UNTRUSTED_LOAD_BALANCER_SUBJECT:
                require_capacity(STORE.hit_rate_limit(
                    namespace="wallet_enrollment", subject=fingerprint_subject(source),
                    limit=3, window_seconds=3600,
                ))
            require_capacity(STORE.hit_rate_limit(
                namespace="wallet_enrollment_all", subject="all", limit=12, window_seconds=600,
            ))
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
        if not _valid_attach_csrf(session, csrf_token):
            return HTMLResponse(
                _form_page(session=session, error="This form expired. Try again."),
                status_code=403,
            )
        if _has_verified_email(user):
            return _already_verified_response(session)
        email_normalized = email.strip().lower()
        if not _plausible_email(email_normalized) or is_disposable_email(email_normalized):
            return HTMLResponse(
                _form_page(session=session, error="Enter a valid, non-disposable email address."),
                status_code=400,
            )
        # Counted before the ownership check, so a taken address costs the
        # same send as a free one and the cap cannot tell them apart.
        if not STORE.hit_rate_limit(
            namespace="wallet_email_send_user", subject=fingerprint_subject(user.id),
            limit=_ATTACH_SENDS_PER_HOUR, window_seconds=3600,
        ).allowed:
            return HTMLResponse(
                _form_page(
                    session=session,
                    error="Too many verification emails. Try again in an hour.",
                ),
                status_code=429,
            )

        # Do not expose that other wallets have recently requested this address.
        destination = STORE.hit_rate_limit(
            namespace="wallet_email_send_recipient", subject=fingerprint_subject(email_normalized),
            limit=_ATTACH_SENDS_PER_HOUR, window_seconds=3600,
        )
        if not destination.allowed:
            return _code_form(session, email_normalized, secrets.token_urlsafe(32))
        existing = STORE.find_user_by_email(email_normalized)
        if existing is not None and existing.id != user.id:
            challenge = secrets.token_urlsafe(32)
            # The response must not say the address has an account: any wallet
            # session could then test addresses one by one. The mailbox owner
            # learns it from this notice instead, and nothing is attached.
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
            message = build_wallet_email_code(
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
                    session=session,
                    error="We could not send a verification email. Try again later.",
                ),
                status_code=503,
            )
        return _code_form(session, email_normalized, challenge)

    @router.get(WALLET_EMAIL_VERIFY_PATH)
    def wallet_email_legacy_link() -> Response:
        # Old email links, including scanner previews, must never consume tokens.
        return HTMLResponse(
            render_template("auth/wallet_email_conflict.html", page_title="Request a code",
                            message="Verification links have been retired. Request a code in your wallet session."),
            status_code=410, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @router.post(WALLET_EMAIL_VERIFY_PATH)
    def wallet_email_verify(
        request: Request, settings: SettingsDep,
        email: str = Form(..., min_length=3, max_length=320),
        challenge: str = Form(..., min_length=1, max_length=128),
        code: str = Form(..., min_length=1, max_length=32),
        csrf_token: str = Form("", max_length=128),
    ) -> Response:
        session, user = _require_wallet_session(request)
        if not _valid_attach_csrf(session, csrf_token):
            return HTMLResponse(_form_page(session=session, error="This form expired. Try again."), status_code=403)
        email_normalized = email.strip().lower()
        require_capacity(STORE.hit_rate_limit(
            namespace="wallet_email_verify_user", subject=fingerprint_subject(user.id),
            limit=_ATTACH_TRIES_PER_WINDOW, window_seconds=VERIFICATION_TTL_SECONDS,
        ))
        code = code.strip()
        consumed = (
            STORE.consume_verification_token(challenge, purpose=_code_purpose(session, email_normalized, code))
            if _plausible_email(email_normalized) and not is_disposable_email(email_normalized)
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
            return HTMLResponse(_invalid_page(), status_code=400)

        if session.state == "pending_email":
            cookie_token = request.cookies[SESSION_COOKIE_NAME]
            if STORE.upgrade_auth_session(cookie_token, state="active") is None:
                return HTMLResponse(render_template("auth/verify_email_done.html", page_title="Email verified"))
            response = RedirectResponse(url="/console/welcome?first=1", status_code=302)
            set_session_cookie(response, cookie_token, settings)
            return response
        return RedirectResponse(url="/console/credits", status_code=302)


def _code_form(session: AuthSession, email: str, challenge: str, *, error: str | None = None) -> HTMLResponse:
    return HTMLResponse(render_template(
        "auth/wallet_email_sent.html", page_title="Verify your email",
        email=email, challenge=challenge, csrf_token=_attach_csrf_token(session), error=error,
    ), status_code=400 if error else 200, headers={"Cache-Control": "no-store"})


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


def _valid_attach_csrf(session: AuthSession, token: str) -> bool:
    return hmac.compare_digest(token.encode(), _attach_csrf_token(session).encode())


def _code_purpose(session: AuthSession, email: str, code: str) -> str:
    digest = hmac.new(
        session.secret_hash.encode(), f"{session.hash}:{session.user_id}:{email}:{code}".encode(), hashlib.sha256,
    ).hexdigest()
    return f"wallet_email_code:{digest}"


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
