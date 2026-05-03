"""MetaMask SIWE sign-in + email-verification step for wallet users.

Three states a wallet user can be in:

1. Fresh — no row, no session. `/v1/auth/wallet/challenge` mints a SIWE
   message; `/v1/auth/wallet/verify` validates the signature, creates a
   user keyed by wallet address, mints a `pending_email` cookie session,
   and tells the frontend to redirect to `/auth/wallet/email`.
2. Wallet-authed but email-pending — has a pending session cookie,
   visited `/auth/wallet/email`, submitted an email; we store it, mint a
   24-hour magic-link token, send via SES.
3. Active — clicked the link, hit `/auth/verify-email?token=…`, session
   was upgraded in place. Console accessible.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from trusted_router.auth import (
    SESSION_COOKIE_NAME,
    SettingsDep,
    set_session_cookie,
)
from trusted_router.errors import api_error
from trusted_router.services.email import build_verification_email, get_email_service
from trusted_router.storage import STORE
from trusted_router.types import ErrorType
from trusted_router.views import render_template
from trusted_router.wallet_auth import (
    ADDRESS_RE,
    build_siwe_message,
    recover_address,
)

CHALLENGE_TTL_SECONDS = 300  # 5 minutes
VERIFICATION_TTL_SECONDS = 60 * 60 * 24  # 24 hours


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
    async def wallet_challenge(
        body: WalletChallengeRequest,
        settings: SettingsDep,
    ) -> JSONResponse:
        if not ADDRESS_RE.match(body.address):
            raise api_error(400, "Invalid Ethereum address", ErrorType.BAD_REQUEST)
        domain = settings.siwe_domain or settings.trusted_domain
        nonce = secrets.token_urlsafe(32)
        message, _ = build_siwe_message(
            domain=domain,
            address=body.address,
            nonce=nonce,
            issued_at=dt.datetime.now(dt.UTC),
            expiration_seconds=CHALLENGE_TTL_SECONDS,
        )
        _, record = STORE.create_wallet_challenge(
            address=body.address,
            message=message,
            ttl_seconds=CHALLENGE_TTL_SECONDS,
            raw_nonce=nonce,
        )
        return JSONResponse(
            {
                "data": {
                    "message": message,
                    "nonce": nonce,
                    "expires_at": record.expires_at,
                }
            }
        )

    @router.post("/auth/wallet/verify")
    async def wallet_verify(
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
            user = STORE.create_wallet_user(body.address)
            redirect = "/auth/wallet/email"
            session_state = "pending_email"
        elif not existing_user.email_verified:
            user = existing_user
            redirect = "/auth/wallet/email"
            session_state = "pending_email"
        else:
            user = existing_user
            redirect = "/console/api-keys"
            session_state = "active"

        raw_token, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="metamask",
            label=body.address.lower(),
            ttl_seconds=settings.auth_session_ttl_seconds,
            state=session_state,
        )
        response = JSONResponse({"data": {"redirect": redirect, "state": session_state}})
        set_session_cookie(response, raw_token, settings)
        return response

    @router.get("/auth/wallet/email")
    async def wallet_email_form(request: Request, settings: SettingsDep) -> HTMLResponse:
        session = _require_pending_session(request)
        return HTMLResponse(_form_page(address=session.label, error=None))

    @router.post("/auth/wallet/email")
    async def wallet_email_submit(
        request: Request,
        settings: SettingsDep,
        email: str = Form(..., min_length=3, max_length=320),
    ) -> HTMLResponse:
        session = _require_pending_session(request)
        email_normalized = email.strip().lower()
        existing = STORE.find_user_by_email(email_normalized)
        if existing is not None and existing.id != session.user_id:
            return HTMLResponse(
                _form_page(
                    address=session.label,
                    error="That email already has an account — sign in with that provider.",
                ),
                status_code=409,
            )
        updated = STORE.set_user_email(session.user_id, email_normalized)
        if updated is None:
            return HTMLResponse(
                _form_page(
                    address=session.label,
                    error="Could not save that email. Try again.",
                ),
                status_code=400,
            )

        raw_token, _ = STORE.create_verification_token(
            user_id=session.user_id,
            purpose="signup",
            ttl_seconds=VERIFICATION_TTL_SECONDS,
        )
        verify_url = _verify_url(request, raw_token)
        message = build_verification_email(
            to=email_normalized,
            verification_url=verify_url,
            from_name=settings.ses_from_name,
        )
        sent = get_email_service(settings).send(message)
        return HTMLResponse(render_template(
            "auth/wallet_email_sent.html",
            page_title="Check your email",
            email=email_normalized,
            dev_link=None if sent else verify_url,
        ))


def _require_pending_session(request: Request) -> Any:
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_token:
        raise api_error(401, "Wallet sign-in required", ErrorType.UNAUTHORIZED)
    session = STORE.get_auth_session_by_raw(cookie_token)
    if session is None or session.provider != "metamask" or session.state != "pending_email":
        raise api_error(401, "Wallet sign-in required", ErrorType.UNAUTHORIZED)
    return session


def _verify_url(request: Request, raw_token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/verify-email?token={raw_token}"


def _form_page(*, address: str, error: str | None) -> str:
    return render_template(
        "auth/wallet_email_form.html",
        page_title="Verify your email",
        address=address,
        error=error,
    )
