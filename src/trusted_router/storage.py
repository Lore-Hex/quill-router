from __future__ import annotations

import datetime as dt
import threading
import uuid
from typing import Any, cast

from trusted_router.money import DEFAULT_TRIAL_CREDIT_MICRODOLLARS
from trusted_router.storage_auth_sessions import InMemoryAuthSessions
from trusted_router.storage_byok import InMemoryByok
from trusted_router.storage_email_blocks import InMemoryEmailBlocks
from trusted_router.storage_generations import InMemoryGenerations
from trusted_router.storage_keys import InMemoryApiKeys
from trusted_router.storage_models import (
    ApiKey,
    AuthSession,
    ByokProviderConfig,
    CreditAccount,
    EmailSendBlock,
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
    iso_now,
)
from trusted_router.storage_oauth_codes import InMemoryOAuthCodes
from trusted_router.storage_rate_limits import InMemoryRateLimits
from trusted_router.storage_verification_tokens import InMemoryVerificationTokens
from trusted_router.storage_wallet_challenges import InMemoryWalletChallenges
from trusted_router.types import UsageType


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
        self.workspaces: dict[str, Workspace] = {}
        self.members: dict[tuple[str, str], Member] = {}
        self.credits: dict[str, CreditAccount] = {}
        self.stripe_events: set[str] = set()
        # Composed feature stores. Each owns its own state and is importable
        # on its own. Keeps storage.py focused on identity + credit ledger;
        # spend control / BYOK / OAuth codes / auth sessions / generations /
        # rate limits / wallet / SES all live in their own modules.
        self.api_keys = InMemoryApiKeys(
            credits_by_workspace=self.credits,
            lock=self._lock,
        )
        self.generation_store = InMemoryGenerations(
            lock=self._lock,
            add_usage_to_key=self.api_keys.add_usage,
        )
        self.byok_store = InMemoryByok(lock=self._lock)
        self.auth_session_store = InMemoryAuthSessions(lock=self._lock)
        self.oauth_code_store = InMemoryOAuthCodes(lock=self._lock)
        self.rate_limit_store = InMemoryRateLimits(lock=self._lock)
        self.wallet_challenges = InMemoryWalletChallenges()
        self.verification_tokens = InMemoryVerificationTokens()
        self.email_blocks = InMemoryEmailBlocks()

    def reset(self) -> None:
        with self._lock:
            self.users.clear()
            self.user_ids_by_email.clear()
            self.user_ids_by_wallet.clear()
            self.workspaces.clear()
            self.members.clear()
            self.credits.clear()
            self.stripe_events.clear()
            self.api_keys.reset()
            self.generation_store.reset()
            self.byok_store.reset()
            self.auth_session_store.reset()
            self.oauth_code_store.reset()
            self.rate_limit_store.reset()
            self.wallet_challenges.reset()
            self.verification_tokens.reset()
            self.email_blocks.reset()

    def ensure_user(self, user_id: str, email: str | None = None) -> User:
        with self._lock:
            normalized_email = _normalize_email(email or user_id)
            existing_id = self.user_ids_by_email.get(normalized_email)
            if existing_id is not None:
                return self.users[existing_id]

            new_id = str(uuid.uuid4())
            self.users[new_id] = User(id=new_id, email=normalized_email)
            self.user_ids_by_email[normalized_email] = new_id
            self.create_workspace(owner_user_id=new_id, name="Personal Workspace")
            return self.users[new_id]

    def signup(
        self,
        *,
        email: str,
        workspace_name: str | None = None,
    ) -> SignupResult | None:
        """Atomically create a new account end-to-end. Returns None if the
        email is already registered."""
        with self._lock:
            if self.user_ids_by_email.get(_normalize_email(email)) is not None:
                return None
            user = self.ensure_user(email, email=email)
            workspace = self.list_workspaces_for_user(user.id)[0]
            if workspace_name:
                workspace.name = workspace_name
            raw_key, api_key = self.create_api_key(
                workspace_id=workspace.id,
                name="Signup key",
                creator_user_id=user.id,
                management=True,
            )
            trial = self.credits[workspace.id].total_credits_microdollars
        return SignupResult(
            user=user,
            workspace=workspace,
            raw_key=raw_key,
            api_key=api_key,
            trial_credit_microdollars=trial,
        )

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
            self.credits[workspace.id] = CreditAccount(
                workspace_id=workspace.id,
                total_credits_microdollars=(
                    DEFAULT_TRIAL_CREDIT_MICRODOLLARS
                    if trial_credit_microdollars is None
                    else trial_credit_microdollars
                ),
            )
            return workspace

    def list_workspaces_for_user(self, user_id: str) -> list[Workspace]:
        with self._lock:
            ids = [wid for (wid, uid), member in self.members.items() if uid == user_id and member.role]
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
    ) -> Workspace | None:
        with self._lock:
            workspace = self.workspaces.get(workspace_id)
            if workspace is None:
                return None
            if name is not None:
                workspace.name = name
            if deleted is not None:
                workspace.deleted = deleted
            return workspace

    def get_credit_account(self, workspace_id: str) -> CreditAccount | None:
        with self._lock:
            return self.credits.get(workspace_id)

    def add_members(self, workspace_id: str, emails: list[str], role: str = "member") -> list[Member]:
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
            return [
                member
                for (wid, _), member in self.members.items()
                if wid == workspace_id
            ]

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
        )

    def get_key_by_hash(self, key_hash: str) -> ApiKey | None:
        return self.api_keys.get_by_hash(key_hash)

    def get_key_by_raw(self, raw_key: str) -> ApiKey | None:
        return self.api_keys.get_by_raw(raw_key)

    def list_keys(self, workspace_id: str) -> list[ApiKey]:
        return self.api_keys.list_for_workspace(workspace_id)

    def delete_key(self, key_hash: str) -> bool:
        return self.api_keys.delete(key_hash)

    def reserve_key_limit(
        self,
        key_hash: str,
        amount_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        self.api_keys.reserve_limit(key_hash, amount_microdollars, usage_type=usage_type)

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
    ) -> ByokProviderConfig:
        return self.byok_store.upsert(
            workspace_id=workspace_id,
            provider=provider,
            secret_ref=secret_ref,
            key_hint=key_hint,
        )

    def list_byok_providers(self, workspace_id: str) -> list[ByokProviderConfig]:
        return self.byok_store.list_for_workspace(workspace_id)

    def get_byok_provider(self, workspace_id: str, provider: str) -> ByokProviderConfig | None:
        return self.byok_store.get(workspace_id, provider)

    def delete_byok_provider(self, workspace_id: str, provider: str) -> bool:
        return self.byok_store.delete(workspace_id, provider)

    def credit_workspace_once(self, workspace_id: str, amount_microdollars: int, event_id: str) -> bool:
        with self._lock:
            if event_id in self.stripe_events:
                return False
            self.stripe_events.add(event_id)
            account = self.credits[workspace_id]
            account.total_credits_microdollars += amount_microdollars
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

    def reserve(self, workspace_id: str, key_hash: str, amount_microdollars: int) -> Reservation:
        return self.api_keys.reserve(workspace_id, key_hash, amount_microdollars)

    def settle(self, reservation_id: str, actual_microdollars: int) -> None:
        self.api_keys.settle(reservation_id, actual_microdollars)

    def refund(self, reservation_id: str) -> None:
        self.api_keys.refund(reservation_id)

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
        requested_model_id: str | None = None,
        candidate_model_ids: list[str] | None = None,
        region: str | None = None,
        endpoint_id: str | None = None,
        candidate_endpoint_ids: list[str] | None = None,
    ) -> GatewayAuthorization:
        return self.api_keys.create_gateway_authorization(
            workspace_id=workspace_id,
            key_hash=key_hash,
            model_id=model_id,
            provider=provider,
            usage_type=usage_type,
            estimated_microdollars=estimated_microdollars,
            credit_reservation_id=credit_reservation_id,
            requested_model_id=requested_model_id,
            candidate_model_ids=candidate_model_ids,
            region=region,
            endpoint_id=endpoint_id,
            candidate_endpoint_ids=candidate_endpoint_ids,
        )

    def get_gateway_authorization(self, authorization_id: str) -> GatewayAuthorization | None:
        return self.api_keys.get_gateway_authorization(authorization_id)

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
                    self.api_keys.settle(
                        authorization.credit_reservation_id, actual_microdollars
                    )
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

            authorization.settled = True
            return True

    # Generations + activity + benchmarks delegate to storage_generations.
    def add_generation(self, generation: Generation) -> None:
        self.generation_store.add(generation)

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

    def get_generation(self, generation_id: str) -> Generation | None:
        return self.generation_store.get(generation_id)

    def activity(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.generation_store.activity(
            workspace_id, api_key_hash=api_key_hash, date=date
        )

    def activity_events(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.generation_store.activity_events(
            workspace_id, api_key_hash=api_key_hash, date=date, limit=limit
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
        )

    def consume_oauth_authorization_code(self, raw_code: str) -> OAuthAuthorizationCode | None:
        return self.oauth_code_store.consume(raw_code)

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
    ) -> EmailSendBlock:
        return self.email_blocks.block(
            email=email,
            reason=reason,
            bounce_type=bounce_type,
            feedback_id=feedback_id,
        )

    def is_email_blocked(self, email: str) -> bool:
        return self.email_blocks.is_blocked(email)

    def get_email_block(self, email: str) -> EmailSendBlock | None:
        return self.email_blocks.get(email)

    def record_sns_message_once(self, message_id: str) -> bool:
        return self.email_blocks.record_message_once(message_id)


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

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


from trusted_router.store_protocol import Store  # noqa: E402 - forward dep on Store Protocol.

_STORE_PROXY = _StoreProxy()
STORE: Store = cast(Store, _STORE_PROXY)


def configure_store(target: Store) -> None:
    _STORE_PROXY._configure(target)


def create_store(settings: Any) -> Store:
    backend = str(getattr(settings, "storage_backend", "memory")).lower()
    if backend == "memory":
        return InMemoryStore()
    if backend == "spanner-bigtable":
        from trusted_router.storage_gcp import SpannerBigtableStore

        return SpannerBigtableStore(
            project_id=settings.gcp_project_id,
            spanner_instance_id=settings.spanner_instance_id,
            spanner_database_id=settings.spanner_database_id,
            bigtable_instance_id=settings.bigtable_instance_id,
            generation_table=settings.bigtable_generation_table,
        )
    raise ValueError(f"unsupported storage backend: {backend}")


def _normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if "@" not in normalized:
        normalized = f"{normalized}@trustedrouter.local"
    return normalized
