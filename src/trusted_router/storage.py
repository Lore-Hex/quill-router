from __future__ import annotations

import dataclasses
import datetime as dt
import threading
import uuid
from typing import Any, cast

from trusted_router import credit_transfer, phone_verification
from trusted_router.analytics_sink import AnalyticsSink, NullAnalyticsSink
from trusted_router.credit_transfer import (
    CreditTransferConflict,
    validate_amount,
    validate_outcome,
    validate_transfer_id,
)
from trusted_router.custom_model_billing import (
    user_model_authorization_id_from_payout_event_id,
)
from trusted_router.money import DEFAULT_SIGNUP_CREDIT_MICRODOLLARS
from trusted_router.operational_analytics_freshness import (
    BACKEND_MEMORY,
    REASON_NOT_CONFIGURED,
    OutboxFreshness,
)
from trusted_router.receipt_keys import (
    ReceiptKeyWriteOutcome,
    merge_receipt_key_observation,
)
from trusted_router.spend_windows import KeyWindowLimitDecision
from trusted_router.storage_attribution import InMemoryAcquisitionAttribution
from trusted_router.storage_auth_context import build_session_auth_context
from trusted_router.storage_auth_sessions import InMemoryAuthSessions
from trusted_router.storage_broadcast import InMemoryBroadcastDestinations
from trusted_router.storage_byok import InMemoryByok
from trusted_router.storage_custom_models import InMemoryCustomModels
from trusted_router.storage_email_blocks import InMemoryEmailBlocks
from trusted_router.storage_errors import StoreConflict
from trusted_router.storage_generations import InMemoryGenerations
from trusted_router.storage_group_buy import InMemoryBedrockGroupBuy
from trusted_router.storage_keys import InMemoryApiKeys
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
    CreditMoney,
    CreditMovement,
    CreditTransfer,
    CustomModel,
    EmailSendBlock,
    EncryptedSecretEnvelope,
    GatewayAuthorization,
    Generation,
    GoogleAdsConversion,
    Member,
    OAuthApp,
    OAuthAuthorizationCode,
    ProviderAccessGrant,
    ProviderBenchmarkSample,
    RateLimitHit,
    ReceiptKey,
    Reservation,
    SessionAuthContext,
    SignupResult,
    SyntheticProbeSample,
    SyntheticRollup,
    User,
    UserProvidedModel,
    VerificationToken,
    VideoJob,
    WalletChallenge,
    Workspace,
    federated_api_key_from_record,
    federated_workspace_from_record,
    iso_now,
    normalize_provider_access_role,
    normalize_provider_access_slug,
)
from trusted_router.storage_oauth_apps import InMemoryOAuthApps
from trusted_router.storage_oauth_codes import InMemoryOAuthCodes
from trusted_router.storage_rate_limits import InMemoryRateLimits
from trusted_router.storage_synthetic import InMemorySyntheticChecks
from trusted_router.storage_user_models import InMemoryUserProvidedModels
from trusted_router.storage_verification_tokens import InMemoryVerificationTokens
from trusted_router.storage_video_jobs import InMemoryVideoJobs
from trusted_router.storage_wallet_challenges import InMemoryWalletChallenges
from trusted_router.types import IdentityVerificationStatus, UsageType


