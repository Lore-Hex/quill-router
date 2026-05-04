"""Static contract for the storage backend.

`Store` enumerates every public method that route code, services, or auth
relies on. `InMemoryStore` and `SpannerBigtableStore` both implement it,
which lets mypy verify that route code only touches the declared surface
and that the two backends stay signature-compatible — a missing or
drifted method on either implementation becomes a static-typing error
instead of a 4-AM AttributeError.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from trusted_router.storage_models import (
    ApiKey,
    AuthSession,
    ByokProviderConfig,
    CreditAccount,
    EmailSendBlock,
    EncryptedSecretEnvelope,
    GatewayAuthorization,
    Generation,
    Member,
    OAuthAuthorizationCode,
    ProviderBenchmarkSample,
    RateLimitHit,
    Reservation,
    SignupResult,
    User,
    VerificationToken,
    WalletChallenge,
    Workspace,
)
from trusted_router.types import UsageType


@runtime_checkable
class Store(Protocol):
    """Public surface that both InMemoryStore and SpannerBigtableStore satisfy."""

    # Lifecycle ---------------------------------------------------------------
    def reset(self) -> None: ...

    # Users + workspaces ------------------------------------------------------
    def ensure_user(self, user_id: str, email: str | None = ...) -> User: ...
    def find_user_by_email(self, email: str) -> User | None: ...
    def find_user_by_wallet(self, address: str) -> User | None: ...
    def create_wallet_user(self, address: str) -> User: ...
    def set_user_email(self, user_id: str, email: str) -> User | None: ...
    def mark_user_email_verified(self, user_id: str) -> User | None: ...
    def get_user(self, user_id: str) -> User | None: ...
    def signup(
        self,
        *,
        email: str,
        workspace_name: str | None = ...,
    ) -> SignupResult | None: ...
    def create_workspace(
        self,
        owner_user_id: str,
        name: str,
        *,
        trial_credit_microdollars: int | None = ...,
    ) -> Workspace: ...
    def list_workspaces_for_user(self, user_id: str) -> list[Workspace]: ...
    def get_workspace(self, workspace_id: str) -> Workspace | None: ...
    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = ...,
        deleted: bool | None = ...,
    ) -> Workspace | None: ...
    def add_members(
        self, workspace_id: str, emails: list[str], role: str = ...
    ) -> list[Member]: ...
    def remove_members(self, workspace_id: str, user_ids: list[str]) -> None: ...
    def list_members(self, workspace_id: str) -> list[Member]: ...
    def user_can_manage(self, user_id: str, workspace_id: str) -> bool: ...
    def user_is_member(self, user_id: str, workspace_id: str) -> bool: ...

    # Auth sessions -----------------------------------------------------------
    def create_auth_session(
        self,
        *,
        user_id: str,
        provider: str,
        label: str,
        ttl_seconds: int,
        workspace_id: str | None = ...,
        state: str = ...,
    ) -> tuple[str, AuthSession]: ...
    def get_auth_session_by_raw(self, raw_token: str) -> AuthSession | None: ...
    def delete_auth_session_by_raw(self, raw_token: str) -> bool: ...
    def upgrade_auth_session(
        self, raw_token: str, *, state: str
    ) -> AuthSession | None: ...
    def set_auth_session_workspace(
        self, raw_token: str, workspace_id: str
    ) -> AuthSession | None: ...

    # Wallet challenges (SIWE) ------------------------------------------------
    def create_wallet_challenge(
        self,
        *,
        address: str,
        message: str,
        ttl_seconds: int,
        raw_nonce: str | None = ...,
    ) -> tuple[str, WalletChallenge]: ...
    def consume_wallet_challenge(self, raw_nonce: str) -> WalletChallenge | None: ...

    # Email verification tokens -----------------------------------------------
    def create_verification_token(
        self,
        *,
        user_id: str,
        purpose: str,
        ttl_seconds: int,
    ) -> tuple[str, VerificationToken]: ...
    def consume_verification_token(
        self, raw_token: str, *, purpose: str
    ) -> VerificationToken | None: ...
    def create_oauth_authorization_code(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
        callback_url: str,
        key_label: str,
        ttl_seconds: int,
        app_id: int,
        limit_microdollars: int | None = ...,
        limit_reset: str | None = ...,
        expires_at: str | None = ...,
        code_challenge: str | None = ...,
        code_challenge_method: str | None = ...,
        spawn_agent: str | None = ...,
        spawn_cloud: str | None = ...,
    ) -> tuple[str, OAuthAuthorizationCode]: ...
    def consume_oauth_authorization_code(
        self, raw_code: str
    ) -> OAuthAuthorizationCode | None: ...

    # Email send blocks (SES bounce/complaint suppression) -------------------
    def block_email_sending(
        self,
        *,
        email: str,
        reason: str,
        bounce_type: str | None = ...,
        feedback_id: str | None = ...,
    ) -> EmailSendBlock: ...
    def is_email_blocked(self, email: str) -> bool: ...
    def get_email_block(self, email: str) -> EmailSendBlock | None: ...
    def record_sns_message_once(self, message_id: str) -> bool: ...

    # API keys ----------------------------------------------------------------
    def create_api_key(
        self,
        *,
        workspace_id: str,
        name: str,
        creator_user_id: str | None,
        management: bool = ...,
        raw_key: str | None = ...,
        limit_microdollars: int | None = ...,
        limit_reset: str | None = ...,
        include_byok_in_limit: bool = ...,
        expires_at: str | None = ...,
    ) -> tuple[str, ApiKey]: ...
    def get_key_by_hash(self, key_hash: str) -> ApiKey | None: ...
    def get_key_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None: ...
    def get_key_by_raw(self, raw_key: str) -> ApiKey | None: ...
    def list_keys(self, workspace_id: str) -> list[ApiKey]: ...
    def delete_key(self, key_hash: str) -> bool: ...
    def update_key(self, key_hash: str, patch: dict[str, Any]) -> ApiKey | None: ...
    def reserve_key_limit(
        self,
        key_hash: str,
        amount_microdollars: int,
        *,
        usage_type: UsageType | str,
    ) -> None: ...
    def settle_key_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        actual_microdollars: int,
        *,
        usage_type: UsageType | str,
    ) -> None: ...
    def refund_key_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: UsageType | str,
    ) -> None: ...

    # BYOK --------------------------------------------------------------------
    def upsert_byok_provider(
        self,
        *,
        workspace_id: str,
        provider: str,
        secret_ref: str,
        key_hint: str | None,
        encrypted_secret: EncryptedSecretEnvelope | None = ...,
    ) -> ByokProviderConfig: ...
    def list_byok_providers(self, workspace_id: str) -> list[ByokProviderConfig]: ...
    def get_byok_provider(
        self, workspace_id: str, provider: str
    ) -> ByokProviderConfig | None: ...
    def delete_byok_provider(self, workspace_id: str, provider: str) -> bool: ...

    # Credit ledger -----------------------------------------------------------
    def get_credit_account(self, workspace_id: str) -> CreditAccount | None: ...
    def credit_workspace_once(
        self, workspace_id: str, amount_microdollars: int, event_id: str
    ) -> bool: ...
    def reserve(
        self, workspace_id: str, key_hash: str, amount_microdollars: int
    ) -> Reservation: ...
    def settle(self, reservation_id: str, actual_microdollars: int) -> None: ...
    def refund(self, reservation_id: str) -> None: ...
    def update_auto_refill_settings(
        self,
        workspace_id: str,
        *,
        enabled: bool,
        threshold_microdollars: int,
        amount_microdollars: int,
    ) -> CreditAccount | None: ...
    def set_stripe_customer(
        self,
        workspace_id: str,
        *,
        customer_id: str,
        payment_method_id: str | None = ...,
    ) -> CreditAccount | None: ...
    def record_auto_refill_outcome(
        self, workspace_id: str, *, status: str
    ) -> CreditAccount | None: ...

    # Gateway authorizations --------------------------------------------------
    def create_gateway_authorization(
        self,
        *,
        workspace_id: str,
        key_hash: str,
        model_id: str,
        provider: str,
        usage_type: UsageType | str,
        estimated_microdollars: int,
        credit_reservation_id: str | None,
        requested_model_id: str | None = ...,
        candidate_model_ids: list[str] | None = ...,
        region: str | None = ...,
        endpoint_id: str | None = ...,
        candidate_endpoint_ids: list[str] | None = ...,
    ) -> GatewayAuthorization: ...
    def get_gateway_authorization(
        self, authorization_id: str
    ) -> GatewayAuthorization | None: ...
    def mark_gateway_authorization_settled(self, authorization_id: str) -> None: ...
    def finalize_gateway_authorization(
        self,
        authorization_id: str,
        *,
        success: bool,
        actual_microdollars: int,
        selected_usage_type: UsageType | str,
        generation: Generation | None = ...,
    ) -> bool: ...

    # Generations + activity --------------------------------------------------
    def add_generation(self, generation: Generation) -> None: ...
    def record_provider_benchmark(self, sample: ProviderBenchmarkSample) -> None: ...
    def provider_benchmark_samples(
        self,
        *,
        date: str | None = ...,
        provider: str | None = ...,
        model: str | None = ...,
        limit: int = ...,
    ) -> list[ProviderBenchmarkSample]: ...
    def get_generation(self, generation_id: str) -> Generation | None: ...
    def activity(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = ...,
        date: str | None = ...,
    ) -> list[dict[str, Any]]: ...
    def activity_events(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = ...,
        date: str | None = ...,
        limit: int = ...,
    ) -> list[dict[str, Any]]: ...

    # Rate limiting -----------------------------------------------------------
    def hit_rate_limit(
        self,
        *,
        namespace: str,
        subject: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitHit: ...
