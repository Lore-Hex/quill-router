from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from typing import Any, TypeVar

from trusted_router.storage import (
    ApiKey,
    AuthSession,
    BroadcastDeliveryJob,
    BroadcastDestination,
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
    SyntheticProbeSample,
    SyntheticRollup,
    User,
    VerificationToken,
    WalletChallenge,
    Workspace,
    iso_now,
)
from trusted_router.storage_gcp_auth_sessions import SpannerAuthSessions
from trusted_router.storage_gcp_broadcast import SpannerBroadcastDestinations
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
from trusted_router.storage_gcp_synthetic_index import (
    synthetic_probe_samples as _bt_synthetic_probe_samples,
)
from trusted_router.storage_gcp_synthetic_index import (
    write_synthetic_probe_sample as _bt_write_synthetic_probe_sample,
)
from trusted_router.storage_gcp_synthetic_rollups import (
    synthetic_rollups as _bt_synthetic_rollups,
)
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
        bigtable_app_profile_id: str = "",
    ) -> None:
        if not spanner_instance_id or not spanner_database_id or not bigtable_instance_id:
            raise ValueError("Spanner and Bigtable IDs are required")
        try:
            from google.cloud import bigtable, spanner
            from google.cloud.spanner_v1 import FixedSizePool, param_types
        except ImportError as exc:  # pragma: no cover - exercised in prod image.
            raise RuntimeError(
                "Install google-cloud-spanner and google-cloud-bigtable for "
                "TR_STORAGE_BACKEND=spanner-bigtable"
            ) from exc

        # Cross-cloud credential bootstrap. On GCP (Cloud Run / GCE) the
        # default ADC chain finds the runtime SA automatically and
        # `credentials=None` is correct. On AWS ECS Fargate (Stage 4D
        # control plane), there's no metadata service the GCP SDK can
        # use, so we feed it a service-account key JSON via env. The
        # AWS task definition mounts the key from Secrets Manager into
        # `GCP_SERVICE_ACCOUNT_KEY_JSON`; we parse it once and pass to
        # both Spanner and Bigtable clients explicitly. Same SA the
        # Nitro enclave uses for cross-cloud Spanner reads.
        credentials = None
        sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_KEY_JSON", "").strip()
        if sa_json:
            try:
                from google.oauth2 import service_account
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "Install google-auth for cross-cloud SA-key auth"
                ) from exc
            try:
                info = json.loads(sa_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "GCP_SERVICE_ACCOUNT_KEY_JSON is set but not valid JSON"
                ) from exc
            credentials = service_account.Credentials.from_service_account_info(info)

        self._spanner = spanner
        self._param_types = param_types
        # Bounded session pool. The SDK default is FixedSizePool(size=10),
        # which preallocates ten gRPC sessions on first use — ~5-8 MB each
        # = 50-80 MB of resident memory per Cloud Run instance. Our
        # workload is single-shot reads/writes per HTTP request with
        # `--concurrency=2` (rollout.sh), so we'll never need more than
        # 2-3 sessions in flight; size=4 gives a 2x headroom over the
        # in-flight ceiling. Saves ~30 MB per instance.
        pool_size = int(
            os.environ.get("TR_SPANNER_POOL_SIZE", "4")
        )
        self._database = (
            spanner.Client(
                project=project_id,
                credentials=credentials,
                disable_builtin_metrics=True,
            )
            .instance(spanner_instance_id)
            .database(
                spanner_database_id,
                pool=FixedSizePool(size=pool_size),
            )
        )
        # Bigtable app-profile selection. Empty string = use the
        # instance's implicit default profile (current behavior; single-
        # cluster routing). Setting `tr-multi` (or whatever name we
        # give the multi-cluster-routing-use-any profile) lets reads/
        # writes go to the closest healthy cluster of three. Activates
        # once the 3rd BT cluster (us-east4-a) is provisioned and the
        # profile is created. See the multi-region expansion plan.
        bt_instance = bigtable.Client(
            project=project_id,
            credentials=credentials,
            admin=True,
        ).instance(bigtable_instance_id)
        if bigtable_app_profile_id:
            self._bt_table = bt_instance.table(
                generation_table, app_profile_id=bigtable_app_profile_id
            )
        else:
            self._bt_table = bt_instance.table(generation_table)
        self._bigtable_app_profile_id = bigtable_app_profile_id
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
        self.broadcast_store = SpannerBroadcastDestinations(io)
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
            # New accounts start at $0; trial credit is granted on first
            # valid-card attach via the Stripe webhook. See
            # routes/internal/webhook.py + the create_workspace doc above.
            credit = CreditAccount(
                workspace_id=workspace.id,
                total_credits_microdollars=0,
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

    def create_workspace(
        self,
        owner_user_id: str,
        name: str,
        *,
        trial_credit_microdollars: int | None = None,
    ) -> Workspace:
        workspace = Workspace(id=str(uuid.uuid4()), name=name, owner_user_id=owner_user_id)
        member = Member(workspace_id=workspace.id, user_id=owner_user_id, role="owner")
        # Trial credit is NOT granted at workspace-creation time anymore.
        # Policy moved to: grant the trial credit only after a valid
        # credit card is attached (via the Stripe setup_intent.succeeded
        # webhook in routes/internal/webhook.py). Stops free-credit
        # farming with throwaway emails. Wallet sign-in already passed
        # 0 explicitly, so its behavior is unchanged.
        credit = CreditAccount(
            workspace_id=workspace.id,
            total_credits_microdollars=(
                0 if trial_credit_microdollars is None else trial_credit_microdollars
            ),
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
                total_credits_microdollars=0,
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

    def get_key_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None:
        return self.api_keys.get_by_lookup_hash(lookup_hash)

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
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
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
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_fingerprint,
        )

    def get_gateway_authorization(
        self, authorization_id: str
    ) -> GatewayAuthorization | None:
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

    def record_synthetic_probe_sample(self, sample: SyntheticProbeSample) -> None:
        _bt_write_synthetic_probe_sample(self._bt_table, self.generation_family, sample)

    def synthetic_probe_samples(
        self,
        *,
        date: str | None = None,
        target: str | None = None,
        probe_type: str | None = None,
        monitor_region: str | None = None,
        limit: int = 1000,
    ) -> list[SyntheticProbeSample]:
        return _bt_synthetic_probe_samples(
            self._bt_table,
            self.generation_family,
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
        return _bt_synthetic_rollups(
            self._bt_table,
            self.generation_family,
            period=period,
            since=since,
            until=until,
            include_histograms=include_histograms,
            limit=limit,
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
        limit: int | None = None,
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
        suffix_sql = " ORDER BY id"
        if limit is not None:
            suffix_sql += " LIMIT @limit"
            params["limit"] = int(limit)
            param_types["limit"] = self._param_types.INT64
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                f"SELECT body FROM tr_entities WHERE {where}{suffix_sql}",  # noqa: S608 - where/suffix are built from fixed predicates; values are bound params.
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