class InMemoryStore:
    """Local/test implementation for the Spanner + Bigtable boundary.

    The methods mirror the production responsibilities:
    - Spanner-like strongly consistent transactional state for accounts/credits.
    - Bigtable-like append/query metadata for generation usage.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.users: dict[str, User] = {}
        self.user_ids_by_email: dict[str, str] = {}
        self.user_ids_by_wallet: dict[str, str] = {}
        self.provider_access_grants: dict[tuple[str, str], ProviderAccessGrant] = {}
        self.workspaces: dict[str, Workspace] = {}
        self.members: dict[tuple[str, str], Member] = {}
        self.credits: dict[str, CreditAccount] = {}
        self.credit_money: dict[str, CreditMoney] = {}
        self.stripe_events: set[str] = set()
        self.webhook_events: set[tuple[str, str]] = set()
        self.earnings_money: dict[str, tuple[int, int]] = {}
        self.credit_movements: dict[tuple[str, str], CreditMovement] = {}
        self.lifetime_topups: dict[str, int] = {}
        # Cross-plane credit transfer (trusted_router.credit_transfer).
        # `credit_transfers` is this plane as the SOURCE (escrow records);
        # `credit_transfer_claims` is this plane as the DESTINATION (the
        # insert-once verdict per transfer id). One store can be both.
        self.credit_transfers: dict[str, CreditTransfer] = {}
        self.credit_transfer_claims: dict[str, dict[str, Any]] = {}
        self.client_events_batches: list[dict[str, Any]] = []
        self.client_event_ids: set[str] = set()
        self.receipt_keys: dict[str, ReceiptKey] = {}
        #: Federated settlement claims, keyed (source_plane, authorization_id).
        #: Insert-once: the recorded terms are the verdict for every replay.
        self.federated_settlement_claims: dict[tuple[str, str], dict[str, Any]] = {}
        #: Aggregate clamp counters, keyed (source_plane, workspace_id, utc_date).
        self.federated_settlement_windows: dict[tuple[str, str, str], int] = {}
        # Composed feature stores. Each owns its own state and is importable
        # on its own. Keeps storage.py focused on identity + credit ledger;
        # spend control / BYOK / OAuth codes / auth sessions / generations /
        # rate limits / wallet / SES all live in their own modules.
        self.api_keys = InMemoryApiKeys(
            credits_by_workspace=self.credits,
            credit_money_by_workspace=self.credit_money,
            lock=self._lock,
        )
        self.acquisition_store = InMemoryAcquisitionAttribution(lock=self._lock)
        self.bedrock_group_buy_store = InMemoryBedrockGroupBuy(lock=self._lock)
        self.generation_store = InMemoryGenerations(
            lock=self._lock,
            add_usage_to_key=self.api_keys.add_usage,
        )
        self.synthetic_store = InMemorySyntheticChecks(lock=self._lock)
        self.byok_store = InMemoryByok(lock=self._lock)
        self.custom_model_store = InMemoryCustomModels(lock=self._lock)
        self.user_model_store = InMemoryUserProvidedModels(lock=self._lock)
        self.broadcast_store = InMemoryBroadcastDestinations(lock=self._lock)
        self.video_job_store = InMemoryVideoJobs(lock=self._lock)
        self.auth_session_store = InMemoryAuthSessions(lock=self._lock)
        self.oauth_code_store = InMemoryOAuthCodes(lock=self._lock)
        self.oauth_app_store = InMemoryOAuthApps(lock=self._lock)
        self.rate_limit_store = InMemoryRateLimits(lock=self._lock)
        self.wallet_challenges = InMemoryWalletChallenges()
        self.verification_tokens = InMemoryVerificationTokens()
        self.email_blocks = InMemoryEmailBlocks()

    def reset(self) -> None:
        with self._lock:
            self.users.clear()
            self.user_ids_by_email.clear()
            self.user_ids_by_wallet.clear()
            self.provider_access_grants.clear()
            self.workspaces.clear()
            self.members.clear()
            self.credits.clear()
            self.credit_money.clear()
            self.stripe_events.clear()
            self.webhook_events.clear()
            self.earnings_money.clear()
            self.credit_movements.clear()
            self.lifetime_topups.clear()
            self.credit_transfers.clear()
            self.credit_transfer_claims.clear()
            self.client_events_batches.clear()
            self.client_event_ids.clear()
            self.receipt_keys.clear()
            self.api_keys.reset()
            self.acquisition_store.reset()
            self.bedrock_group_buy_store.reset()
            self.generation_store.reset()
            self.synthetic_store.reset()
            self.byok_store.reset()
            self.custom_model_store.reset()
            self.user_model_store.reset()
            self.broadcast_store.reset()
            self.video_job_store.reset()
            self.auth_session_store.reset()
            self.oauth_code_store.reset()
            self.oauth_app_store.reset()
            self.rate_limit_store.reset()
            self.wallet_challenges.reset()
            self.verification_tokens.reset()
            self.email_blocks.reset()

    def readiness_check(self) -> None:
        """The in-memory backend has no external serving dependency."""

    def observe_receipt_key(self, record: ReceiptKey) -> ReceiptKeyWriteOutcome:
        with self._lock:
            merged, outcome = merge_receipt_key_observation(
                self.receipt_keys.get(record.kid), record
            )
            if merged is not None and outcome in {"appended", "refreshed"}:
                self.receipt_keys[record.kid] = merged
            return outcome

    def list_receipt_keys(self, *, limit: int = 5_000) -> list[ReceiptKey]:
        bounded = max(0, min(limit, 10_000))
        with self._lock:
            return [self.receipt_keys[kid] for kid in sorted(self.receipt_keys)[:bounded]]

    def ensure_user(
        self,
        user_id: str,
        email: str | None = None,
        *,
        trial_credit_microdollars: int | None = None,
    ) -> User:
        with self._lock:
            normalized_email = _normalize_email(email or user_id)
            existing_id = self.user_ids_by_email.get(normalized_email)
            if existing_id is not None:
                return self.users[existing_id]

            new_id = str(uuid.uuid4())
            self.users[new_id] = User(id=new_id, email=normalized_email)
            self.user_ids_by_email[normalized_email] = new_id
            self.create_workspace(
                owner_user_id=new_id,
                name="Personal Workspace",
                trial_credit_microdollars=trial_credit_microdollars,
            )
            return self.users[new_id]

    def grant_provider_access(
        self,
        user_id: str,
        provider: str,
        *,
        role: str = "viewer",
    ) -> ProviderAccessGrant:
        normalized_provider = normalize_provider_access_slug(provider)
        normalized_role = normalize_provider_access_role(role)
        if user_id not in self.users:
            raise ValueError("user does not exist")
        grant = ProviderAccessGrant(
            user_id=user_id,
            provider=normalized_provider,
            role=normalized_role,
        )
        with self._lock:
            self.provider_access_grants[(user_id, normalized_provider)] = grant
        return grant

    def list_provider_access_for_user(self, user_id: str) -> list[ProviderAccessGrant]:
        with self._lock:
            return sorted(
                (
                    grant
                    for (grant_user_id, _), grant in self.provider_access_grants.items()
                    if grant_user_id == user_id
                ),
                key=lambda grant: grant.provider,
            )

    def revoke_provider_access(self, user_id: str, provider: str) -> bool:
        normalized_provider = normalize_provider_access_slug(provider)
        with self._lock:
            return self.provider_access_grants.pop((user_id, normalized_provider), None) is not None

    def signup(
        self,
        *,
        email: str,
        workspace_name: str | None = None,
        trial_credit_microdollars: int = DEFAULT_SIGNUP_CREDIT_MICRODOLLARS,
    ) -> SignupResult | None:
        """Atomically create a new account end-to-end. Returns None if the
        email is already registered."""
        with self._lock:
            if self.user_ids_by_email.get(_normalize_email(email)) is not None:
                return None
            user = self.ensure_user(
                email,
                email=email,
                trial_credit_microdollars=trial_credit_microdollars,
            )
            workspace = self.list_workspaces_for_user(user.id)[0]
            if workspace_name:
                workspace.name = workspace_name
            raw_key, api_key = self.create_api_key(
                workspace_id=workspace.id,
                name="Signup key",
                creator_user_id=user.id,
                management=True,
            )
            trial = self.credit_money[workspace.id].total_credits_microdollars
        return SignupResult(
            user=user,
            workspace=workspace,
            raw_key=raw_key,
            api_key=api_key,
            trial_credit_microdollars=trial,
        )

    def create_acquisition_attribution(self, record: AcquisitionAttribution) -> bool:
        return self.acquisition_store.create(record)

    def get_acquisition_attribution(self, workspace_id: str) -> AcquisitionAttribution | None:
        return self.acquisition_store.get(workspace_id)

    def claim_acquisition_milestones(
        self,
        workspace_id: str,
        milestones: list[str],
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, list[str]]:
        return self.acquisition_store.claim_milestones(
            workspace_id,
            milestones,
            occurred_at=occurred_at,
        )

    def record_acquisition_purchase(
        self,
        workspace_id: str,
        *,
        amount_microdollars: int,
        occurred_at: str,
    ) -> AcquisitionAttribution | None:
        return self.acquisition_store.record_purchase(
            workspace_id,
            amount_microdollars=amount_microdollars,
            occurred_at=occurred_at,
        )

    def repair_google_ads_delivery_queue(self, *, since: str, limit: int) -> int:
        return self.acquisition_store.repair_google_ads_delivery_queue(
            since=since,
            limit=limit,
        )

    def purge_expired_google_ads_click_ids(self, *, before: str, limit: int) -> int:
        return self.acquisition_store.purge_expired_google_ads_click_ids(
            before=before,
            limit=limit,
        )

    def claim_google_ads_deliveries(
        self,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[GoogleAdsConversion]:
        return self.acquisition_store.claim_google_ads_deliveries(
            limit=limit,
            lease_seconds=lease_seconds,
        )

    def mark_google_ads_delivery_submitted(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        request_id: str,
    ) -> GoogleAdsConversion | None:
        return self.acquisition_store.mark_google_ads_delivery_submitted(
            order_id=order_id,
            occurred_at=occurred_at,
            lease_owner=lease_owner,
            request_id=request_id,
        )

    def mark_google_ads_delivery_failed(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        error: str,
        retryable: bool,
        max_attempts: int,
    ) -> GoogleAdsConversion | None:
        return self.acquisition_store.mark_google_ads_delivery_failed(
            order_id=order_id,
            occurred_at=occurred_at,
            lease_owner=lease_owner,
            error=error,
            retryable=retryable,
            max_attempts=max_attempts,
        )

    def list_activation_reminders(self, *, limit: int = 100) -> list[ActivationReminderTask]:
        return self.acquisition_store.list_reminders(limit=limit)

    def delete_activation_reminders(self, reminder_ids: list[str]) -> None:
        self.acquisition_store.delete_reminders(reminder_ids)

    def claim_activation_reminder(
        self,
        workspace_id: str,
        stage: str,
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, bool]:
        return self.acquisition_store.claim_reminder(
            workspace_id,
            stage,
            occurred_at=occurred_at,
        )

    def upsert_bedrock_group_buy_pledge(
        self, pledge: BedrockGroupBuyPledge
    ) -> BedrockGroupBuyPledge:
        return self.bedrock_group_buy_store.upsert(pledge)

    def get_bedrock_group_buy_pledge(self, user_id: str) -> BedrockGroupBuyPledge | None:
        return self.bedrock_group_buy_store.get(user_id)

    def withdraw_bedrock_group_buy_pledge(self, user_id: str) -> bool:
        return self.bedrock_group_buy_store.withdraw(user_id)

    def bedrock_group_buy_aggregate(self) -> BedrockGroupBuyAggregate:
        return self.bedrock_group_buy_store.aggregate()

    def list_bedrock_group_buy_public_messages(
        self, *, limit: int = 50
    ) -> list[BedrockGroupBuyPublicMessage]:
        return self.bedrock_group_buy_store.list_public_messages(limit=limit)

    def list_bedrock_group_buy_private_pledges(
        self, *, limit: int = 1000
    ) -> list[BedrockGroupBuyPledge]:
        return self.bedrock_group_buy_store.list_private_pledges(limit=limit)

    # Auth sessions delegate to storage_auth_sessions.InMemoryAuthSessions.
    def create_auth_session(
        self,
        *,
        user_id: str,
        provider: str,
        label: str,
        ttl_seconds: int,
        workspace_id: str | None = None,
        state: str = "active",
    ) -> tuple[str, AuthSession]:
        return self.auth_session_store.create(
            user_id=user_id,
            provider=provider,
            label=label,
            ttl_seconds=ttl_seconds,
            workspace_id=workspace_id,
            state=state,
        )

    def upgrade_auth_session(self, raw_token: str, *, state: str) -> AuthSession | None:
        return self.auth_session_store.upgrade(raw_token, state=state)

    def set_auth_session_workspace(self, raw_token: str, workspace_id: str) -> AuthSession | None:
        return self.auth_session_store.set_workspace(raw_token, workspace_id)

    def get_auth_session_by_raw(self, raw_token: str) -> AuthSession | None:
        return self.auth_session_store.get_by_raw(raw_token)

    def delete_auth_session_by_raw(self, raw_token: str) -> bool:
        return self.auth_session_store.delete_by_raw(raw_token)

    def session_auth_context(
        self,
        raw_token: str,
        *,
        requested_workspace_id: str | None = None,
    ) -> SessionAuthContext | None:
        """Resolve a session principal under one lock.

        Production backends implement the same contract with one strong SQL
        statement.  Keeping the in-memory implementation atomic makes tests
        model the same point-in-time membership decision instead of a sequence
        of independently locked lookups.
        """
        with self._lock:
            session = self.auth_session_store.get_by_raw(raw_token)
            if session is None:
                return None
            user = self.users.get(session.user_id)
            memberships: list[tuple[Member, Workspace]] = []
            for (workspace_id, user_id), candidate_member in self.members.items():
                if user_id != session.user_id:
                    continue
                candidate = self.workspaces.get(workspace_id)
                if candidate is None:
                    continue
                memberships.append((candidate_member, candidate))
            return build_session_auth_context(
                session=session,
                user=user,
                memberships=memberships,
                requested_workspace_id=requested_workspace_id,
            )

    def create_workspace(
        self,
        owner_user_id: str,
        name: str,
        *,
        trial_credit_microdollars: int | None = None,
    ) -> Workspace:
        with self._lock:
            workspace = Workspace(id=str(uuid.uuid4()), name=name, owner_user_id=owner_user_id)
            self.workspaces[workspace.id] = workspace
            self.members[(workspace.id, owner_user_id)] = Member(
                workspace_id=workspace.id, user_id=owner_user_id, role="owner"
            )
            # Account creation passes the configured starter amount explicitly.
            # Secondary workspaces omit it and therefore start at zero.
            initial_total = 0 if trial_credit_microdollars is None else trial_credit_microdollars
            self.credits[workspace.id] = CreditAccount(workspace_id=workspace.id)
            self.credit_money[workspace.id] = CreditMoney(total_credits_microdollars=initial_total)
            return workspace

    def list_workspaces_for_user(self, user_id: str) -> list[Workspace]:
        with self._lock:
            ids = [
                wid for (wid, uid), member in self.members.items() if uid == user_id and member.role
            ]
            return [self.workspaces[wid] for wid in ids if not self.workspaces[wid].deleted]

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._lock:
            workspace = self.workspaces.get(workspace_id)
            if workspace is None or workspace.deleted:
                return None
            return workspace

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        deleted: bool | None = None,
        billing_paused: bool | None = None,
        billing_pause_reason: str | None = None,
    ) -> Workspace | None:
        with self._lock:
            workspace = self.workspaces.get(workspace_id)
            if workspace is None:
                return None
            if name is not None:
                workspace.name = name
            if deleted is not None:
                workspace.deleted = deleted
            if billing_paused is not None:
                workspace.billing_paused = billing_paused
            if billing_pause_reason is not None:
                workspace.billing_pause_reason = billing_pause_reason
            return workspace

    def get_credit_account(self, workspace_id: str) -> CreditAccount | None:
        with self._lock:
            return self.credits.get(workspace_id)

    def credit_money_snapshot(self, workspace_id: str) -> tuple[int, int, int] | None:
        money = self.credit_money.get(workspace_id)
        if money is None:
            return None
        return (
            money.total_credits_microdollars,
            money.total_usage_microdollars,
            money.reserved_microdollars,
        )

    def add_members(
        self, workspace_id: str, emails: list[str], role: str = "member"
    ) -> list[Member]:
        with self._lock:
            members: list[Member] = []
            for email in emails:
                user = self.ensure_user(email)
                member = Member(workspace_id=workspace_id, user_id=user.id, role=role)
                self.members[(workspace_id, user.id)] = member
                members.append(member)
            return members

    def remove_members(self, workspace_id: str, user_ids: list[str]) -> None:
        with self._lock:
            for identifier in user_ids:
                user_id = self._resolve_user_identifier(identifier)
                if user_id is not None:
                    self.members.pop((workspace_id, user_id), None)

    def list_members(self, workspace_id: str) -> list[Member]:
        with self._lock:
            return [member for (wid, _), member in self.members.items() if wid == workspace_id]

    def user_can_manage(self, user_id: str, workspace_id: str) -> bool:
        with self._lock:
            member = self.members.get((workspace_id, user_id))
            return member is not None and member.role in {"owner", "admin"}

    def user_is_member(self, user_id: str, workspace_id: str) -> bool:
        with self._lock:
            return (workspace_id, user_id) in self.members

    def get_user(self, user_id: str) -> User | None:
        with self._lock:
            return self.users.get(user_id)

    def find_user_by_email(self, email: str) -> User | None:
        with self._lock:
            user_id = self.user_ids_by_email.get(_normalize_email(email))
            if user_id is None:
                return None
            return self.users.get(user_id)

    def find_user_by_wallet(self, address: str) -> User | None:
        with self._lock:
            user_id = self.user_ids_by_wallet.get(address.strip().lower())
            if user_id is None:
                return None
            return self.users.get(user_id)

    def create_wallet_user(self, address: str) -> User:
        """Create a fresh user keyed only by wallet address. email and
        email_verified stay unset until the verification flow completes."""
        with self._lock:
            normalized = address.strip().lower()
            existing = self.user_ids_by_wallet.get(normalized)
            if existing is not None:
                return self.users[existing]
            new_id = str(uuid.uuid4())
            self.users[new_id] = User(id=new_id, email=None, wallet_address=normalized)
            self.user_ids_by_wallet[normalized] = new_id
            self.create_workspace(
                owner_user_id=new_id,
                name="Personal Workspace",
                trial_credit_microdollars=0,
            )
            return self.users[new_id]

    def set_user_email(self, user_id: str, email: str) -> User | None:
        """Attach an email to a wallet-only user. Returns None if email
        collides with another existing user. Does not verify it."""
        with self._lock:
            normalized_email = _normalize_email(email)
            existing = self.user_ids_by_email.get(normalized_email)
            if existing is not None and existing != user_id:
                return None
            user = self.users.get(user_id)
            if user is None:
                return None
            previous_email = _normalize_email(user.email) if user.email else None
            if user.email and _normalize_email(user.email) in self.user_ids_by_email:
                self.user_ids_by_email.pop(_normalize_email(user.email), None)
            user.email = normalized_email
            if previous_email != normalized_email:
                user.email_verified = False
            self.user_ids_by_email[normalized_email] = user_id
            return user

    def mark_user_email_verified(self, user_id: str) -> User | None:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                return None
            user.email_verified = True
            return user

    def set_user_identity_status(
        self,
        user_id: str,
        *,
        status: str,
        session_id: str | None = None,
        session_url: str | None = None,
        decision_code: int | None = None,
        decision_reason: str | None = None,
        decision_reason_code: int | None = None,
        verified_name: str | None = None,
        increment_attempts: bool = False,
    ) -> User | None:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                return None
            normalized = IdentityVerificationStatus.coerce(status)
            if normalized is IdentityVerificationStatus.APPROVED and not user.identity_verified_at:
                user.identity_verified_at = iso_now()
            user.identity_status = normalized.value
            if session_id is not None:
                if session_id != user.veriff_session_id or not user.veriff_session_created_at:
                    user.veriff_session_created_at = iso_now()
                user.veriff_session_id = session_id
            if session_url is not None:
                user.veriff_session_url = session_url
            if decision_code is not None:
                user.veriff_decision_code = decision_code
            if decision_reason is not None:
                user.veriff_decision_reason = decision_reason
            if decision_reason_code is not None:
                user.veriff_decision_reason_code = decision_reason_code
            if verified_name is not None:
                user.identity_verified_name = verified_name
            if increment_attempts:
                user.veriff_attempt_count += 1
            return user

    def begin_phone_verification(
        self, user_id: str, phone: str, channel: str | None = None
    ) -> tuple[str, User] | None:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                return None
            code = phone_verification.begin(user, phone, channel=channel)
            return code, user

    def confirm_phone_verification(self, user_id: str, code: str) -> tuple[str, User | None]:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                return "no_pending", None
            result = phone_verification.confirm(user, code)
            return result.status, user

    def cancel_phone_verification(self, user_id: str) -> User | None:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                return None
            phone_verification.cancel_pending(user)
            return user

    def clear_user_phone(self, user_id: str) -> User | None:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                return None
            phone_verification.clear(user)
            return user

    def _resolve_user_identifier(self, identifier: str) -> str | None:
        if identifier in self.users:
            return identifier
        return self.user_ids_by_email.get(_normalize_email(identifier))

    # API key + per-key spend cap. The actual logic lives in
    # storage_keys.InMemoryApiKeys; these methods are thin delegations to
    # keep the Store Protocol surface stable.
    def create_api_key(
        self,
        *,
        workspace_id: str,
        name: str,
        creator_user_id: str | None,
        management: bool = False,
        raw_key: str | None = None,
        limit_microdollars: int | None = None,
        limit_reset: str | None = None,
        include_byok_in_limit: bool = True,
        expires_at: str | None = None,
        limit_daily_microdollars: int | None = None,
        limit_weekly_microdollars: int | None = None,
        limit_monthly_microdollars: int | None = None,
        budget_alert_only: bool = False,
        tags: dict[str, str] | None = None,
        scopes: list[str] | None = None,
        app_id: str = "",
    ) -> tuple[str, ApiKey]:
        return self.api_keys.create(
            workspace_id=workspace_id,
            name=name,
            creator_user_id=creator_user_id,
            management=management,
            raw_key=raw_key,
            limit_microdollars=limit_microdollars,
            limit_reset=limit_reset,
            include_byok_in_limit=include_byok_in_limit,
            expires_at=expires_at,
            limit_daily_microdollars=limit_daily_microdollars,
            limit_weekly_microdollars=limit_weekly_microdollars,
            limit_monthly_microdollars=limit_monthly_microdollars,
            budget_alert_only=budget_alert_only,
            tags=tags,
            scopes=scopes,
            app_id=app_id,
        )

    def get_key_by_hash(self, key_hash: str) -> ApiKey | None:
        return self.api_keys.get_by_hash(key_hash)

    def typed_key_usage(
        self,
        key_hash: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, Any] | None:
        """InMemory twin of the Spanner typed point-read: lifetime counters are
        already live on the ApiKey; windows come from the lazy snapshot."""
        key = self.api_keys.get_by_hash(key_hash)
        if key is None:
            return None
        return {
            "usage": key.usage_microdollars,
            "byok_usage": key.byok_usage_microdollars,
            "reserved": key.reserved_microdollars,
            "windows": self.api_keys.window_usage_snapshot(key_hash),
        }

    def upsert_federated_api_key(self, record: dict[str, Any]) -> ApiKey:
        """Persist a key record resolved from the home plane.

        Identity only. The record carries NO salt/secret_hash (a peer never
        holds home-issued key material) and NO credits — a federated key
        seeds at ZERO local balance, because copying a balance mints money.
        Spending on this plane requires an explicit transfer.

        The shadow workspace is materialized under the SAME lock as the key.
        A key without its workspace 403s on every request (the authorize path
        reads the workspace before it reads credits), so the pair must appear
        together — the InMemory twin of the Postgres single transaction.
        """
        key = federated_api_key_from_record(record)
        workspace = federated_workspace_from_record(record)
        with self._lock:
            if not workspace.id:
                raise ValueError("federated record carries no workspace_id")
            existing = self.workspaces.get(workspace.id)
            if existing is not None and not existing.federated_home:
                # Directory collision: a real local workspace already owns
                # this id. Overwriting it would replace a tenant with an
                # ownerless shadow. See the Postgres twin for the reasoning.
                raise StoreConflict(
                    f"workspace {workspace.id} exists locally and is not federated; "
                    "refusing to overwrite it with a federated shadow"
                )
            self.workspaces[workspace.id] = workspace
            # setdefault, NOT assignment: re-federating a key must never reset
            # a balance a completed credit transfer already funded.
            self.credits.setdefault(workspace.id, CreditAccount(workspace_id=workspace.id))
            self.credit_money.setdefault(workspace.id, CreditMoney())
            # BOTH the entity and its lookup index. Writing only `keys` made
            # the first federated request work (the resolve returns the record
            # directly) and every one after it miss — the same defect the
            # Postgres backend had, because I wrote both by hand instead of
            # going through one shared helper.
            self.api_keys.keys[key.hash] = key
            self.api_keys.key_ids_by_lookup_hash[key.lookup_hash] = key.hash
            return key

    def get_key_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None:
        return self.api_keys.get_by_lookup_hash(lookup_hash)

    def get_key_by_raw(self, raw_key: str) -> ApiKey | None:
        return self.api_keys.get_by_raw(raw_key)

    def api_key_auth_context(self, raw_key: str) -> ApiKeyAuthContext | None:
        """Resolve the key and its workspace atomically, without a cache."""
        with self._lock:
            api_key = self.api_keys.get_by_raw(raw_key)
            if api_key is None:
                return None
            workspace = self.workspaces.get(api_key.workspace_id)
            if workspace is not None and workspace.deleted:
                workspace = None
            return ApiKeyAuthContext(api_key=api_key, workspace=workspace)

    def list_keys(self, workspace_id: str) -> list[ApiKey]:
        return self.api_keys.list_for_workspace(workspace_id)

    def list_api_keys_with_usage(self, workspace_id: str) -> list[ApiKeyUsageSnapshot]:
        return self.api_keys.list_with_usage_for_workspace(workspace_id)

    def delete_key(self, key_hash: str) -> bool:
        return self.api_keys.delete(key_hash)

    def reserve_key_limit(
        self,
        key_hash: str,
        amount_microdollars: int,
        *,
        usage_type: str,
    ) -> KeyWindowLimitDecision | None:
        return self.api_keys.reserve_limit(key_hash, amount_microdollars, usage_type=usage_type)

    def settle_key_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        actual_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        self.api_keys.settle_limit(
            key_hash, reserved_microdollars, actual_microdollars, usage_type=usage_type
        )

    def refund_key_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        self.api_keys.refund_limit(key_hash, reserved_microdollars, usage_type=usage_type)

    def update_key(self, key_hash: str, patch: dict[str, Any]) -> ApiKey | None:
        return self.api_keys.update(key_hash, patch)

    # BYOK delegates to storage_byok.InMemoryByok.
    def upsert_byok_provider(
        self,
        *,
        workspace_id: str,
        provider: str,
        secret_ref: str,
        key_hint: str | None,
        encrypted_secret: EncryptedSecretEnvelope | None = None,
    ) -> ByokProviderConfig:
        return self.byok_store.upsert(
            workspace_id=workspace_id,
            provider=provider,
            secret_ref=secret_ref,
            key_hint=key_hint,
            encrypted_secret=encrypted_secret,
        )

    def list_byok_providers(self, workspace_id: str) -> list[ByokProviderConfig]:
        return self.byok_store.list_for_workspace(workspace_id)

    def get_byok_provider(self, workspace_id: str, provider: str) -> ByokProviderConfig | None:
        return self.byok_store.get(workspace_id, provider)

    def delete_byok_provider(self, workspace_id: str, provider: str) -> bool:
        return self.byok_store.delete(workspace_id, provider)

    def create_custom_model(
        self,
        *,
        owner_user_id: str,
        owner_workspace_id: str,
        name: str,
        base_model_id: str,
        hidden_prompt: str,
        enabled: bool = True,
        slug: str | None = None,
    ) -> CustomModel:
        return self.custom_model_store.create(
            owner_user_id=owner_user_id,
            owner_workspace_id=owner_workspace_id,
            name=name,
            base_model_id=base_model_id,
            hidden_prompt=hidden_prompt,
            enabled=enabled,
            slug=slug,
            other_model_exists=lambda model_id: self.user_model_store.get(model_id) is not None,
        )

    def list_custom_models_for_user(self, owner_user_id: str) -> list[CustomModel]:
        return self.custom_model_store.list_for_user(owner_user_id)

    def get_custom_model(self, model_id: str) -> CustomModel | None:
        return self.custom_model_store.get(model_id)

    def update_custom_model(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> CustomModel | None:
        return self.custom_model_store.update(
            model_id,
            owner_user_id=owner_user_id,
            patch=patch,
            other_model_exists=lambda candidate_id: (
                self.user_model_store.get(candidate_id) is not None
            ),
        )

    def delete_custom_model(self, model_id: str, *, owner_user_id: str) -> bool:
        return self.custom_model_store.delete(model_id, owner_user_id=owner_user_id)

    def create_user_model(
        self,
        *,
        owner_user_id: str,
        owner_workspace_id: str,
        name: str,
        kind: str,
        description: str = "",
        display_identity: str = "handle",
        display_name: str = "",
        endpoint_url: str,
        upstream_model_id: str | None = None,
        encrypted_endpoint_api_key: EncryptedSecretEnvelope | None = None,
        endpoint_key_hint: str | None = None,
        encrypted_signing_secret: EncryptedSecretEnvelope | None = None,
        supports_streaming: bool = True,
        heartbeat_interval_seconds: int | None = None,
        max_concurrency: int = 4,
        prompt_price_microdollars_per_million_tokens: int = 0,
        completion_price_microdollars_per_million_tokens: int = 0,
        human_verified: bool = False,
        enabled: bool = True,
        status: str = "active",
        slug: str | None = None,
    ) -> UserProvidedModel:
        return self.user_model_store.create(
            owner_user_id=owner_user_id,
            owner_workspace_id=owner_workspace_id,
            name=name,
            kind=kind,
            description=description,
            display_identity=display_identity,
            display_name=display_name,
            endpoint_url=endpoint_url,
            upstream_model_id=upstream_model_id,
            encrypted_endpoint_api_key=encrypted_endpoint_api_key,
            endpoint_key_hint=endpoint_key_hint,
            encrypted_signing_secret=encrypted_signing_secret,
            supports_streaming=supports_streaming,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            max_concurrency=max_concurrency,
            prompt_price_microdollars_per_million_tokens=(
                prompt_price_microdollars_per_million_tokens
            ),
            completion_price_microdollars_per_million_tokens=(
                completion_price_microdollars_per_million_tokens
            ),
            human_verified=human_verified,
            enabled=enabled,
            status=status,
            slug=slug,
            other_model_exists=lambda model_id: self.custom_model_store.get(model_id) is not None,
        )

    def list_user_models_for_user(self, owner_user_id: str) -> list[UserProvidedModel]:
        return self.user_model_store.list_for_user(owner_user_id)

    def get_user_model(self, model_id: str) -> UserProvidedModel | None:
        return self.user_model_store.get(model_id)

    def get_user_models_by_ids(
        self,
        model_ids: list[str],
    ) -> dict[str, UserProvidedModel]:
        return self.user_model_store.get_many(model_ids)

    def update_user_model(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> UserProvidedModel:
        return self.user_model_store.update(
            model_id,
            owner_user_id=owner_user_id,
            patch=patch,
            other_model_exists=lambda candidate_id: (
                self.custom_model_store.get(candidate_id) is not None
            ),
        )

    def delete_user_model(self, model_id: str, *, owner_user_id: str) -> bool:
        return self.user_model_store.delete(model_id, owner_user_id=owner_user_id)

    def set_user_model_online(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        online: bool,
    ) -> UserProvidedModel:
        return self.user_model_store.set_online(
            model_id,
            owner_user_id=owner_user_id,
            online=online,
        )

    def record_user_model_heartbeat(
        self,
        model_id: str,
        *,
        expires_at: str,
    ) -> UserProvidedModel:
        return self.user_model_store.record_heartbeat(model_id, expires_at=expires_at)

    def record_user_model_probe(
        self,
        model_id: str,
        *,
        status: str,
        checked_at: str,
    ) -> UserProvidedModel:
        return self.user_model_store.record_probe(
            model_id,
            status=status,
            checked_at=checked_at,
        )

    def record_user_model_dispatch_result(
        self,
        model_id: str,
        *,
        success: bool,
    ) -> UserProvidedModel:
        return self.user_model_store.record_dispatch_result(model_id, success=success)

    def acquire_user_model_slot(
        self,
        model_id: str,
        authorization_id: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> bool:
        return self.user_model_store.acquire_slot(
            model_id,
            authorization_id,
            limit=limit,
            ttl_seconds=ttl_seconds,
        )

    def release_user_model_slot(self, model_id: str, authorization_id: str) -> None:
        self.user_model_store.release_slot(model_id, authorization_id)

    def list_public_user_models(
        self,
        *,
        kind: str | None = None,
    ) -> list[UserProvidedModel]:
        return self.user_model_store.list_public(kind=kind)

    def create_broadcast_destination(
        self,
        *,
        workspace_id: str,
        type: str,
        name: str,
        endpoint: str,
        enabled: bool = True,
        include_content: bool = False,
        method: str = "POST",
        encrypted_api_key: EncryptedSecretEnvelope | None = None,
        encrypted_headers: EncryptedSecretEnvelope | None = None,
        header_names: list[str] | None = None,
    ) -> BroadcastDestination:
        return self.broadcast_store.create(
            workspace_id=workspace_id,
            type=type,
            name=name,
            endpoint=endpoint,
            enabled=enabled,
            include_content=include_content,
            method=method,
            encrypted_api_key=encrypted_api_key,
            encrypted_headers=encrypted_headers,
            header_names=header_names,
        )

    def list_broadcast_destinations(self, workspace_id: str) -> list[BroadcastDestination]:
        return self.broadcast_store.list_for_workspace(workspace_id)

    def get_broadcast_destination(
        self, workspace_id: str, destination_id: str
    ) -> BroadcastDestination | None:
        return self.broadcast_store.get(workspace_id, destination_id)

    def update_broadcast_destination(
        self,
        workspace_id: str,
        destination_id: str,
        **patch: Any,
    ) -> BroadcastDestination | None:
        return self.broadcast_store.update(workspace_id, destination_id, **patch)

    def delete_broadcast_destination(self, workspace_id: str, destination_id: str) -> bool:
        return self.broadcast_store.delete(workspace_id, destination_id)

    def enqueue_broadcast_delivery(
        self,
        *,
        workspace_id: str,
        destination_id: str,
        generation_id: str,
        settle_body: dict[str, Any],
    ) -> BroadcastDeliveryJob:
        return self.broadcast_store.enqueue_delivery(
            workspace_id=workspace_id,
            destination_id=destination_id,
            generation_id=generation_id,
            settle_body=settle_body,
        )

    def due_broadcast_deliveries(self, *, limit: int = 100) -> list[BroadcastDeliveryJob]:
        return self.broadcast_store.due_deliveries(limit=limit)

    def claim_broadcast_deliveries(
        self,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[BroadcastDeliveryJob]:
        return self.broadcast_store.claim_deliveries(limit=limit, lease_seconds=lease_seconds)

    def mark_broadcast_delivery(
        self,
        job_id: str,
        *,
        success: bool,
        error: str | None = None,
        lease_owner: str | None = None,
    ) -> BroadcastDeliveryJob | None:
        return self.broadcast_store.mark_delivery(
            job_id,
            success=success,
            error=error,
            lease_owner=lease_owner,
        )

    def prepare_video_job(self, job: VideoJob) -> tuple[VideoJob, bool]:
        return self.video_job_store.prepare(job)

    def get_video_job(self, job_id: str) -> VideoJob | None:
        return self.video_job_store.get(job_id)

    def get_video_job_for_key(self, job_id: str, key_hash: str) -> VideoJob | None:
        return self.video_job_store.get_for_key(job_id, key_hash)

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
    ) -> VideoJob | None:
        return self.video_job_store.mark_queued(
            job_id,
            provider_job_id=provider_job_id,
            provider=provider,
            endpoint_id=endpoint_id,
            provider_model=provider_model,
            quoted_microdollars=quoted_microdollars,
            poll_after_seconds=poll_after_seconds,
        )

    def claim_video_jobs(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[VideoJob]:
        return self.video_job_store.claim_due(
            lease_owner=lease_owner,
            limit=limit,
            lease_seconds=lease_seconds,
        )

    def update_video_job(
        self,
        job_id: str,
        *,
        status: str,
        lease_owner: str | None = None,
        provider_status: str | None = None,
        generation_id: str | None = None,
        error: str | None = None,
        poll_after_seconds: int = 5,
    ) -> VideoJob | None:
        return self.video_job_store.update(
            job_id,
            status=status,
            lease_owner=lease_owner,
            provider_status=provider_status,
            generation_id=generation_id,
            error=error,
            poll_after_seconds=poll_after_seconds,
        )

    def mark_video_job_cleaned(self, job_id: str) -> VideoJob | None:
        return self.video_job_store.mark_cleaned(job_id)

    def apply_federated_usage(
        self,
        *,
        source_plane: str,
        authorization_id: str,
        workspace_id: str,
        cost_microdollars: int,
        daily_cap_microdollars: int,
    ) -> str:
        """HOME side of deferred settlement: book a peer's recorded debt.

        Outcomes (strings, matched by the route):
          applied            first application; usage booked
          already            replay of the SAME terms; nothing booked again
          conflict           same id, DIFFERENT terms; nothing booked, ever
          workspace_unknown  no such workspace here
          clamped            per-(plane, workspace) daily cap would be
                             exceeded; nothing recorded — the row stays
                             pending on the peer and retries tomorrow

        Debit-only by construction: the only mutation is total_usage UP.
        The clamp is checked BEFORE the claim is recorded, so a clamped row
        leaves no residue and can apply cleanly later. The window is keyed by
        THIS plane's clock — a peer-supplied timestamp choosing its own
        window would let a compromised peer spread a burst across days.
        """
        from trusted_router.spend_windows import utcnow

        cost = int(cost_microdollars)
        if cost <= 0:
            raise ValueError("cost_microdollars must be positive")
        claim_key = (source_plane, authorization_id)
        window_key = (source_plane, workspace_id, utcnow().date().isoformat())
        with self._lock:
            existing = self.federated_settlement_claims.get(claim_key)
            if existing is not None:
                if (
                    existing["workspace_id"] == workspace_id
                    and existing["cost_microdollars"] == cost
                ):
                    return "already"
                return "conflict"
            if self.workspaces.get(workspace_id) is None:
                return "workspace_unknown"
            applied_today = self.federated_settlement_windows.get(window_key, 0)
            if applied_today + cost > int(daily_cap_microdollars):
                return "clamped"
            self.federated_settlement_claims[claim_key] = {
                "workspace_id": workspace_id,
                "cost_microdollars": cost,
            }
            self.federated_settlement_windows[window_key] = applied_today + cost
            money = self.credit_money.setdefault(workspace_id, CreditMoney())
            money.total_usage_microdollars += cost
            return "applied"

    def credit_workspace_once(
        self, workspace_id: str, amount_microdollars: int, event_id: str
    ) -> bool:
        with self._lock:
            if event_id in self.stripe_events:
                return False
            money = self.credit_money.get(workspace_id)
            if money is None:
                raise ValueError("credit_account_not_found")
            self.stripe_events.add(event_id)
            money.total_credits_microdollars += amount_microdollars
            return True

    def credit_workspace_typed_direct(
        self,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        lifetime_topup_user_id: str | None = None,
    ) -> bool:
        with self._lock:
            if event_id in self.stripe_events:
                return False
            self.stripe_events.add(event_id)
            self.credit_money[workspace_id].total_credits_microdollars += amount_microdollars
            if lifetime_topup_user_id is not None:
                self.lifetime_topups[lifetime_topup_user_id] = self.lifetime_topups.get(
                    lifetime_topup_user_id, 0
                ) + int(amount_microdollars)
            return True

    # Earnings & movement primitives -----------------------------------------

    @staticmethod
    def _positive_money_amount(amount_microdollars: int) -> int:
        amount = int(amount_microdollars)
        if amount <= 0:
            raise ValueError("amount_must_be_positive")
        return amount

    def debit_workspace_guarded(
        self,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        kind: str,
        custom_model_id: str | None = None,
        authorization_id: str | None = None,
    ) -> str:
        amount = self._positive_money_amount(amount_microdollars)
        with self._lock:
            if event_id in self.stripe_events:
                return "duplicate"
            money = self.credit_money.get(workspace_id)
            available = (
                -1
                if money is None
                else money.total_credits_microdollars
                - money.total_usage_microdollars
                - money.reserved_microdollars
            )
            if money is None or available < amount:
                return "insufficient"
            money.total_credits_microdollars -= amount
            self.stripe_events.add(event_id)
            self.credit_movements[(workspace_id, event_id)] = CreditMovement(
                account_id=workspace_id,
                movement_id=event_id,
                kind=kind,
                amount_microdollars=-amount,
                custom_model_id=custom_model_id,
                authorization_id=authorization_id,
            )
            return "accepted"

    def credit_user_earnings(
        self,
        user_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        custom_model_id: str | None = None,
        payer_workspace_id: str | None = None,
    ) -> bool:
        amount = self._positive_money_amount(amount_microdollars)
        account_id = f"user:{user_id}"
        with self._lock:
            if event_id in self.stripe_events:
                return False
            earned, transferred = self.earnings_money.get(user_id, (0, 0))
            self.earnings_money[user_id] = (earned + amount, transferred)
            self.stripe_events.add(event_id)
            self.credit_movements[(account_id, event_id)] = CreditMovement(
                account_id=account_id,
                movement_id=event_id,
                kind="custom_model_payout",
                amount_microdollars=amount,
                counterparty_account_id=payer_workspace_id,
                custom_model_id=custom_model_id,
                authorization_id=(user_model_authorization_id_from_payout_event_id(event_id)),
            )
            return True

    def transfer_earnings_to_workspace(
        self,
        user_id: str,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> str:
        amount = self._positive_money_amount(amount_microdollars)
        user_account_id = f"user:{user_id}"
        with self._lock:
            if event_id in self.stripe_events:
                return "duplicate"
            earned, transferred = self.earnings_money.get(user_id, (0, 0))
            if earned - transferred < amount:
                return "insufficient"
            money = self.credit_money.get(workspace_id)
            if money is None:
                raise ValueError("credit_account_not_found")
            self.earnings_money[user_id] = (earned, transferred + amount)
            money.total_credits_microdollars += amount
            self.stripe_events.add(event_id)
            created_at = iso_now()
            self.credit_movements[(user_account_id, event_id)] = CreditMovement(
                account_id=user_account_id,
                movement_id=event_id,
                kind="earnings_transfer_out",
                amount_microdollars=-amount,
                counterparty_account_id=workspace_id,
                created_at=created_at,
            )
            self.credit_movements[(workspace_id, event_id)] = CreditMovement(
                account_id=workspace_id,
                movement_id=event_id,
                kind="earnings_transfer_in",
                amount_microdollars=amount,
                counterparty_account_id=user_account_id,
                created_at=created_at,
            )
            return "accepted"

    def ensure_earnings_account(self, user_id: str) -> None:
        with self._lock:
            self.earnings_money.setdefault(user_id, (0, 0))

    def earnings_summary(
        self,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, int]:
        with self._lock:
            earned, transferred = self.earnings_money.get(user_id, (0, 0))
            return {
                "total_earned": earned,
                "total_transferred": transferred,
                "available": earned - transferred,
            }

    def list_credit_movements(
        self,
        account_id: str,
        *,
        kinds: list[str] | None = None,
        limit: int = 50,
        before: str | None = None,
    ) -> list[CreditMovement]:
        allowed = None if kinds is None else set(kinds)
        bounded = max(0, int(limit))
        before_timestamp = None if before is None else _parse_iso_timestamp(before)
        with self._lock:
            matches = [
                movement
                for (movement_account_id, _), movement in self.credit_movements.items()
                if movement_account_id == account_id
                and (allowed is None or movement.kind in allowed)
                and (
                    before_timestamp is None
                    or _parse_iso_timestamp(movement.created_at) < before_timestamp
                )
            ]
        matches.sort(
            key=lambda movement: (movement.created_at, movement.movement_id),
            reverse=True,
        )
        return matches[:bounded]

    def custom_model_earnings_by_model(
        self,
        user_id: str,
        *,
        since: str,
    ) -> dict[str, int]:
        totals: dict[str, int] = {}
        since_timestamp = _parse_iso_timestamp(since)
        with self._lock:
            for movement in self.credit_movements.values():
                if (
                    movement.account_id == f"user:{user_id}"
                    and movement.kind == "custom_model_payout"
                    and movement.custom_model_id is not None
                    and _parse_iso_timestamp(movement.created_at) >= since_timestamp
                ):
                    totals[movement.custom_model_id] = (
                        totals.get(movement.custom_model_id, 0) + movement.amount_microdollars
                    )
        return totals

    def get_lifetime_topup_microdollars(
        self,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> int:
        with self._lock:
            return self.lifetime_topups.get(user_id, 0)

    def add_lifetime_topup(
        self,
        user_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> bool:
        amount = self._positive_money_amount(amount_microdollars)
        with self._lock:
            if event_id in self.stripe_events:
                return False
            self.stripe_events.add(event_id)
            self.lifetime_topups[user_id] = self.lifetime_topups.get(user_id, 0) + amount
            return True

    def update_auto_refill_settings(
        self,
        workspace_id: str,
        *,
        enabled: bool,
        threshold_microdollars: int,
        amount_microdollars: int,
    ) -> CreditAccount | None:
        """Update auto-refill thresholds. Caller validates ranges; we just
        store. Disabling clears the schedule but keeps the saved Stripe
        customer/payment-method so re-enabling doesn't require re-onboarding."""
        with self._lock:
            account = self.credits.get(workspace_id)
            if account is None:
                return None
            account.auto_refill_enabled = enabled
            account.auto_refill_threshold_microdollars = max(0, threshold_microdollars)
            account.auto_refill_amount_microdollars = max(0, amount_microdollars)
            return account

    def set_stripe_customer(
        self,
        workspace_id: str,
        *,
        customer_id: str,
        payment_method_id: str | None = None,
    ) -> CreditAccount | None:
        """Record the Stripe customer + default off-session payment method
        captured during a Checkout session. Lets future auto-refills charge
        without re-prompting for a card."""
        with self._lock:
            account = self.credits.get(workspace_id)
            if account is None:
                return None
            account.stripe_customer_id = customer_id
            if payment_method_id is not None:
                account.stripe_payment_method_id = payment_method_id
            return account

    def clear_stripe_payment_method(self, workspace_id: str) -> CreditAccount | None:
        with self._lock:
            account = self.credits.get(workspace_id)
            if account is None:
                return None
            account.stripe_payment_method_id = None
            account.auto_refill_enabled = False
            account.last_auto_refill_at = iso_now()
            account.last_auto_refill_status = "disabled:payment_method_removed"
            return account

    def record_auto_refill_outcome(
        self,
        workspace_id: str,
        *,
        status: str,
    ) -> CreditAccount | None:
        with self._lock:
            account = self.credits.get(workspace_id)
            if account is None:
                return None
            account.last_auto_refill_at = iso_now()
            account.last_auto_refill_status = status
            return account

    def reserve(
        self,
        workspace_id: str,
        key_hash: str,
        amount_microdollars: int,
        *,
        idempotency_key: str | None = None,
    ) -> Reservation:
        return self.api_keys.reserve(
            workspace_id,
            key_hash,
            amount_microdollars,
            idempotency_key=idempotency_key,
        )

    def settle(self, reservation_id: str, actual_microdollars: int) -> None:
        self.api_keys.settle(reservation_id, actual_microdollars)

    def refund(self, reservation_id: str) -> None:
        self.api_keys.refund(reservation_id)

    # --- Cross-plane credit transfer ---------------------------------------
    #
    # The InMemory twin of the Postgres implementation. See
    # trusted_router.credit_transfer for the state machine, which plane holds
    # the value in each state, and the conservation invariant. `self._lock` is
    # this backend's transaction: every insert-once check and the balance
    # change it authorizes happen inside ONE acquisition, so a concurrent
    # caller can never observe (or act on) a half-applied transition.

    def open_credit_transfer(
        self,
        *,
        transfer_id: str,
        workspace_id: str,
        amount_microdollars: int,
        destination: str,
    ) -> CreditTransfer:
        """SOURCE side: debit into escrow. Value becomes held by THIS plane."""
        transfer_id = validate_transfer_id(transfer_id)
        amount = validate_amount(amount_microdollars)
        with self._lock:
            existing = self.credit_transfers.get(transfer_id)
            if existing is not None:
                # Redelivered open: return the first one, debit nothing.
                # But only to a caller naming the SAME move — an id is not an
                # agreement. A different destination lets two planes rule on
                # one escrow; a different workspace or amount reports somebody
                # else's completed transfer as this caller's.
                credit_transfer.require_matching_transfer(
                    transfer_id,
                    existing,
                    workspace_id=workspace_id,
                    amount_microdollars=amount,
                    destination=destination,
                )
                return existing
            money = self.credit_money.get(workspace_id)
            available = (
                0
                if money is None
                else money.total_credits_microdollars
                - money.total_usage_microdollars
                - money.reserved_microdollars
            )
            if money is None or amount > available:
                # Nothing was written yet, so the transfer id stays usable
                # after a top-up — a refused transfer leaves no trace.
                raise ValueError("insufficient credits")
            money.total_credits_microdollars -= amount
            transfer = CreditTransfer(
                id=transfer_id,
                workspace_id=workspace_id,
                amount_microdollars=amount,
                destination=str(destination or ""),
                state=credit_transfer.ESCROWED,
            )
            self.credit_transfers[transfer_id] = transfer
            return transfer

    def get_credit_transfer(self, transfer_id: str) -> CreditTransfer | None:
        with self._lock:
            return self.credit_transfers.get(transfer_id)

    def list_open_credit_transfers(
        self, limit: int = 100, *, after_id: str = ""
    ) -> list[CreditTransfer]:
        """Transfers still in ESCROWED — the recovery queue, paged by id."""
        bounded = max(1, min(int(limit), 500))
        cursor_id = str(after_id or "")
        with self._lock:
            return [
                transfer
                for transfer in sorted(self.credit_transfers.values(), key=lambda t: t.id)
                if transfer.state == credit_transfer.ESCROWED and transfer.id > cursor_id
            ][:bounded]

    def resolve_credit_transfer(self, *, transfer_id: str, outcome: str) -> CreditTransfer:
        """SOURCE side: record the DESTINATION's verdict, and only that."""
        transfer_id = validate_transfer_id(transfer_id)
        outcome = validate_outcome(outcome)
        target_state = credit_transfer.STATE_FOR_OUTCOME[outcome]
        with self._lock:
            existing = self.credit_transfers.get(transfer_id)
            if existing is None:
                raise KeyError(transfer_id)
            if existing.state != credit_transfer.ESCROWED:
                if existing.state != target_state:
                    raise CreditTransferConflict(
                        f"transfer {transfer_id} is {existing.state}; "
                        f"cannot re-resolve it as {target_state}"
                    )
                return existing
            # Look the balance up BEFORE recording the state. The Postgres twin
            # gets this for free — a rowcount != 1 rolls the whole transaction
            # back — but a dict store has no rollback, so writing the state
            # first and then raising leaves the transfer RETURNED with nothing
            # returned. That is value destroyed in the store the conservation
            # tests assert against, i.e. a place a real bug could hide.
            money = (
                self.credit_money.get(existing.workspace_id)
                if outcome == credit_transfer.REJECTED
                else None
            )
            if outcome == credit_transfer.REJECTED and money is None:
                raise RuntimeError(f"missing credit money for workspace {existing.workspace_id}")
            resolved = dataclasses.replace(existing, state=target_state, resolved_at=iso_now())
            self.credit_transfers[transfer_id] = resolved
            if money is not None:
                money.total_credits_microdollars += existing.amount_microdollars
            return resolved

    def claim_credit_transfer(
        self,
        *,
        transfer_id: str,
        workspace_id: str,
        amount_microdollars: int,
        source: str,
        accept: bool,
    ) -> str:
        """DESTINATION side: decide a transfer's fate, exactly once."""
        transfer_id = validate_transfer_id(transfer_id)
        amount = validate_amount(amount_microdollars)
        requested = credit_transfer.ACCEPTED if accept else credit_transfer.REJECTED
        with self._lock:
            recorded = self.credit_transfer_claims.get(transfer_id)
            if recorded is not None:
                # First writer won; every later caller learns that verdict —
                # but only a caller asking about the SAME move. The recorded
                # verdict says nothing about a different (workspace, amount),
                # and replaying it hands a second source plane "accepted" for
                # free: it debited, nothing here was credited.
                credit_transfer.require_matching_transfer(
                    transfer_id,
                    recorded,
                    workspace_id=workspace_id,
                    amount_microdollars=amount,
                    source=str(source or ""),
                )
                return str(recorded["outcome"])
            if requested == credit_transfer.ACCEPTED:
                money = self.credit_money.get(workspace_id)
                if money is None:
                    # No workspace here yet: write no claim, so the source can
                    # retry once it is federated rather than being told a
                    # plane accepted value it never credited.
                    raise ValueError(
                        f"no credit balance for workspace {workspace_id} on this plane"
                    )
                money.total_credits_microdollars += amount
            self.credit_transfer_claims[transfer_id] = {
                "outcome": requested,
                "workspace_id": workspace_id,
                "amount_microdollars": amount,
                "source": str(source or ""),
                "created_at": iso_now(),
            }
            return requested

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
        authorization_id: str | None = None,
        requested_model_id: str | None = None,
        candidate_model_ids: list[str] | None = None,
        region: str | None = None,
        endpoint_id: str | None = None,
        candidate_endpoint_ids: list[str] | None = None,
        idempotency_key: str | None = None,
        tags: dict[str, str] | None = None,
        idempotency_fingerprint: str | None = None,
        app_id: str = "",
        custom_model_id: str | None = None,
        custom_model_revision: int | None = None,
        user_provided_model_id: str | None = None,
        user_provided_model_revision: int | None = None,
        user_model_prompt_price_microdollars_per_m: int | None = None,
        user_model_completion_price_microdollars_per_m: int | None = None,
        user_model_owner_user_id: str | None = None,
        additional_cost_reservation_microdollars: int = 0,
        native_batch_eligible: bool = False,
        settlement: str = "local",
        expires_at: str | None = None,
        deferred_cap_microdollars: int | None = None,
    ) -> GatewayAuthorization:
        return self.api_keys.create_gateway_authorization(
            workspace_id=workspace_id,
            key_hash=key_hash,
            model_id=model_id,
            provider=provider,
            usage_type=usage_type,
            estimated_microdollars=estimated_microdollars,
            credit_reservation_id=credit_reservation_id,
            authorization_id=authorization_id,
            requested_model_id=requested_model_id,
            candidate_model_ids=candidate_model_ids,
            region=region,
            endpoint_id=endpoint_id,
            candidate_endpoint_ids=candidate_endpoint_ids,
            idempotency_key=idempotency_key,
            tags=tags,
            idempotency_fingerprint=idempotency_fingerprint,
            app_id=app_id,
            custom_model_id=custom_model_id,
            custom_model_revision=custom_model_revision,
            user_provided_model_id=user_provided_model_id,
            user_provided_model_revision=user_provided_model_revision,
            user_model_prompt_price_microdollars_per_m=(user_model_prompt_price_microdollars_per_m),
            user_model_completion_price_microdollars_per_m=(
                user_model_completion_price_microdollars_per_m
            ),
            user_model_owner_user_id=user_model_owner_user_id,
            additional_cost_reservation_microdollars=additional_cost_reservation_microdollars,
            native_batch_eligible=native_batch_eligible,
            settlement=settlement,
            expires_at=expires_at,
            deferred_cap_microdollars=deferred_cap_microdollars,
        )

    def get_gateway_authorization(self, authorization_id: str) -> GatewayAuthorization | None:
        return self.api_keys.get_gateway_authorization(authorization_id)

    def get_gateway_authorization_by_idempotency_key(
        self, workspace_id: str, key_hash: str, idempotency_key: str
    ) -> GatewayAuthorization | None:
        return self.api_keys.get_gateway_authorization_by_idempotency_key(
            workspace_id, key_hash, idempotency_key
        )

    def mark_gateway_authorization_settled(self, authorization_id: str) -> None:
        self.api_keys.mark_gateway_authorization_settled(authorization_id)

    def finalize_gateway_authorization(
        self,
        authorization_id: str,
        *,
        success: bool,
        actual_microdollars: int,
        selected_usage_type: UsageType | str,
        generation: Generation | None = None,
    ) -> bool:
        """Atomically finalize gateway billing + usage in the in-memory backend.

        This mirrors the production Spanner transaction: release the credit
        reservation, release key-limit holds, write generation metadata, add
        key usage, and mark the gateway authorization settled under one lock.
        """
        actual_usage_type = UsageType.coerce(selected_usage_type)
        with self._lock:
            authorization = self.api_keys.gateway_authorizations.get(authorization_id)
            if authorization is None or authorization.settled:
                return False

            if authorization.credit_reservation_id is not None:
                if success and actual_usage_type == UsageType.CREDITS:
                    self.api_keys.settle(authorization.credit_reservation_id, actual_microdollars)
                else:
                    self.api_keys.refund(authorization.credit_reservation_id)

            if success:
                self.api_keys.settle_limit(
                    authorization.key_hash,
                    authorization.estimated_microdollars,
                    actual_microdollars,
                    usage_type=authorization.usage_type,
                )
                if generation is not None:
                    self.generation_store.add(generation)
            else:
                self.api_keys.refund_limit(
                    authorization.key_hash,
                    authorization.estimated_microdollars,
                    usage_type=authorization.usage_type,
                )

            authorization.record_finalization(
                success=success,
                actual_microdollars=actual_microdollars,
                selected_usage_type=actual_usage_type,
                generation=generation,
            )
            return True

    # Generations + activity + benchmarks delegate to storage_generations.
    def add_generation(self, generation: Generation) -> None:
        self.generation_store.add(generation)

    def record_client_events_batch(self, payload: dict[str, Any]) -> None:
        event_id = f"{payload['tenant_id']}:{payload['batch_id']}"
        with self._lock:
            if event_id in self.client_event_ids:
                return
            self.client_event_ids.add(event_id)
            self.client_events_batches.append(dict(payload))
            if len(self.client_events_batches) > 1_000:
                removed = self.client_events_batches.pop(0)
                self.client_event_ids.discard(f"{removed['tenant_id']}:{removed['batch_id']}")

    def record_provider_benchmark(self, sample: ProviderBenchmarkSample) -> None:
        self.generation_store.record_benchmark(sample)

    def provider_benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        return self.generation_store.benchmark_samples(
            date=date, provider=provider, model=model, limit=limit
        )

    def record_synthetic_probe_sample(self, sample: SyntheticProbeSample) -> None:
        self.synthetic_store.record(sample)

    def operational_analytics_outbox_freshness(self) -> OutboxFreshness:
        """No outbox exists in memory, and this says so rather than returning 0.

        The in-memory backend never enqueues an operational-analytics row and
        has no drain behind it, so an empty queue here is not evidence that a
        drain is keeping up -- it is evidence that there is nothing to keep up
        with. Reporting `not_configured` keeps a dev or test deployment from
        publishing the healthiest possible number for a pipeline it does not
        run.
        """
        return OutboxFreshness.unavailable(BACKEND_MEMORY, REASON_NOT_CONFIGURED)

    def synthetic_probe_samples(
        self,
        *,
        date: str | None = None,
        target: str | None = None,
        probe_type: str | None = None,
        monitor_region: str | None = None,
        limit: int = 1000,
    ) -> list[SyntheticProbeSample]:
        return self.synthetic_store.query(
            date=date,
            target=target,
            probe_type=probe_type,
            monitor_region=monitor_region,
            limit=limit,
        )

    def synthetic_rollups(
        self,
        *,
        period: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_histograms: bool = True,
        limit: int = 1000,
    ) -> list[SyntheticRollup]:
        return self.synthetic_store.query_rollups(
            period=period,
            since=since,
            until=until,
            include_histograms=include_histograms,
            limit=limit,
        )

    def get_generation(self, generation_id: str) -> Generation | None:
        return self.generation_store.get(generation_id)

    def activity(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        group_by_tag: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.generation_store.activity(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            tag_key=tag_key,
            tag_value=tag_value,
            group_by_tag=group_by_tag,
        )

    def activity_events(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        limit: int = 100,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.generation_store.activity_events(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=limit,
            tag_key=tag_key,
            tag_value=tag_value,
        )

    def activity_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        group_by_tag: str | None = None,
    ) -> Any:
        return self.generation_store.activity_result(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            tag_key=tag_key,
            tag_value=tag_value,
            group_by_tag=group_by_tag,
        )

    def activity_events_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        limit: int = 100,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> Any:
        return self.generation_store.activity_events_result(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=limit,
            tag_key=tag_key,
            tag_value=tag_value,
        )

    def usage_series(
        self,
        workspace_id: str,
        *,
        window_minutes: int,
        granularity: str,
        api_key_hash: str | None = None,
        by_model: bool = False,
    ) -> dict[str, Any]:
        return self.generation_store.usage_series(
            workspace_id,
            window_minutes=window_minutes,
            granularity=granularity,
            api_key_hash=api_key_hash,
            by_model=by_model,
        )

    def reconcile_generation_activity(
        self,
        workspace_id: str,
        *,
        date: str | None = None,
        limit: int = 1000,
    ) -> int:
        return self.generation_store.reconcile_activity(
            workspace_id,
            date=date,
            limit=limit,
        )

    def hit_rate_limit(
        self,
        *,
        namespace: str,
        subject: str,
        limit: int,
        window_seconds: int,
        now: dt.datetime | None = None,
    ) -> RateLimitHit:
        return self.rate_limit_store.hit(
            namespace=namespace,
            subject=subject,
            limit=limit,
            window_seconds=window_seconds,
            now=now,
        )

    # ── Wallet challenges + verification tokens ────────────────────────
    # These delegate to composed feature stores. See
    # storage_wallet_challenges.py and storage_verification_tokens.py.
    def create_wallet_challenge(
        self,
        *,
        address: str,
        message: str,
        ttl_seconds: int,
        raw_nonce: str | None = None,
    ) -> tuple[str, WalletChallenge]:
        return self.wallet_challenges.create(
            address=address,
            message=message,
            ttl_seconds=ttl_seconds,
            raw_nonce=raw_nonce,
        )

    def consume_wallet_challenge(self, raw_nonce: str) -> WalletChallenge | None:
        return self.wallet_challenges.consume(raw_nonce)

    def create_verification_token(
        self,
        *,
        user_id: str,
        purpose: str,
        ttl_seconds: int,
    ) -> tuple[str, VerificationToken]:
        return self.verification_tokens.create(
            user_id=user_id, purpose=purpose, ttl_seconds=ttl_seconds
        )

    def consume_verification_token(
        self, raw_token: str, *, purpose: str
    ) -> VerificationToken | None:
        return self.verification_tokens.consume(raw_token, purpose=purpose)

    # OAuth authorization codes delegate to storage_oauth_codes.
    def create_oauth_authorization_code(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
        callback_url: str,
        key_label: str,
        ttl_seconds: int,
        app_id: int,
        limit_microdollars: int | None = None,
        limit_reset: str | None = None,
        expires_at: str | None = None,
        code_challenge: str | None = None,
        code_challenge_method: str | None = None,
        spawn_agent: str | None = None,
        spawn_cloud: str | None = None,
        client_app_id: str = "",
    ) -> tuple[str, OAuthAuthorizationCode]:
        return self.oauth_code_store.create(
            workspace_id=workspace_id,
            user_id=user_id,
            callback_url=callback_url,
            key_label=key_label,
            ttl_seconds=ttl_seconds,
            app_id=app_id,
            limit_microdollars=limit_microdollars,
            limit_reset=limit_reset,
            expires_at=expires_at,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            spawn_agent=spawn_agent,
            spawn_cloud=spawn_cloud,
            client_app_id=client_app_id,
        )

    def consume_oauth_authorization_code(self, raw_code: str) -> OAuthAuthorizationCode | None:
        return self.oauth_code_store.consume(raw_code)

    def create_oauth_app(self, app: OAuthApp) -> OAuthApp:
        return self.oauth_app_store.create(app)

    def get_oauth_app(self, app_id: str) -> OAuthApp | None:
        return self.oauth_app_store.get(app_id)

    def list_oauth_apps_for_user(self, owner_user_id: str) -> list[OAuthApp]:
        return self.oauth_app_store.list_for_user(owner_user_id)

    def update_oauth_app(
        self,
        app_id: str,
        *,
        patch: dict[str, Any],
    ) -> OAuthApp | None:
        return self.oauth_app_store.update(app_id, patch=patch)

    # ── Email send blocks (SES bounce/complaint suppression) ────────────
    # Delegates to the composed InMemoryEmailBlocks store; see
    # storage_email_blocks.py for the data + logic.
    def block_email_sending(
        self,
        *,
        email: str,
        reason: str,
        bounce_type: str | None = None,
        feedback_id: str | None = None,
        mail_class: str | None = None,
        sender_profile: str | None = None,
        acquisition_source: str | None = None,
        acquisition_medium: str | None = None,
        acquisition_campaign: str | None = None,
    ) -> EmailSendBlock:
        return self.email_blocks.block(
            email=email,
            reason=reason,
            bounce_type=bounce_type,
            feedback_id=feedback_id,
            mail_class=mail_class,
            sender_profile=sender_profile,
            acquisition_source=acquisition_source,
            acquisition_medium=acquisition_medium,
            acquisition_campaign=acquisition_campaign,
        )

    def is_email_blocked(self, email: str) -> bool:
        return self.email_blocks.is_blocked(email)

    def get_email_block(self, email: str) -> EmailSendBlock | None:
        return self.email_blocks.get(email)

    def record_sns_message_once(self, message_id: str) -> bool:
        return self.email_blocks.record_message_once(message_id)

    def record_webhook_event_once(self, source: str, event_id: str) -> bool:
        with self._lock:
            key = (source, event_id)
            if key in self.webhook_events:
                return False
            self.webhook_events.add(key)
            return True


#: Analytics mirror. No-op until the app factory installs a real one, so
#: importing this module never starts a background thread (tests, CLIs).
_ANALYTICS_SINK: AnalyticsSink = NullAnalyticsSink()


class _StoreProxy:
    """Singleton that forwards method calls to the active backend.

    Tests build an `InMemoryStore` and call `configure_store(...)`;
    production builds a `SpannerBigtableStore` from the same call site.
    Both are siblings under the `Store` Protocol — there's no runtime
    inheritance, so a method missing from `SpannerBigtableStore` is a
    static-typing error at the call site (where `STORE: Store` is
    consulted) rather than a silent in-process fallback.

    The proxy exposes `target` as `InMemoryStore` for the test suite,
    which inspects the backend's instance dicts directly. Production
    code routes through `STORE: Store` (the Protocol) and never reaches
    those attributes.
    """

    def __init__(self, initial: Store | None = None) -> None:
        self._lock = threading.RLock()
        self._target: Store = initial or InMemoryStore()

    def _configure(self, target: Store) -> None:
        with self._lock:
            self._target = target

    @property
    def target(self) -> Store:
        with self._lock:
            return self._target

    @property
    def in_memory_target(self) -> InMemoryStore:
        """Tests inspect backend dicts directly — they only run against
        InMemoryStore. Use this in tests instead of casting STORE."""
        target = self.target
        if not isinstance(target, InMemoryStore):
            raise TypeError("in_memory_target is only valid for the InMemoryStore backend")
        return target

    def record_provider_benchmark(self, sample: Any) -> None:
        """Write to the authoritative store, then mirror to analytics.

        Defined explicitly rather than falling through `__getattr__` because
        this is the single chokepoint every caller already routes through, so
        the fan-out cannot miss a call site. It is deliberately NOT a wrapper
        object around the store: `typed_billing_store()` unwraps this proxy to
        do its capability check, and an extra layer it did not know about
        would make that check read False and silently route typed billing down
        the legacy path.

        The store write is authoritative and its exceptions propagate. The
        analytics mirror is best-effort and, by the sink's contract, cannot
        raise.
        """
        self.target.record_provider_benchmark(sample)
        _ANALYTICS_SINK.record_benchmark_sample(sample)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


from trusted_router.store_protocol import (  # noqa: E402 - forward dep on Store protocols.
    Store,
    TypedBillingStore,
)

_STORE_PROXY = _StoreProxy()
STORE: Store = cast(Store, _STORE_PROXY)


def configure_store(target: Store) -> None:
    _STORE_PROXY._configure(target)


def typed_billing_store(store: Any = None) -> TypedBillingStore | None:
    """The live store (or the given `store`) as a TypedBillingStore, or None
    when the backend lacks the typed-Spanner capability.

    MUST be used instead of ``isinstance(STORE, TypedBillingStore)``: the module
    STORE is a ``_StoreProxy`` and ``isinstance`` against a runtime_checkable
    Protocol does NOT see methods that are only reachable through the proxy's
    ``__getattr__`` — so the guard would read False even for a typed Spanner
    backend and silently route every typed authorize/settle down the legacy
    path (codex #97). Unwrap the proxy to its real target and check that."""
    target = _STORE_PROXY.target if store is None else store
    if isinstance(target, _StoreProxy):
        target = target.target
    return target if isinstance(target, TypedBillingStore) else None


def configure_analytics_sink(sink: AnalyticsSink) -> None:
    """Install the analytics mirror. Called once from the app factory."""
    global _ANALYTICS_SINK
    _ANALYTICS_SINK = sink


def create_store(settings: Any) -> Store:
    backend = str(getattr(settings, "storage_backend", "memory")).lower()
    if backend == "memory":
        return InMemoryStore()
    if backend == "postgres":
        # Postgres-wire system of record for the non-GCP deployments. The same
        # implementation runs on Azure Flexible Server / Citus, AWS Aurora DSQL,
        # and Spanner's PostgreSQL dialect, which is why there is one backend
        # here and not three.
        from trusted_router.storage_postgres import PostgresStore

        dsn = str(getattr(settings, "postgres_dsn", "") or "")
        if not dsn:
            raise ValueError(
                "TR_STORAGE_BACKEND=postgres requires TR_POSTGRES_DSN. "
                "Without it the process would start and fail on first query."
            )
        store = PostgresStore(
            dsn,
            postgres_iam_auth=str(getattr(settings, "postgres_iam_auth", "") or ""),
            postgres_iam_region=str(getattr(settings, "postgres_iam_region", "") or ""),
            operational_analytics_outbox_enabled=bool(
                getattr(settings, "operational_analytics_outbox_enabled", False)
            ),
        )
        store.apply_schema()
        return store
    if backend in {"spanner-bigtable", "spanner-clickhouse"}:
        from trusted_router.storage_gcp import SpannerBigtableStore

        bigtable_enabled = backend == "spanner-bigtable"
        return SpannerBigtableStore(
            project_id=settings.gcp_project_id,
            spanner_instance_id=settings.spanner_instance_id,
            spanner_database_id=settings.spanner_database_id,
            bigtable_instance_id=settings.bigtable_instance_id,
            generation_table=settings.bigtable_generation_table,
            bigtable_app_profile_id=getattr(settings, "bigtable_app_profile_id", ""),
            bigtable_enabled=bigtable_enabled,
            bigtable_writes_enabled=bigtable_enabled
            and getattr(settings, "bigtable_mirror_writes_enabled", True),
            generation_records_enabled=getattr(
                settings,
                "generation_records_enabled",
                False,
            ),
            request_record_write_mode=getattr(settings, "request_record_write_mode", "legacy"),
            analytics_outbox_enabled=getattr(settings, "analytics_outbox_enabled", False),
            operational_analytics_outbox_enabled=getattr(
                settings,
                "operational_analytics_outbox_enabled",
                False,
            ),
            operational_analytics_sink=getattr(settings, "operational_analytics_sink", "outbox"),
            operational_analytics_clickhouse_write_user=getattr(
                settings, "operational_analytics_clickhouse_write_user", "tr"
            ),
            operational_analytics_clickhouse_write_password=getattr(
                settings, "operational_analytics_clickhouse_write_password", ""
            ),
            operational_analytics_clickhouse_url=getattr(
                settings, "operational_analytics_clickhouse_url", ""
            ),
            operational_analytics_clickhouse_user=getattr(
                settings,
                "operational_analytics_clickhouse_user",
                "tr_control_read",
            ),
            operational_analytics_clickhouse_password=getattr(
                settings, "operational_analytics_clickhouse_password", ""
            ),
            operational_analytics_clickhouse_database=getattr(
                settings, "operational_analytics_clickhouse_database", "tr"
            ),
            analytics_read_mode=(
                "clickhouse-only"
                if backend == "spanner-clickhouse"
                else getattr(settings, "analytics_read_mode", "bigtable")
            ),
            analytics_dual_read_grace_seconds=getattr(
                settings, "analytics_dual_read_grace_seconds", 30
            ),
            regional_quota_leases_enabled=getattr(settings, "regional_quota_leases_enabled", False),
            regional_quota_bigtable_table=getattr(
                settings,
                "regional_quota_bigtable_table",
                "trustedrouter-regional-quota",
            ),
            regional_quota_bigtable_app_profiles=getattr(
                settings,
                "regional_quota_bigtable_app_profile_map",
                {},
            ),
        )
    raise ValueError(f"unsupported storage backend: {backend}")


def _parse_iso_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if "@" not in normalized:
        normalized = f"{normalized}@trustedrouter.local"
    return normalized
