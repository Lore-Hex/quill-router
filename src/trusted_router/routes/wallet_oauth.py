"""MetaMask SIWE sign-in.

Wallet users authenticate fully with a signed SIWE challenge. We do not
require email before creating an active console session: crypto-native
users expect wallet-only auth. Wallet-only accounts start at $0; the starter
credit is reserved for email and OAuth signups.

Optional mailbox confirmation is handled by routes.email_verify.
"""

from __future__ import annotations

import datetime as dt
import secrets
import threading

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from trusted_router.acquisition import (
    record_signup_attribution,
    same_site_referer_path,
)
from trusted_router.auth import (
    SettingsDep,
    set_session_cookie,
)
from trusted_router.config import Settings
from trusted_router.domains import request_control_domain
from trusted_router.errors import api_error
from trusted_router.request_limits import (
    UNTRUSTED_LOAD_BALANCER_SUBJECT,
    fingerprint_subject,
    normalized_client_identity,
    rate_limit_headers,
)
from trusted_router.signup_gate import (
    require_wallet_account_creation,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import (
    RateLimitHit,
    WalletChallenge,
)
from trusted_router.storage_rate_limits import InMemoryRateLimits
from trusted_router.storage_wallet_challenges import parse_siwe_challenge_scope
from trusted_router.types import ErrorType
from trusted_router.wallet_auth import (
    ADDRESS_RE,
    build_siwe_message,
    recover_address,
)

CHALLENGE_TTL_SECONDS = 300  # 5 minutes


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
