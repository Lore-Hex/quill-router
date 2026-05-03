from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any, TypeVar

from trusted_router.money import DEFAULT_TRIAL_CREDIT_MICRODOLLARS
from trusted_router.storage import (
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
from trusted_router.storage_gcp_auth_sessions import SpannerAuthSessions
from trusted_router.storage_gcp_byok import SpannerByok
from trusted_router.storage_gcp_codec import (
    generation_workspace_id as _generation_workspace_id,
)
from trusted_router.storage_gcp_codec import (
    json_body as _json_body,
)
from trusted_router.storage_gcp_codec import (
    member_id as _member_id,
)
from trusted_router.storage_gcp_codec import (
    normalize_email as _normalize_email,
)
from trusted_router.storage_gcp_email_blocks import SpannerEmailBlocks
from trusted_router.storage_gcp_generations import SpannerGenerations
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_gcp_keys import SpannerApiKeys
from trusted_router.storage_gcp_oauth_codes import SpannerOAuthCodes
from trusted_router.storage_gcp_rate_limits import SpannerRateLimits
from trusted_router.storage_gcp_verification_tokens import SpannerVerificationTokens
from trusted_router.storage_gcp_wallet_challenges import SpannerWalletChallenges
from trusted_router.storage_models import _is_byok
from trusted_router.types import UsageType

T = TypeVar("T")
log = logging.getLogger(__name__)


class SpannerBigtableStore:
    """Production Spanner + Bigtable implementation.

    Spanner owns strongly consistent control-plane state: users, orgs, API
    keys, reservations, credit ledger state, BYOK metadata, and Stripe event
    idempotency. Bigtable receives append-oriented generation metadata rows.

    Sibling of `InMemoryStore` rather than subclass — both implement the
    `Store` Protocol. The intentional non-inheritance means a method
    that exists on InMemoryStore but is missing here is a static-typing
    error the moment it's called via `Store`, not a silent runtime
    fallback to in-process dict access in production.
    """

    entity_table = "tr_entities"
    generation_family = "m"

    def __init__(
        self,
        *,
        project_id: str,
        spanner_instance_id: str,
        spanner_database_id: str,
        bigtable_instance_id: str,
        generation_table: str,
    ) -> None:
        if not spanner_instance_id or not spanner_database_id or not bigtable_instance_id:
            raise ValueError("Spanner and Bigtable IDs are required")
        try:
            from google.cloud import bigtable, spanner
            from google.cloud.spanner_v1 import param_types
        except ImportError as exc:  # pragma: no cover - exercised in prod image.
            raise RuntimeError(
                "Install google-cloud-spanner and google-cloud-bigtable for "
                "TR_STORAGE_BACKEND=spanner-bigtable"
            ) from exc

        self._spanner = spanner
        self._param_types = param_types
        self._database = (
            spanner.Client(project=project_id)
            .instance(spanner_instance_id)
            .database(spanner_database_id)
        )
        self._bt_table = (
            bigtable.Client(project=project_id, admin=True)
            .instance(bigtable_instance_id)
            .table(generation_table)
        )
        # Composed feature stores. Each owns its own logic and is importable
        # on its own — keeps the core SpannerBigtableStore body focused on
        # identity + credit ledger. Mirrors the InMemoryStore pattern.
        io = SpannerIO(
            database=self._database,
            write_entity_batch=self._write_entity_batch,
            read_entity_tx=self._read_entity_tx,
            write_entity_tx=self._write_entity_tx,
            write_entity=self._write_entity,
            read_entity=self._read_entity,
            list_entities=self._list_entities,
            delete_entities=self._delete_entities,
            delete_entities_tx=self._delete_entities_tx,
        )
        self.api_keys = SpannerApiKeys(io)
        self.generation_store = SpannerGenerations(
            io,
            bt_table=self._bt_table,
            generation_family=self.generation_family,
            add_usage_to_key=self.api_keys.add_usage,
        )
        self.byok_store = SpannerByok(io)
        self.auth_session_store = SpannerAuthSessions(io)
        self.oauth_code_store = SpannerOAuthCodes(io)
        self.rate_limit_store = SpannerRateLimits(io)
        self.wallet_challenges = SpannerWalletChallenges(io)
        self.verification_tokens = SpannerVerificationTokens(io)
        self.email_blocks = SpannerEmailBlocks(io)

    def reset(self) -> None:
        raise RuntimeError("refusing to reset production Spanner/Bigtable store")

    def ensure_user(self, user_id: str, email: str | None = None) -> User:
        normalized_email = _normalize_email(email or user_id)

        def txn(transaction: Any) -> User:
            existing = self._read_entity_tx(transaction, "email_user", normalized_email, dict)
            if existing is not None:
                user = self._read_entity_tx(transaction, "user", existing["user_id"], User)
                if user is not None:
                    return user

            new_user = User(id=str(uuid.uuid4()), email=normalized_email)
            workspace = Workspace(
                id=str(uuid.uuid4()),
                name="Personal Workspace",
                owner_user_id=new_user.id,
            )
            member = Member(workspace_id=workspace.id, user_id=new_user.id, role="owner")
            credit = CreditAccount(
                workspace_id=workspace.id,
                total_credits_microdollars=DEFAULT_TRIAL_CREDIT_MICRODOLLARS,
            )
            self._write_entity_tx(transaction, "user", new_user.id, new_user)
            self._write_entity_tx(transaction, "email_user", normalized_email, {"user_id": new_user.id})
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            self._write_entity_tx(transaction, "member", _member_id(workspace.id, new_user.id), member)
            self._write_entity_tx(transaction, "credit", workspace.id, credit)
            return new_user

        return self._database.run_in_transaction(txn)

    def signup(
        self,
        *,
        email: str,
        workspace_name: str | None = None,
    ) -> SignupResult | None:
        if self.find_user_by_email(email) is not None:
            return None
        user = self.ensure_user(email, email=email)
        workspace = self.list_workspaces_for_user(user.id)[0]
        if workspace_name:
            workspace.name = workspace_name
            self._write_entity("workspace", workspace.id, workspace)
        raw_key, api_key = self.create_api_key(
            workspace_id=workspace.id,
            name="Signup key",
            creator_user_id=user.id,
            management=True,
        )
        credit = self.get_credit_account(workspace.id)
        return SignupResult(
            user=user,
            workspace=workspace,
            raw_key=raw_key,
            api_key=api_key,
            trial_credit_microdollars=credit.total_credits_microdollars if credit else 0,
        )

    # Auth sessions delegate to storage_gcp_auth_sessions.SpannerAuthSessions.
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

    def create_workspace(self, owner_user_id: str, name: str) -> Workspace:
        workspace = Workspace(id=str(uuid.uuid4()), name=name, owner_user_id=owner_user_id)
        member = Member(workspace_id=workspace.id, user_id=owner_user_id, role="owner")
        credit = CreditAccount(
            workspace_id=workspace.id,
            total_credits_microdollars=DEFAULT_TRIAL_CREDIT_MICRODOLLARS,
        )
        with self._database.batch() as batch:
            self._write_entity_batch(batch, "workspace", workspace.id, workspace)
            self._write_entity_batch(batch, "member", _member_id(workspace.id, owner_user_id), member)
            self._write_entity_batch(batch, "credit", workspace.id, credit)
        return workspace

    def list_workspaces_for_user(self, user_id: str) -> list[Workspace]:
        members = self._list_entities("member", suffix=f"#{user_id}", cls=Member)
        workspaces: list[Workspace] = []
        for member in members:
            if not member.role:
                continue
            workspace = self.get_workspace(member.workspace_id)
            if workspace is not None:
                workspaces.append(workspace)
        return workspaces

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        workspace = self._read_entity("workspace", workspace_id, Workspace)
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
        def txn(transaction: Any) -> Workspace | None:
            workspace = self._read_entity_tx(transaction, "workspace", workspace_id, Workspace)
            if workspace is None:
                return None
            if name is not None:
                workspace.name = name
            if deleted is not None:
                workspace.deleted = deleted
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            return None if workspace.deleted else workspace

        return self._database.run_in_transaction(txn)

    def get_credit_account(self, workspace_id: str) -> CreditAccount | None:
        return self._read_entity("credit", workspace_id, CreditAccount)

    def add_members(self, workspace_id: str, emails: list[str], role: str = "member") -> list[Member]:
        members: list[Member] = []
        with self._database.batch() as batch:
            for email in emails:
                user = self.ensure_user(email)
                member = Member(workspace_id=workspace_id, user_id=user.id, role=role)
                self._write_entity_batch(batch, "member", _member_id(workspace_id, user.id), member)
                members.append(member)
        return members

    def remove_members(self, workspace_id: str, user_ids: list[str]) -> None:
        ids: list[str] = []
        for identifier in user_ids:
            user_id = self._resolve_user_identifier(identifier)
            if user_id is not None:
                ids.append(_member_id(workspace_id, user_id))
        if ids:
            self._delete_entities("member", ids)

    def list_members(self, workspace_id: str) -> list[Member]:
        return self._list_entities("member", prefix=f"{workspace_id}#", cls=Member)

    def user_can_manage(self, user_id: str, workspace_id: str) -> bool:
        member = self._read_entity("member", _member_id(workspace_id, user_id), Member)
        return member is not None and member.role in {"owner", "admin"}

    def user_is_member(self, user_id: str, workspace_id: str) -> bool:
        return self._read_entity("member", _member_id(workspace_id, user_id), Member) is not None

    def get_user(self, user_id: str) -> User | None:
        return self._read_entity("user", user_id, User)

    def find_user_by_email(self, email: str) -> User | None:
        record = self._read_entity("email_user", _normalize_email(email), dict)
        if not record:
            return None
        return self.get_user(str(record["user_id"]))

    def find_user_by_wallet(self, address: str) -> User | None:
        record = self._read_entity("wallet_user", address.strip().lower(), dict)
        if not record:
            return None
        return self.get_user(str(record["user_id"]))

    def create_wallet_user(self, address: str) -> User:
        normalized = address.strip().lower()
        existing = self.find_user_by_wallet(normalized)
        if existing is not None:
            return existing

        def txn(transaction: Any) -> User:
            existing_record = self._read_entity_tx(transaction, "wallet_user", normalized, dict)
            if existing_record is not None:
                user = self._read_entity_tx(transaction, "user", existing_record["user_id"], User)
                if user is not None:
                    return user
            new_user = User(id=str(uuid.uuid4()), email=None, wallet_address=normalized)
            workspace = Workspace(
                id=str(uuid.uuid4()),
                name="Personal Workspace",
                owner_user_id=new_user.id,
            )
            member = Member(workspace_id=workspace.id, user_id=new_user.id, role="owner")
            credit = CreditAccount(
                workspace_id=workspace.id,
                total_credits_microdollars=DEFAULT_TRIAL_CREDIT_MICRODOLLARS,
            )
            self._write_entity_tx(transaction, "user", new_user.id, new_user)
            self._write_entity_tx(transaction, "wallet_user", normalized, {"user_id": new_user.id})
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            self._write_entity_tx(transaction, "member", _member_id(workspace.id, new_user.id), member)
            self._write_entity_tx(transaction, "credit", workspace.id, credit)
            return new_user

        return self._database.run_in_transaction(txn)

    def set_user_email(self, user_id: str, email: str) -> User | None:
        normalized_email = _normalize_email(email)

        def txn(transaction: Any) -> User | None:
            existing = self._read_entity_tx(transaction, "email_user", normalized_email, dict)
            if existing is not None and existing["user_id"] != user_id:
                return None
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None:
                return None
            previous_email = _normalize_email(user.email) if user.email else None
            if previous_email and previous_email != normalized_email:
                transaction.delete(
                    self.entity_table,
                    self._spanner.KeySet(keys=[("email_user", previous_email)]),
                )
            user.email = normalized_email
            if previous_email != normalized_email:
                user.email_verified = False
            self._write_entity_tx(transaction, "user", user.id, user)
            self._write_entity_tx(transaction, "email_user", normalized_email, {"user_id": user.id})
            return user

        return self._database.run_in_transaction(txn)

    def mark_user_email_verified(self, user_id: str) -> User | None:
        def txn(transaction: Any) -> User | None:
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None:
                return None
            user.email_verified = True
            self._write_entity_tx(transaction, "user", user.id, user)
            return user

        return self._database.run_in_transaction(txn)

    # OAuth authorization codes delegate to storage_gcp_oauth_codes.
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

    # API key + per-key spend cap. The actual logic lives in
    # storage_gcp_keys.SpannerApiKeys; these methods are thin delegations.
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

    def update_key(self, key_hash: str, patch: dict[str, Any]) -> ApiKey | None:
        return self.api_keys.update(key_hash, patch)

    # BYOK delegates to storage_gcp_byok.SpannerByok.
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
        def txn(transaction: Any) -> bool:
            if self._read_entity_tx(transaction, "stripe_event", event_id, dict) is not None:
                return False
            account = self._require_credit_tx(transaction, workspace_id)
            account.total_credits_microdollars += amount_microdollars
            self._write_entity_tx(transaction, "credit", workspace_id, account)
            self._write_entity_tx(transaction, "stripe_event", event_id, {"created_at": iso_now()})
            return True

        return self._database.run_in_transaction(txn)

    def update_auto_refill_settings(
        self,
        workspace_id: str,
        *,
        enabled: bool,
        threshold_microdollars: int,
        amount_microdollars: int,
    ) -> CreditAccount | None:
        def txn(transaction: Any) -> CreditAccount | None:
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
            if account is None:
                return None
            account.auto_refill_enabled = enabled
            account.auto_refill_threshold_microdollars = max(0, threshold_microdollars)
            account.auto_refill_amount_microdollars = max(0, amount_microdollars)
            self._write_entity_tx(transaction, "credit", workspace_id, account)
            return account

        return self._database.run_in_transaction(txn)

    def set_stripe_customer(
        self,
        workspace_id: str,
        *,
        customer_id: str,
        payment_method_id: str | None = None,
    ) -> CreditAccount | None:
        def txn(transaction: Any) -> CreditAccount | None:
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
            if account is None:
                return None
            account.stripe_customer_id = customer_id
            if payment_method_id is not None:
                account.stripe_payment_method_id = payment_method_id
            self._write_entity_tx(transaction, "credit", workspace_id, account)
            return account

        return self._database.run_in_transaction(txn)

    def record_auto_refill_outcome(
        self,
        workspace_id: str,
        *,
        status: str,
    ) -> CreditAccount | None:
        def txn(transaction: Any) -> CreditAccount | None:
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
            if account is None:
                return None
            account.last_auto_refill_at = iso_now()
            account.last_auto_refill_status = status
            self._write_entity_tx(transaction, "credit", workspace_id, account)
            return account

        return self._database.run_in_transaction(txn)

    def reserve(
        self, workspace_id: str, key_hash: str, amount_microdollars: int
    ) -> Reservation:
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

    def get_gateway_authorization(
        self, authorization_id: str
    ) -> GatewayAuthorization | None:
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
        actual_usage_type = UsageType.coerce(selected_usage_type)

        def txn(transaction: Any) -> bool:
            authorization = self._read_entity_tx(
                transaction, "gateway_authorization", authorization_id, GatewayAuthorization
            )
            if authorization is None or authorization.settled:
                return False

            if authorization.credit_reservation_id is not None:
                reservation = self._read_entity_tx(
                    transaction,
                    "reservation",
                    authorization.credit_reservation_id,
                    Reservation,
                )
                if reservation is None:
                    raise ValueError("gateway reservation not found")
                if not reservation.settled:
                    account = self._require_credit_tx(
                        transaction, reservation.workspace_id
                    )
                    account.reserved_microdollars -= reservation.amount_microdollars
                    if success and actual_usage_type == UsageType.CREDITS:
                        account.total_usage_microdollars += actual_microdollars
                    reservation.settled = True
                    self._write_entity_tx(
                        transaction, "credit", account.workspace_id, account
                    )
                    self._write_entity_tx(
                        transaction, "reservation", reservation.id, reservation
                    )

            key = self._read_entity_tx(transaction, "api_key", authorization.key_hash, ApiKey)
            if key is not None:
                if key.limit_microdollars is not None and not (
                    _is_byok(authorization.usage_type) and not key.include_byok_in_limit
                ):
                    key.reserved_microdollars = max(
                        0,
                        key.reserved_microdollars
                        - authorization.estimated_microdollars,
                    )
                if success and generation is not None:
                    if _is_byok(generation.usage_type):
                        key.byok_usage_microdollars += generation.total_cost_microdollars
                    else:
                        key.usage_microdollars += generation.total_cost_microdollars
                self._write_entity_tx(transaction, "api_key", key.hash, key)

            if success and generation is not None:
                self._write_entity_tx(transaction, "generation", generation.id, generation)
                self._write_entity_tx(
                    transaction,
                    "generation_by_workspace",
                    _generation_workspace_id(generation),
                    {"generation_id": generation.id},
                )

            authorization.settled = True
            self._write_entity_tx(
                transaction,
                "gateway_authorization",
                authorization.id,
                authorization,
            )
            return True

        finalized = self._database.run_in_transaction(txn)
        if finalized and success and generation is not None:
            self.generation_store.index_after_commit(generation)
        return bool(finalized)

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

    # Generations + activity + benchmarks delegate to storage_gcp_generations.
    def add_generation(self, generation: Generation) -> None:
        self.generation_store.add(generation)

    def get_generation(self, generation_id: str) -> Generation | None:
        return self.generation_store.get(generation_id)

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

    def reconcile_generation_activity(
        self,
        workspace_id: str,
        *,
        date: str | None = None,
        limit: int = 1000,
    ) -> int:
        return self.generation_store.reconcile_activity(
            workspace_id, date=date, limit=limit
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

    # Wallet/verification/email-block delegations. The actual logic lives in
    # storage_gcp_wallet_challenges / _verification_tokens / _email_blocks
    # so this module stays focused on the core ledger. Mirrors InMemoryStore.
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

    def _resolve_user_identifier(self, identifier: str) -> str | None:
        user = self._read_entity("user", identifier, User)
        if user is not None:
            return user.id
        email_user = self._read_entity("email_user", _normalize_email(identifier), dict)
        if email_user:
            return str(email_user["user_id"])
        return None

    def _require_credit_tx(self, transaction: Any, workspace_id: str) -> CreditAccount:
        account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
        if account is None:
            raise ValueError("credit account not found")
        return account

    def _read_entity(self, kind: str, entity_id: str, cls: type[T]) -> T | None:
        with self._database.snapshot() as snapshot:
            return self._read_entity_from(snapshot, kind, entity_id, cls)

    def _read_entity_tx(self, transaction: Any, kind: str, entity_id: str, cls: type[T]) -> T | None:
        return self._read_entity_from(transaction, kind, entity_id, cls)

    def _read_entity_from(self, reader: Any, kind: str, entity_id: str, cls: type[T]) -> T | None:
        rows = list(
            reader.execute_sql(
                "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
                params={"kind": kind, "id": entity_id},
                param_types={
                    "kind": self._param_types.STRING,
                    "id": self._param_types.STRING,
                },
            )
        )
        if not rows:
            return None
        data = json.loads(rows[0][0])
        if cls is dict:
            return data
        return cls(**data)

    def _list_entities(
        self,
        kind: str,
        *,
        cls: type[T],
        prefix: str | None = None,
        suffix: str | None = None,
    ) -> list[T]:
        where = "kind=@kind"
        params: dict[str, Any] = {"kind": kind}
        param_types: dict[str, Any] = {"kind": self._param_types.STRING}
        if prefix is not None:
            where += " AND STARTS_WITH(id, @prefix)"
            params["prefix"] = prefix
            param_types["prefix"] = self._param_types.STRING
        if suffix is not None:
            where += " AND ENDS_WITH(id, @suffix)"
            params["suffix"] = suffix
            param_types["suffix"] = self._param_types.STRING
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                f"SELECT body FROM tr_entities WHERE {where}",  # noqa: S608 - where is built from fixed predicates; values are bound params.
                params=params,
                param_types=param_types,
            )
            return [cls(**json.loads(row[0])) for row in rows]

    def _write_entity(self, kind: str, entity_id: str, value: Any) -> None:
        with self._database.batch() as batch:
            self._write_entity_batch(batch, kind, entity_id, value)

    def _write_entity_batch(self, batch: Any, kind: str, entity_id: str, value: Any) -> None:
        batch.insert_or_update(
            table=self.entity_table,
            columns=("kind", "id", "body", "updated_at"),
            values=[(kind, entity_id, _json_body(value), self._spanner.COMMIT_TIMESTAMP)],
        )

    def _write_entity_tx(self, transaction: Any, kind: str, entity_id: str, value: Any) -> None:
        transaction.insert_or_update(
            table=self.entity_table,
            columns=("kind", "id", "body", "updated_at"),
            values=[(kind, entity_id, _json_body(value), self._spanner.COMMIT_TIMESTAMP)],
        )

    def _delete_entities(self, kind: str, entity_ids: list[str]) -> None:
        with self._database.batch() as batch:
            batch.delete(
                self.entity_table,
                self._spanner.KeySet(keys=[(kind, entity_id) for entity_id in entity_ids]),
            )

    def _delete_entities_tx(self, transaction: Any, kind: str, entity_ids: list[str]) -> None:
        transaction.delete(
            self.entity_table,
            self._spanner.KeySet(keys=[(kind, entity_id) for entity_id in entity_ids]),
        )
