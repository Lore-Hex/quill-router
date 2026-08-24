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

from trusted_router.operational_analytics_freshness import OutboxFreshness
from trusted_router.storage_models import (
    AcquisitionAttribution,
    ActivationReminderTask,
    ApiKey,
    ApiKeyAuthContext,
    ApiKeyUsageSnapshot,
    AuthSession,
    BedrockGroupBuyAggregate,
    BedrockGroupBuyPledge,
    BedrockGroupBuyPublicMessage,
    BroadcastDeliveryJob,
    BroadcastDestination,
    ByokProviderConfig,
    CreditAccount,
    CreditMovement,
    CreditTransfer,
    CustomModel,
    EmailSendBlock,
    EncryptedSecretEnvelope,
    GatewayAuthorization,
    Generation,
    GoogleAdsConversion,
    Member,
    OAuthAuthorizationCode,
    ProviderAccessGrant,
    ProviderBenchmarkSample,
    RateLimitHit,
    Reservation,
    SessionAuthContext,
    SignupResult,
    SyntheticProbeSample,
    SyntheticRollup,
    User,
    UserModelPayout,
    UserProvidedModel,
    VerificationToken,
    VideoJob,
    WalletChallenge,
    Workspace,
)
from trusted_router.types import UsageType


@runtime_checkable
class Store(Protocol):
    """Public surface that both InMemoryStore and SpannerBigtableStore satisfy."""

    # Lifecycle ---------------------------------------------------------------
    def reset(self) -> None: ...
    def readiness_check(self) -> None: ...

    # Users + workspaces ------------------------------------------------------
    def ensure_user(
        self,
        user_id: str,
        email: str | None = ...,
        *,
        trial_credit_microdollars: int | None = ...,
    ) -> User: ...
    def find_user_by_email(self, email: str) -> User | None: ...
    def find_user_by_wallet(self, address: str) -> User | None: ...
    def create_wallet_user(self, address: str) -> User: ...
    def set_user_email(self, user_id: str, email: str) -> User | None: ...
    def mark_user_email_verified(self, user_id: str) -> User | None: ...
    def set_user_identity_status(
        self,
        user_id: str,
        *,
        status: str,
        session_id: str | None = ...,
        session_url: str | None = ...,
        decision_code: int | None = ...,
        decision_reason: str | None = ...,
        decision_reason_code: int | None = ...,
        verified_name: str | None = ...,
        increment_attempts: bool = ...,
    ) -> User | None: ...
    # Phone ownership proof for notifications. The rules live in
    # phone_verification.py; a backend only reads the user, applies them, and
    # writes it back, so the three stores cannot drift apart on policy.
    def begin_phone_verification(
        self, user_id: str, phone: str, channel: str | None = ...
    ) -> tuple[str, User] | None: ...
    def confirm_phone_verification(self, user_id: str, code: str) -> tuple[str, User | None]: ...
    def cancel_phone_verification(self, user_id: str) -> User | None: ...
    def clear_user_phone(self, user_id: str) -> User | None: ...
    def get_user(self, user_id: str) -> User | None: ...
    def signup(
        self,
        *,
        email: str,
        workspace_name: str | None = ...,
        trial_credit_microdollars: int = ...,
    ) -> SignupResult | None: ...
    def create_acquisition_attribution(self, record: AcquisitionAttribution) -> bool: ...
    def get_acquisition_attribution(self, workspace_id: str) -> AcquisitionAttribution | None: ...
    def claim_acquisition_milestones(
        self,
        workspace_id: str,
        milestones: list[str],
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, list[str]]: ...
    def record_acquisition_purchase(
        self,
        workspace_id: str,
        *,
        amount_microdollars: int,
        occurred_at: str,
    ) -> AcquisitionAttribution | None: ...
    def repair_google_ads_delivery_queue(self, *, since: str, limit: int) -> int: ...
    def purge_expired_google_ads_click_ids(self, *, before: str, limit: int) -> int: ...
    def claim_google_ads_deliveries(
        self,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[GoogleAdsConversion]: ...
    def mark_google_ads_delivery_submitted(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        request_id: str,
    ) -> GoogleAdsConversion | None: ...
    def mark_google_ads_delivery_failed(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        error: str,
        retryable: bool,
        max_attempts: int,
    ) -> GoogleAdsConversion | None: ...
    def list_activation_reminders(self, *, limit: int = ...) -> list[ActivationReminderTask]: ...
    def delete_activation_reminders(self, reminder_ids: list[str]) -> None: ...
    def claim_activation_reminder(
        self,
        workspace_id: str,
        stage: str,
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, bool]: ...
    def upsert_bedrock_group_buy_pledge(
        self, pledge: BedrockGroupBuyPledge
    ) -> BedrockGroupBuyPledge: ...
    def get_bedrock_group_buy_pledge(self, user_id: str) -> BedrockGroupBuyPledge | None: ...
    def withdraw_bedrock_group_buy_pledge(self, user_id: str) -> bool: ...
    def bedrock_group_buy_aggregate(self) -> BedrockGroupBuyAggregate: ...
    def list_bedrock_group_buy_public_messages(
        self, *, limit: int = ...
    ) -> list[BedrockGroupBuyPublicMessage]: ...
    def list_bedrock_group_buy_private_pledges(
        self, *, limit: int = ...
    ) -> list[BedrockGroupBuyPledge]: ...
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
        billing_paused: bool | None = ...,
        billing_pause_reason: str | None = ...,
    ) -> Workspace | None: ...
    def add_members(
        self, workspace_id: str, emails: list[str], role: str = ...
    ) -> list[Member]: ...
    def remove_members(self, workspace_id: str, user_ids: list[str]) -> None: ...
    def list_members(self, workspace_id: str) -> list[Member]: ...
    def user_can_manage(self, user_id: str, workspace_id: str) -> bool: ...
    def user_is_member(self, user_id: str, workspace_id: str) -> bool: ...
    def grant_provider_access(
        self,
        user_id: str,
        provider: str,
        *,
        role: str = ...,
    ) -> ProviderAccessGrant: ...
    def list_provider_access_for_user(self, user_id: str) -> list[ProviderAccessGrant]: ...
    def revoke_provider_access(self, user_id: str, provider: str) -> bool: ...

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
    def session_auth_context(
        self,
        raw_token: str,
        *,
        requested_workspace_id: str | None = ...,
    ) -> SessionAuthContext | None: ...
    def upgrade_auth_session(self, raw_token: str, *, state: str) -> AuthSession | None: ...
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
    def consume_oauth_authorization_code(self, raw_code: str) -> OAuthAuthorizationCode | None: ...

    # Email send blocks (SES bounce/complaint suppression) -------------------
    def block_email_sending(
        self,
        *,
        email: str,
        reason: str,
        bounce_type: str | None = ...,
        feedback_id: str | None = ...,
        mail_class: str | None = ...,
        sender_profile: str | None = ...,
        acquisition_source: str | None = ...,
        acquisition_medium: str | None = ...,
        acquisition_campaign: str | None = ...,
    ) -> EmailSendBlock: ...
    def is_email_blocked(self, email: str) -> bool: ...
    def get_email_block(self, email: str) -> EmailSendBlock | None: ...
    def record_sns_message_once(self, message_id: str) -> bool: ...
    def record_webhook_event_once(self, source: str, event_id: str) -> bool: ...

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
        limit_daily_microdollars: int | None = ...,
        limit_weekly_microdollars: int | None = ...,
        limit_monthly_microdollars: int | None = ...,
        budget_alert_only: bool = ...,
        tags: dict[str, str] | None = ...,
    ) -> tuple[str, ApiKey]: ...
    def get_key_by_hash(self, key_hash: str) -> ApiKey | None: ...
    def typed_key_usage(
        self,
        key_hash: str,
        *,
        allow_stale: bool = ...,
    ) -> dict[str, Any] | None: ...
    def get_key_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None: ...

    def upsert_federated_api_key(self, record: dict[str, Any]) -> ApiKey:
        """Persist an identity-only key record resolved from a home plane.

        No secret material, no credits: a federated key seeds at ZERO
        local balance. Copying a balance would mint money.

        Materializes the SHADOW workspace atomically with the key — a key
        whose workspace is missing 403s on every request, so the two must
        never exist apart.
        """
        ...

    # Cross-plane credit transfer ---------------------------------------------
    # The state machine, which plane holds the value in each state, and the
    # conservation invariant live in `trusted_router.credit_transfer`. A store
    # may act as SOURCE (open/resolve) and DESTINATION (claim) at once.
    def open_credit_transfer(
        self,
        *,
        transfer_id: str,
        workspace_id: str,
        amount_microdollars: int,
        destination: str,
    ) -> CreditTransfer:
        """SOURCE: conditionally debit into escrow, idempotent on transfer_id.

        Raises ValueError("insufficient credits") rather than overdrawing.
        """
        ...

    def get_credit_transfer(self, transfer_id: str) -> CreditTransfer | None: ...
    def list_open_credit_transfers(
        self, limit: int = ..., *, after_id: str = ...
    ) -> list[CreditTransfer]:
        """SOURCE: the recovery queue, in id order, starting after `after_id`.

        Paged rather than "the first N", because a transfer the recovery pass
        must SKIP (one escrowed for a different destination) never leaves the
        queue. Without a cursor, enough such rows sorting ahead of the live
        ones would hide every later escrow from recovery forever.
        """
        ...

    def resolve_credit_transfer(self, *, transfer_id: str, outcome: str) -> CreditTransfer:
        """SOURCE: record the destination's verdict; never invent one.

        Repeating the same verdict is a no-op; a disagreeing one raises
        CreditTransferConflict instead of moving value a second time.
        """
        ...

    def claim_credit_transfer(
        self,
        *,
        transfer_id: str,
        workspace_id: str,
        amount_microdollars: int,
        source: str,
        accept: bool,
    ) -> str:
        """DESTINATION: decide once. Returns the DECIDED outcome, which may
        differ from `accept` when another caller got there first."""
        ...

    def get_key_by_raw(self, raw_key: str) -> ApiKey | None: ...
    def api_key_auth_context(self, raw_key: str) -> ApiKeyAuthContext | None: ...
    def list_keys(self, workspace_id: str) -> list[ApiKey]: ...
    def list_api_keys_with_usage(self, workspace_id: str) -> list[ApiKeyUsageSnapshot]: ...
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
    def get_byok_provider(self, workspace_id: str, provider: str) -> ByokProviderConfig | None: ...
    def delete_byok_provider(self, workspace_id: str, provider: str) -> bool: ...

    # Custom models -----------------------------------------------------------
    def create_custom_model(
        self,
        *,
        owner_user_id: str,
        owner_workspace_id: str,
        name: str,
        base_model_id: str,
        hidden_prompt: str,
        enabled: bool = ...,
        slug: str | None = ...,
    ) -> CustomModel: ...
    def list_custom_models_for_user(self, owner_user_id: str) -> list[CustomModel]: ...
    def get_custom_model(self, model_id: str) -> CustomModel | None: ...
    def update_custom_model(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> CustomModel | None: ...
    def delete_custom_model(self, model_id: str, *, owner_user_id: str) -> bool: ...

    # User-provided models ----------------------------------------------------
    def create_user_model(
        self,
        *,
        owner_user_id: str,
        owner_workspace_id: str,
        name: str,
        kind: str,
        description: str = ...,
        display_identity: str = ...,
        display_name: str = ...,
        endpoint_url: str,
        upstream_model_id: str | None = ...,
        encrypted_endpoint_api_key: EncryptedSecretEnvelope | None = ...,
        endpoint_key_hint: str | None = ...,
        encrypted_signing_secret: EncryptedSecretEnvelope | None = ...,
        supports_streaming: bool = ...,
        heartbeat_interval_seconds: int | None = ...,
        max_concurrency: int = ...,
        prompt_price_microdollars_per_million_tokens: int = ...,
        completion_price_microdollars_per_million_tokens: int = ...,
        human_verified: bool = ...,
        enabled: bool = ...,
        status: str = ...,
        slug: str | None = ...,
    ) -> UserProvidedModel: ...
    def list_user_models_for_user(self, owner_user_id: str) -> list[UserProvidedModel]: ...
    def get_user_model(self, model_id: str) -> UserProvidedModel | None: ...
    def get_user_models_by_ids(
        self,
        model_ids: list[str],
    ) -> dict[str, UserProvidedModel]: ...
    def update_user_model(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> UserProvidedModel: ...
    def delete_user_model(self, model_id: str, *, owner_user_id: str) -> bool: ...
    def set_user_model_online(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        online: bool,
    ) -> UserProvidedModel: ...
    def record_user_model_heartbeat(
        self,
        model_id: str,
        *,
        expires_at: str,
    ) -> UserProvidedModel: ...
    def record_user_model_probe(
        self,
        model_id: str,
        *,
        status: str,
        checked_at: str,
    ) -> UserProvidedModel: ...
    def record_user_model_dispatch_result(
        self,
        model_id: str,
        *,
        success: bool,
    ) -> UserProvidedModel: ...
    def acquire_user_model_slot(
        self,
        model_id: str,
        authorization_id: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> bool: ...
    def release_user_model_slot(self, model_id: str, authorization_id: str) -> None: ...
    def list_public_user_models(
        self,
        *,
        kind: str | None = ...,
    ) -> list[UserProvidedModel]: ...

    # Broadcast destinations -------------------------------------------------
    def create_broadcast_destination(
        self,
        *,
        workspace_id: str,
        type: str,
        name: str,
        endpoint: str,
        enabled: bool = ...,
        include_content: bool = ...,
        method: str = ...,
        encrypted_api_key: EncryptedSecretEnvelope | None = ...,
        encrypted_headers: EncryptedSecretEnvelope | None = ...,
        header_names: list[str] | None = ...,
    ) -> BroadcastDestination: ...
    def list_broadcast_destinations(self, workspace_id: str) -> list[BroadcastDestination]: ...
    def get_broadcast_destination(
        self, workspace_id: str, destination_id: str
    ) -> BroadcastDestination | None: ...
    def update_broadcast_destination(
        self,
        workspace_id: str,
        destination_id: str,
        **patch: Any,
    ) -> BroadcastDestination | None: ...
    def delete_broadcast_destination(self, workspace_id: str, destination_id: str) -> bool: ...
    def enqueue_broadcast_delivery(
        self,
        *,
        workspace_id: str,
        destination_id: str,
        generation_id: str,
        settle_body: dict[str, Any],
    ) -> BroadcastDeliveryJob: ...
    def due_broadcast_deliveries(self, *, limit: int = ...) -> list[BroadcastDeliveryJob]: ...
    def claim_broadcast_deliveries(
        self,
        *,
        limit: int = ...,
        lease_seconds: int = ...,
    ) -> list[BroadcastDeliveryJob]: ...
    def mark_broadcast_delivery(
        self,
        job_id: str,
        *,
        success: bool,
        error: str | None = ...,
        lease_owner: str | None = ...,
    ) -> BroadcastDeliveryJob | None: ...

    # Asynchronous video jobs -----------------------------------------------
    def prepare_video_job(self, job: VideoJob) -> tuple[VideoJob, bool]: ...
    def get_video_job(self, job_id: str) -> VideoJob | None: ...
    def get_video_job_for_key(self, job_id: str, key_hash: str) -> VideoJob | None: ...
    def mark_video_job_queued(
        self,
        job_id: str,
        *,
        provider_job_id: str,
        provider: str,
        endpoint_id: str,
        provider_model: str,
        quoted_microdollars: int,
        poll_after_seconds: int,
    ) -> VideoJob | None: ...
    def claim_video_jobs(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[VideoJob]: ...
    def update_video_job(
        self,
        job_id: str,
        *,
        status: str,
        lease_owner: str | None = ...,
        provider_status: str | None = ...,
        generation_id: str | None = ...,
        error: str | None = ...,
        poll_after_seconds: int = ...,
    ) -> VideoJob | None: ...
    def mark_video_job_cleaned(self, job_id: str) -> VideoJob | None: ...

    # Credit ledger -----------------------------------------------------------
    def get_credit_account(self, workspace_id: str) -> CreditAccount | None: ...
    def credit_workspace_typed_direct(
        self,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        lifetime_topup_user_id: str | None = ...,
    ) -> bool: ...
    def credit_workspace_once(
        self, workspace_id: str, amount_microdollars: int, event_id: str
    ) -> bool: ...

    # Earnings & movement primitives -----------------------------------------
    def debit_workspace_guarded(
        self,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        kind: str,
        custom_model_id: str | None = ...,
        authorization_id: str | None = ...,
    ) -> str: ...
    def credit_user_earnings(
        self,
        user_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        custom_model_id: str | None = ...,
        payer_workspace_id: str | None = ...,
    ) -> bool: ...
    def transfer_earnings_to_workspace(
        self,
        user_id: str,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> str: ...
    def ensure_earnings_account(self, user_id: str) -> None: ...
    def earnings_summary(
        self,
        user_id: str,
        *,
        allow_stale: bool = ...,
    ) -> dict[str, int]: ...
    def list_credit_movements(
        self,
        account_id: str,
        *,
        kinds: list[str] | None = ...,
        limit: int = ...,
        before: str | None = ...,
    ) -> list[CreditMovement]: ...
    def custom_model_earnings_by_model(
        self,
        user_id: str,
        *,
        since: str,
    ) -> dict[str, int]: ...
    def add_lifetime_topup(
        self,
        user_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> bool: ...
    def get_lifetime_topup_microdollars(
        self,
        user_id: str,
        *,
        allow_stale: bool = ...,
    ) -> int: ...

    def reserve(
        self,
        workspace_id: str,
        key_hash: str,
        amount_microdollars: int,
        *,
        idempotency_key: str | None = ...,
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
    def clear_stripe_payment_method(self, workspace_id: str) -> CreditAccount | None: ...
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
        authorization_id: str | None = ...,
        requested_model_id: str | None = ...,
        candidate_model_ids: list[str] | None = ...,
        region: str | None = ...,
        endpoint_id: str | None = ...,
        candidate_endpoint_ids: list[str] | None = ...,
        idempotency_key: str | None = ...,
        tags: dict[str, str] | None = ...,
        idempotency_fingerprint: str | None = ...,
        custom_model_id: str | None = ...,
        custom_model_revision: int | None = ...,
        user_provided_model_id: str | None = ...,
        user_provided_model_revision: int | None = ...,
        user_model_prompt_price_microdollars_per_m: int | None = ...,
        user_model_completion_price_microdollars_per_m: int | None = ...,
        user_model_owner_user_id: str | None = ...,
        additional_cost_reservation_microdollars: int = ...,
        native_batch_eligible: bool = ...,
        # Deferred settlement. `settlement="deferred_home"` records that this
        # spend is debt owed to the home plane's ledger rather than a debit
        # here; `expires_at` is what lets the reaper reclaim its admitted
        # estimate if settle never arrives. `deferred_cap_microdollars`, when
        # given, makes this call the ADMISSION point: the outstanding counter
        # moves in the same transaction as the authorization insert, and
        # DeferredSettlementCapReached is raised if the cap refuses.
        settlement: str = ...,
        expires_at: str | None = ...,
        deferred_cap_microdollars: int | None = ...,
    ) -> GatewayAuthorization: ...
    def get_gateway_authorization(self, authorization_id: str) -> GatewayAuthorization | None: ...
    def get_gateway_authorization_by_idempotency_key(
        self, workspace_id: str, key_hash: str, idempotency_key: str
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
    def record_client_events_batch(self, payload: dict[str, Any]) -> None: ...
    def record_provider_benchmark(self, sample: ProviderBenchmarkSample) -> None: ...
    def provider_benchmark_samples(
        self,
        *,
        date: str | None = ...,
        provider: str | None = ...,
        model: str | None = ...,
        limit: int = ...,
    ) -> list[ProviderBenchmarkSample]: ...
    def record_synthetic_probe_sample(self, sample: SyntheticProbeSample) -> None: ...
    def synthetic_probe_samples(
        self,
        *,
        date: str | None = ...,
        target: str | None = ...,
        probe_type: str | None = ...,
        monitor_region: str | None = ...,
        limit: int = ...,
    ) -> list[SyntheticProbeSample]: ...
    def synthetic_rollups(
        self,
        *,
        period: str | None = ...,
        since: str | None = ...,
        until: str | None = ...,
        include_histograms: bool = ...,
        limit: int = ...,
    ) -> list[SyntheticRollup]: ...
    # Operational-analytics drain freshness -----------------------------------
    # Declared on the Protocol, not duck-typed off STORE, so a backend that
    # forgets it is a mypy error rather than a cloud that quietly publishes no
    # drain signal. That omission is the exact shape of the AWS-EU outage of
    # 2026-08-02..17: the drain was absent and the only alarm was the drain's.
    def operational_analytics_outbox_freshness(self) -> OutboxFreshness: ...
    def reconcile_generation_activity(
        self,
        workspace_id: str,
        *,
        date: str | None = ...,
        limit: int = ...,
    ) -> int: ...
    def get_generation(self, generation_id: str) -> Generation | None: ...
    def activity(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = ...,
        date: str | None = ...,
        tag_key: str | None = ...,
        tag_value: str | None = ...,
        group_by_tag: str | None = ...,
    ) -> list[dict[str, Any]]: ...
    def activity_events(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = ...,
        date: str | None = ...,
        limit: int = ...,
        tag_key: str | None = ...,
        tag_value: str | None = ...,
    ) -> list[dict[str, Any]]: ...
    def activity_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = ...,
        date: str | None = ...,
        tag_key: str | None = ...,
        tag_value: str | None = ...,
        group_by_tag: str | None = ...,
    ) -> Any: ...
    def activity_events_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = ...,
        date: str | None = ...,
        limit: int = ...,
        tag_key: str | None = ...,
        tag_value: str | None = ...,
    ) -> Any: ...
    def usage_series(
        self,
        workspace_id: str,
        *,
        window_minutes: int,
        granularity: str,
        api_key_hash: str | None = ...,
        by_model: bool = ...,
    ) -> dict[str, Any]: ...

    # Rate limiting -----------------------------------------------------------
    def hit_rate_limit(
        self,
        *,
        namespace: str,
        subject: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitHit: ...


@runtime_checkable
class TypedBillingStore(Protocol):
    """Optional capability: a store backed by the typed Spanner counter tables
    (tr_credit_balance / tr_key_limit) with the conditional-DML authorize/finalize
    path (the deadlock-fix billing cutover). The Spanner store implements it;
    InMemoryStore does not.

    Callers guard with ``isinstance(store, TypedBillingStore)`` instead of
    ``getattr(store, "authorize_gateway_typed", None)`` — the capability check
    mypy also understands (it narrows the type so the typed calls are statically
    verified), turning a stringly-typed probe on the authorization path back
    into a compile-time contract. These methods are deliberately NOT on the base
    ``Store`` protocol: they only exist on the typed backend, and the base
    surface must stay backend-symmetric.
    """

    def authorize_gateway_typed(
        self,
        *,
        workspace_id: str,
        key_hash: str,
        authorization_id: str | None = ...,
        estimate: int,
        has_credit_candidate: bool,
        reservation_usage_type: UsageType | str,
        model_id: str,
        provider: str,
        requested_model_id: str | None,
        candidate_model_ids: list[str],
        region: str | None,
        endpoint_id: str | None,
        candidate_endpoint_ids: list[str],
        idempotency_key: str | None,
        idempotency_fingerprint: str | None,
        key_usage_shards: int = ...,
        tags: dict[str, str] | None = ...,
        custom_model_id: str | None = ...,
        custom_model_revision: int | None = ...,
        user_provided_model_id: str | None = ...,
        user_provided_model_revision: int | None = ...,
        user_model_prompt_price_microdollars_per_m: int | None = ...,
        user_model_completion_price_microdollars_per_m: int | None = ...,
        user_model_owner_user_id: str | None = ...,
        additional_cost_reservation_microdollars: int = ...,
        native_batch_eligible: bool = ...,
        expires_at: Any = ...,
        window_limits: dict[str, int] | None = ...,
    ) -> tuple[str, GatewayAuthorization | None]: ...

    def typed_finalize_gateway_authorization(
        self,
        authorization_id: str,
        *,
        success: bool,
        actual_microdollars: int,
        selected_usage_type: UsageType | str,
        generation: Generation | None = ...,
        user_model_payout: UserModelPayout | None = ...,
    ) -> bool: ...

    def typed_finalize_gateway(self, **kwargs: Any) -> dict[str, Any]: ...

    def read_typed_reservation(self, reservation_id: str) -> dict[str, Any] | None: ...

    def is_typed_reservation(self, reservation_id: str | None, authorization_id: str) -> bool: ...

    def get_typed_authorization_by_idempotency(
        self, workspace_id: str, key_hash: str, idempotency_key: str
    ) -> GatewayAuthorization | None: ...

    def typed_credit_snapshot(self, workspace_id: str) -> tuple[int, int, int] | None: ...
