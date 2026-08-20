from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, TypeVar, cast

from trusted_router import phone_verification
from trusted_router import storage_gcp_credit_transfer as spanner_credit_transfer
from trusted_router.custom_model_billing import (
    user_model_authorization_id_from_payout_event_id,
)
from trusted_router.money import DEFAULT_SIGNUP_CREDIT_MICRODOLLARS
from trusted_router.operational_analytics import (
    OperationalAnalyticsClient,
    stable_rows_fingerprint,
)
from trusted_router.operational_analytics_freshness import (
    BACKEND_SPANNER,
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
    OutboxFreshness,
)
from trusted_router.security import lookup_hash_api_key, verify_api_key
from trusted_router.storage import (
    AcquisitionAttribution,
    ActivationReminderTask,
    ApiKey,
    ApiKeyUsageSnapshot,
    AuthSession,
    BroadcastDeliveryJob,
    BroadcastDestination,
    ByokProviderConfig,
    CreditAccount,
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
    SignupResult,
    SyntheticProbeSample,
    SyntheticRollup,
    User,
    UserProvidedModel,
    VerificationToken,
    VideoJob,
    WalletChallenge,
    Workspace,
    iso_now,
    normalize_provider_access_role,
    normalize_provider_access_slug,
)
from trusted_router.storage_activity import (
    ActivityResult,
    filter_generations,
    generation_events,
    generation_metrics,
    summarize_activity_result,
    usage_bucket_key,
)
from trusted_router.storage_auth_context import build_session_auth_context
from trusted_router.storage_errors import is_duplicate_key_error
from trusted_router.storage_gcp_analytics_outbox import SpannerAnalyticsOutbox
from trusted_router.storage_gcp_attribution import SpannerAcquisitionAttribution
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
from trusted_router.storage_gcp_counter_dml import (
    credit_credit_shard,
    debit_workspace_credit,
    insert_entity_dml_at,
)
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_COLUMNS,
    CREDIT_BALANCE_TABLE,
    UNSHARDED,
    credit_balance_mirror_row,
    credit_shard_count,
    distribute_credit_amount,
    key_usage_shard_count,
)
from trusted_router.storage_gcp_credit_shards import (
    CreditShardConfigurationMissingError,
    CreditShardCountCache,
    randomized_credit_shards,
)
from trusted_router.storage_gcp_custom_models import SpannerCustomModels
from trusted_router.storage_gcp_email_blocks import SpannerEmailBlocks
from trusted_router.storage_gcp_generations import SpannerGenerations
from trusted_router.storage_gcp_group_buy import SpannerBedrockGroupBuy
from trusted_router.storage_gcp_io import (
    SpannerIO,
    configure_spanner_rpc_deadlines,
    run_in_transaction_with_retry,
)
from trusted_router.storage_gcp_keys import SpannerApiKeys
from trusted_router.storage_gcp_oauth_codes import SpannerOAuthCodes
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
    analytics_surrogate,
)
from trusted_router.storage_gcp_rate_limits import SpannerRateLimits
from trusted_router.storage_gcp_request_records import read_gateway_authorization
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_gcp_synthetic_index import (
    synthetic_probe_samples as _bt_synthetic_probe_samples,
)
from trusted_router.storage_gcp_synthetic_index import (
    write_synthetic_probe_sample as _bt_write_synthetic_probe_sample,
)
from trusted_router.storage_gcp_synthetic_rollups import (
    synthetic_rollups as _bt_synthetic_rollups,
)
from trusted_router.storage_gcp_user_models import SpannerUserProvidedModels
from trusted_router.storage_gcp_verification_tokens import SpannerVerificationTokens
from trusted_router.storage_gcp_video_jobs import SpannerVideoJobs
from trusted_router.storage_gcp_wallet_challenges import SpannerWalletChallenges
from trusted_router.storage_models import (
    ApiKeyAuthContext,
    BedrockGroupBuyAggregate,
    BedrockGroupBuyPledge,
    BedrockGroupBuyPublicMessage,
    CreditMovement,
    SessionAuthContext,
    TypedFinalizeResult,
    UserModelPayout,
    _is_expired,
)
from trusted_router.types import IdentityVerificationStatus, UsageType

T = TypeVar("T")
log = logging.getLogger(__name__)

#: Whole-call budget for the /status.json outbox-lag read. Same 3s as
#: `readiness_check`, and the same rule: a public page degrades rather than
#: waits. See `SpannerBigtableStore.operational_analytics_outbox_freshness`.
OUTBOX_FRESHNESS_TIMEOUT_SECONDS = 3.0

_SESSION_AUTH_CONTEXT_SQL = """
    /* auth_session_context */
    WITH resolved_session AS (
      SELECT
        session_record.body AS session_body,
        JSON_VALUE(session_record.body, '$.user_id') AS user_id
      FROM tr_entities AS lookup_record
      JOIN tr_entities AS session_record
        ON session_record.kind='auth_session'
       AND session_record.id=JSON_VALUE(lookup_record.body, '$.session_id')
      WHERE lookup_record.kind='auth_session_lookup'
        AND lookup_record.id=@lookup_hash
    )
    SELECT
      resolved.session_body,
      user_record.body,
      workspace_record.body,
      member_record.body
    FROM resolved_session AS resolved
    LEFT JOIN tr_entities AS user_record
      ON user_record.kind='user' AND user_record.id=resolved.user_id
    LEFT JOIN tr_entities AS member_record
      ON member_record.kind='member'
     AND JSON_VALUE(member_record.body, '$.user_id')=resolved.user_id
     AND member_record.id=CONCAT(JSON_VALUE(member_record.body, '$.workspace_id'), '#', resolved.user_id)
    LEFT JOIN tr_entities AS workspace_record
      ON workspace_record.kind='workspace'
     AND workspace_record.id=JSON_VALUE(member_record.body, '$.workspace_id')
    ORDER BY member_record.id
"""

_API_KEY_AUTH_CONTEXT_SQL = """
    /* api_key_auth_context */
    SELECT key_record.body, workspace_record.body
    FROM tr_entities AS lookup_record
    JOIN tr_entities AS key_record
      ON key_record.kind='api_key'
     AND key_record.id=JSON_VALUE(lookup_record.body, '$.key_id')
    LEFT JOIN tr_entities AS workspace_record
      ON workspace_record.kind='workspace'
     AND workspace_record.id=JSON_VALUE(key_record.body, '$.workspace_id')
    WHERE lookup_record.kind='api_key_lookup'
      AND lookup_record.id=@lookup_hash
"""


def _auth_record(raw: str, cls: type[T]) -> T:
    data = json.loads(raw)
    known = {field.name for field in dataclasses.fields(cast(Any, cls))}
    return cls(**{key: value for key, value in data.items() if key in known})


def _empty_usage_bucket(bucket: str) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cost_micro": 0,
        "byok_micro": 0,
    }


def _add_usage_metrics(bucket: dict[str, Any], metrics: dict[str, int]) -> None:
    for key, value in metrics.items():
        bucket[key] += value


def _parse_iso_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _iso_timestamp(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


class SpannerBigtableStore:
    """Production Spanner store with ClickHouse analytics.

    Spanner owns strongly consistent control-plane state: users, orgs, API
    keys, reservations, credit ledger state, BYOK metadata, and Stripe event
    idempotency. ClickHouse receives bounded analytics through durable Spanner
    outboxes. Bigtable can remain attached as a migration-only mirror.

    Sibling of `InMemoryStore` rather than subclass — both implement the
    `Store` Protocol. The intentional non-inheritance means a method
    that exists on InMemoryStore but is missing here is a static-typing
    error the moment it's called via `Store`, not a silent runtime
    fallback to in-process dict access in production.
    """

    entity_table = "tr_entities"
    # New Bigtable writes are separated by retention class. ``m`` is retained
    # as a read-only compatibility family until legacy history ages out.
    legacy_generation_family = "m"
    activity_family = "activity"
    benchmark_family = "benchmark"
    synthetic_family = "synthetic"
    synthetic_rollup_family = "rollup"
    generation_family = legacy_generation_family

    def __init__(
        self,
        *,
        project_id: str,
        spanner_instance_id: str,
        spanner_database_id: str,
        bigtable_instance_id: str | None = None,
        generation_table: str = "trustedrouter-generations",
        bigtable_app_profile_id: str = "",
        bigtable_enabled: bool = True,
        bigtable_writes_enabled: bool = True,
        generation_records_enabled: bool = False,
        request_record_write_mode: str = "legacy",
        analytics_outbox_enabled: bool = False,
        operational_analytics_outbox_enabled: bool = False,
        operational_analytics_clickhouse_url: str = "",
        operational_analytics_clickhouse_user: str = "tr_control_read",
        operational_analytics_clickhouse_password: str = "",
        operational_analytics_clickhouse_database: str = "tr",
        analytics_read_mode: str = "bigtable",
        analytics_dual_read_grace_seconds: int = 30,
    ) -> None:
        if not spanner_instance_id or not spanner_database_id:
            raise ValueError("Spanner instance and database IDs are required")
        if bigtable_enabled and not bigtable_instance_id:
            raise ValueError("Bigtable instance ID is required when Bigtable is enabled")
        if request_record_write_mode not in {"legacy", "typed"}:
            raise ValueError("request_record_write_mode must be 'legacy' or 'typed'")
        if analytics_read_mode not in {
            "bigtable",
            "dual",
            "clickhouse",
            "clickhouse-only",
        }:
            raise ValueError(
                "analytics_read_mode must be bigtable, dual, clickhouse, or clickhouse-only"
            )
        if not bigtable_enabled and analytics_read_mode != "clickhouse-only":
            raise ValueError("Bigtable-free storage requires clickhouse-only reads")
        self.request_record_write_mode = request_record_write_mode
        self._generation_records_enabled = generation_records_enabled
        self._bigtable_enabled = bigtable_enabled
        self._bigtable_writes_enabled = bigtable_writes_enabled and bigtable_enabled
        self._analytics_read_mode = analytics_read_mode
        self._analytics_dual_read_grace_seconds = max(
            0,
            int(analytics_dual_read_grace_seconds),
        )
        self._operational_analytics = (
            OperationalAnalyticsClient(
                base_url=operational_analytics_clickhouse_url,
                user=operational_analytics_clickhouse_user,
                password=operational_analytics_clickhouse_password,
                database=operational_analytics_clickhouse_database,
            )
            if operational_analytics_clickhouse_url and operational_analytics_clickhouse_password
            else None
        )
        self._analytics_parity_log_lock = threading.Lock()
        self._analytics_last_parity_log: dict[str, float] = {}
        try:
            from google.cloud import spanner
            from google.cloud.spanner_v1 import FixedSizePool, param_types
        except ImportError as exc:  # pragma: no cover - exercised in prod image.
            raise RuntimeError("Install google-cloud-spanner for persistent GCP storage") from exc

        # GCP credential bootstrap. On GCP (Cloud Run / GCE) the default ADC
        # chain finds the runtime SA automatically and `credentials=None` is
        # correct. Local tests or one-off admin jobs may still provide
        # `GCP_SERVICE_ACCOUNT_KEY_JSON`; we parse it once and pass it to both
        # Spanner and Bigtable clients explicitly.
        credentials = None
        sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_KEY_JSON", "").strip()
        if sa_json:
            try:
                from google.oauth2 import service_account
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("Install google-auth for SA-key auth") from exc
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
        pool_size = int(os.environ.get("TR_SPANNER_POOL_SIZE", "4"))
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
        configure_spanner_rpc_deadlines(self._database)
        # Bigtable app-profile selection. Empty string = use the
        # instance's implicit default profile (current behavior; single-
        # cluster routing). Setting `tr-multi` (or whatever name we
        # give the multi-cluster-routing-use-any profile) lets reads/
        # writes go to the closest healthy cluster of three. Activates
        # once the 3rd BT cluster (us-east4-a) is provisioned and the
        # profile is created. See the multi-region expansion plan.
        self._bt_table = None
        if bigtable_enabled:
            try:
                from google.cloud import bigtable
            except ImportError as exc:  # pragma: no cover - production image.
                raise RuntimeError(
                    "Install google-cloud-bigtable when Bigtable mirroring is enabled"
                ) from exc
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

        self._credit_shard_counts = CreditShardCountCache(
            ttl_seconds=float(os.environ.get("TR_CREDIT_SHARD_COUNT_CACHE_SECONDS", "60")),
            max_entries=int(os.environ.get("TR_CREDIT_SHARD_COUNT_CACHE_ENTRIES", "10000")),
        )
        self._rebalance_last_attempt: dict[str, float] = {}
        self._rebalance_last_attempt_lock = threading.Lock()
        io = SpannerIO(
            database=self._database,
            spanner_module=self._spanner,
            param_types=self._param_types,
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
        self.acquisition_store = SpannerAcquisitionAttribution(io)
        self.bedrock_group_buy_store = SpannerBedrockGroupBuy(io)
        self._operational_analytics_outbox = (
            SpannerOperationalAnalyticsOutbox(self._database, self._param_types)
            if operational_analytics_outbox_enabled
            else None
        )
        self.generation_store = SpannerGenerations(
            io,
            bt_table=self._bt_table,
            param_types=self._param_types,
            generation_records_enabled=generation_records_enabled,
            bigtable_writes_enabled=bigtable_writes_enabled,
            activity_family=self.activity_family,
            benchmark_family=self.benchmark_family,
            legacy_family=self.legacy_generation_family,
            add_usage_to_key=self.api_keys.add_usage,
            analytics_outbox=(
                SpannerAnalyticsOutbox(self._database, self._param_types)
                if analytics_outbox_enabled
                else None
            ),
            operational_analytics_outbox=self._operational_analytics_outbox,
        )
        self.byok_store = SpannerByok(io)
        self.custom_model_store = SpannerCustomModels(io)
        self.user_model_store = SpannerUserProvidedModels(io)
        self.broadcast_store = SpannerBroadcastDestinations(io)
        self.video_job_store = SpannerVideoJobs(io)
        # Durable settle outbox (docs/design/durable-settle-outbox.md). Native
        # table, so it takes the raw database + param_types like the counter DML
        # rather than the entity IO. Dormant until settle_outbox_enabled + the
        # later increments wire enqueue/drain/reaper-guard to it.
        self.settle_outbox = SpannerSettleOutbox(self._database, self._param_types)
        self.auth_session_store = SpannerAuthSessions(io)
        self.oauth_code_store = SpannerOAuthCodes(io)
        self.rate_limit_store = SpannerRateLimits(io)
        self.wallet_challenges = SpannerWalletChallenges(io)
        self.verification_tokens = SpannerVerificationTokens(io)
        self.email_blocks = SpannerEmailBlocks(io)

    def readiness_check(self) -> None:
        """Verify the strongly consistent billing store within a hard deadline."""

        # This is the billing-store health signal, not a display value: keep it
        # strong so readiness cannot be certified from an older database view.
        with self._database.snapshot() as snapshot:
            list(
                snapshot.execute_sql(
                    "SELECT 1 FROM tr_credit_balance LIMIT 1",
                    timeout=3.0,
                )
            )

    def reset(self) -> None:
        raise RuntimeError("refusing to reset production Spanner/Bigtable store")

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

    def ensure_user(
        self,
        user_id: str,
        email: str | None = None,
        *,
        trial_credit_microdollars: int | None = None,
    ) -> User:
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
            initial_total = 0 if trial_credit_microdollars is None else trial_credit_microdollars
            credit = CreditAccount(workspace_id=workspace.id)
            self._write_entity_tx(transaction, "user", new_user.id, new_user)
            self._write_entity_tx(
                transaction, "email_user", normalized_email, {"user_id": new_user.id}
            )
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            self._write_entity_tx(
                transaction, "member", _member_id(workspace.id, new_user.id), member
            )
            self._write_entity_tx(transaction, "credit", workspace.id, credit)
            self._seed_credit_balance_on_create(transaction, workspace.id, initial_total)
            return new_user

        return self._run_in_transaction(txn)

    def grant_provider_access(
        self,
        user_id: str,
        provider: str,
        *,
        role: str = "viewer",
    ) -> ProviderAccessGrant:
        normalized_provider = normalize_provider_access_slug(provider)
        grant = ProviderAccessGrant(
            user_id=user_id,
            provider=normalized_provider,
            role=normalize_provider_access_role(role),
        )
        if self.get_user(user_id) is None:
            raise ValueError("user does not exist")
        self._write_entity(
            "provider_access",
            f"{user_id}#{normalized_provider}",
            grant,
        )
        return grant

    def list_provider_access_for_user(self, user_id: str) -> list[ProviderAccessGrant]:
        return self._list_entities(
            "provider_access",
            cls=ProviderAccessGrant,
            prefix=f"{user_id}#",
        )

    def revoke_provider_access(self, user_id: str, provider: str) -> bool:
        normalized_provider = normalize_provider_access_slug(provider)
        entity_id = f"{user_id}#{normalized_provider}"
        if self._read_entity("provider_access", entity_id, ProviderAccessGrant) is None:
            return False
        self._delete_entities("provider_access", [entity_id])
        return True

    def signup(
        self,
        *,
        email: str,
        workspace_name: str | None = None,
        trial_credit_microdollars: int = DEFAULT_SIGNUP_CREDIT_MICRODOLLARS,
    ) -> SignupResult | None:
        if self.find_user_by_email(email) is not None:
            return None
        user = self.ensure_user(
            email,
            email=email,
            trial_credit_microdollars=trial_credit_microdollars,
        )
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
        from trusted_router.typed_balance import live_credit_summary

        summary = live_credit_summary(workspace.id, store=self)
        return SignupResult(
            user=user,
            workspace=workspace,
            raw_key=raw_key,
            api_key=api_key,
            trial_credit_microdollars=summary["total_credits"] if summary else 0,
        )

    def _seed_credit_balance_on_create(
        self,
        writer: Any,
        workspace_id: str,
        initial_total_micro: int,
    ) -> None:
        writer.insert_or_update(
            table=CREDIT_BALANCE_TABLE,
            columns=CREDIT_BALANCE_COLUMNS,
            values=[
                credit_balance_mirror_row(
                    workspace_id,
                    initial_total_micro,
                    self._spanner.COMMIT_TIMESTAMP,
                )
            ],
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

    def session_auth_context(
        self,
        raw_token: str,
        *,
        requested_workspace_id: str | None = None,
    ) -> SessionAuthContext | None:
        """Resolve session, user, workspaces, and selected role in one RPC.

        This is deliberately a strong read: session invalidation and membership
        removal are authorization changes and must be visible on the next
        request.  A cache or bounded-staleness read would extend access.
        """
        lookup_hash = lookup_hash_api_key(raw_token)
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    _SESSION_AUTH_CONTEXT_SQL,
                    params={"lookup_hash": lookup_hash},
                    param_types={"lookup_hash": self._param_types.STRING},
                )
            )
        if not rows:
            return None

        session = _auth_record(str(rows[0][0]), AuthSession)
        if _is_expired(session.expires_at):
            # Preserve get_auth_session_by_raw's expired-record cleanup.  The
            # valid-request path above remains exactly one read RPC.
            with self._database.batch() as batch:
                batch.delete(
                    self.entity_table,
                    self._spanner.KeySet(
                        keys=[
                            ("auth_session", session.hash),
                            ("auth_session_lookup", lookup_hash),
                        ]
                    ),
                )
            return None
        if not verify_api_key(raw_token, session.salt, session.secret_hash):
            return None

        user = _auth_record(str(rows[0][1]), User) if rows[0][1] is not None else None
        memberships: list[tuple[Member, Workspace]] = []
        for _session_body, _user_body, workspace_body, member_body in rows:
            if workspace_body is None or member_body is None:
                continue
            member = _auth_record(str(member_body), Member)
            workspace = _auth_record(str(workspace_body), Workspace)
            memberships.append((member, workspace))
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
        workspace = Workspace(id=str(uuid.uuid4()), name=name, owner_user_id=owner_user_id)
        member = Member(workspace_id=workspace.id, user_id=owner_user_id, role="owner")
        # Account creation passes the configured starter amount explicitly.
        # Secondary workspaces omit it and therefore start at zero.
        initial_total = 0 if trial_credit_microdollars is None else trial_credit_microdollars
        credit = CreditAccount(workspace_id=workspace.id)
        with self._database.batch() as batch:
            self._write_entity_batch(batch, "workspace", workspace.id, workspace)
            self._write_entity_batch(
                batch, "member", _member_id(workspace.id, owner_user_id), member
            )
            self._write_entity_batch(batch, "credit", workspace.id, credit)
            self._seed_credit_balance_on_create(batch, workspace.id, initial_total)
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
        billing_paused: bool | None = None,
        billing_pause_reason: str | None = None,
    ) -> Workspace | None:
        def txn(transaction: Any) -> Workspace | None:
            workspace = self._read_entity_tx(transaction, "workspace", workspace_id, Workspace)
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
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            return None if workspace.deleted else workspace

        return self._run_in_transaction(txn)

    def get_credit_account(self, workspace_id: str) -> CreditAccount | None:
        return self._read_entity("credit", workspace_id, CreditAccount)

    def _credit_shard_count_loader(self, workspace_id: str) -> Callable[[], int]:
        def load() -> int:
            account = self.get_credit_account(workspace_id)
            if account is None:
                # The typed balance cannot be administered safely without its
                # control-plane account/config row. Treat this as drift, not as
                # an implicit one-shard account.
                raise CreditShardConfigurationMissingError(
                    "credit account not found while selecting shard"
                )
            return credit_shard_count(account)

        return load

    def _credit_shard_count(self, workspace_id: str) -> int:
        return self._credit_shard_counts.get(
            workspace_id,
            self._credit_shard_count_loader(workspace_id),
        )

    def _credit_shard_candidates(self, workspace_id: str) -> tuple[int, ...]:
        return randomized_credit_shards(self._credit_shard_count(workspace_id))

    def _refresh_credit_shard_candidates(self, workspace_id: str) -> tuple[int, ...]:
        count = self._credit_shard_counts.refresh(
            workspace_id,
            self._credit_shard_count_loader(workspace_id),
        )
        return randomized_credit_shards(count)

    def _credit_rebalance_cooldown_allows(self, workspace_id: str) -> bool:
        from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

        if not hasattr(self, "_rebalance_last_attempt"):
            self._rebalance_last_attempt = {}
        if not hasattr(self, "_rebalance_last_attempt_lock"):
            self._rebalance_last_attempt_lock = threading.Lock()
        attempts = self._rebalance_last_attempt
        now = time.monotonic()
        with self._rebalance_last_attempt_lock:
            last = attempts.get(workspace_id, float("-inf"))
            if now - last < rebalance_mod.REBALANCE_COOLDOWN_SECONDS:
                return False
            attempts[workspace_id] = now
            if len(attempts) > 10_000:
                cutoff = now - 60.0
                stale = [
                    stale_workspace
                    for stale_workspace, attempted_at in attempts.items()
                    if attempted_at < cutoff
                ]
                for stale_workspace in stale:
                    attempts.pop(stale_workspace, None)
            return True

    def add_members(
        self, workspace_id: str, emails: list[str], role: str = "member"
    ) -> list[Member]:
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
            credit = CreditAccount(workspace_id=workspace.id)
            self._write_entity_tx(transaction, "user", new_user.id, new_user)
            self._write_entity_tx(transaction, "wallet_user", normalized, {"user_id": new_user.id})
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            self._write_entity_tx(
                transaction, "member", _member_id(workspace.id, new_user.id), member
            )
            self._write_entity_tx(transaction, "credit", workspace.id, credit)
            self._seed_credit_balance_on_create(
                transaction,
                workspace.id,
                0,
            )
            return new_user

        return self._run_in_transaction(txn)

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

        return self._run_in_transaction(txn)

    def mark_user_email_verified(self, user_id: str) -> User | None:
        def txn(transaction: Any) -> User | None:
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None:
                return None
            user.email_verified = True
            self._write_entity_tx(transaction, "user", user.id, user)
            return user

        return self._run_in_transaction(txn)

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
        def txn(transaction: Any) -> User | None:
            user = self._read_entity_tx(transaction, "user", user_id, User)
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
            self._write_entity_tx(transaction, "user", user.id, user)
            return user

        return self._run_in_transaction(txn)

    def begin_phone_verification(
        self, user_id: str, phone: str, channel: str | None = None
    ) -> tuple[str, User] | None:
        # The code is generated inside the transaction and returned to the
        # caller to send; only its hash is persisted.
        holder: dict[str, str] = {}

        def txn(transaction: Any) -> User | None:
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None:
                return None
            holder["code"] = phone_verification.begin(user, phone, channel=channel)
            self._write_entity_tx(transaction, "user", user.id, user)
            return user

        user = self._run_in_transaction(txn)
        if user is None:
            return None
        return holder["code"], user

    def confirm_phone_verification(self, user_id: str, code: str) -> tuple[str, User | None]:
        holder: dict[str, str] = {}

        def txn(transaction: Any) -> User | None:
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None:
                return None
            # The attempt counter must be written whether or not the code
            # matched, or a failed guess would be free and the cap meaningless.
            holder["status"] = phone_verification.confirm(user, code).status
            self._write_entity_tx(transaction, "user", user.id, user)
            return user

        user = self._run_in_transaction(txn)
        return holder.get("status", "no_pending"), user

    def cancel_phone_verification(self, user_id: str) -> User | None:
        def txn(transaction: Any) -> User | None:
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None:
                return None
            phone_verification.cancel_pending(user)
            self._write_entity_tx(transaction, "user", user.id, user)
            return user

        return self._run_in_transaction(txn)

    def clear_user_phone(self, user_id: str) -> User | None:
        def txn(transaction: Any) -> User | None:
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None:
                return None
            phone_verification.clear(user)
            self._write_entity_tx(transaction, "user", user.id, user)
            return user

        return self._run_in_transaction(txn)

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
        limit_daily_microdollars: int | None = None,
        limit_weekly_microdollars: int | None = None,
        limit_monthly_microdollars: int | None = None,
        budget_alert_only: bool = False,
        tags: dict[str, str] | None = None,
    ) -> tuple[str, ApiKey]:
        # Keep every new key at the workspace's established write scale.
        # Lifetime limits use escrowed per-shard sub-budgets, so retaining an
        # exact cap no longer requires recreating a single hot key-limit row.
        usage_shard_count = self._credit_shard_count(workspace_id)
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
            usage_shard_count=usage_shard_count,
        )

    def get_key_by_hash(self, key_hash: str) -> ApiKey | None:
        return self.api_keys.get_by_hash(key_hash)

    def upsert_federated_api_key(self, record: dict[str, Any]) -> ApiKey:
        """Persist a key record resolved from the home plane.

        Identity only: no salt/secret_hash (a peer never holds home-issued
        key material) and NO credits — a federated key seeds at ZERO local
        balance, because copying a balance mints money. Spending on this
        plane requires an explicit transfer.
        """
        # Spanner is the HOME plane in this topology: it ISSUES keys, it does
        # not learn them from a peer. Federation-in is gated by
        # federation_home_base_url, which is empty on GCP, so this is
        # unreachable there. Raising beats a plausible-looking write that
        # silently creates a key with no secret material in the directory
        # of record.
        _ = record
        raise NotImplementedError(
            "GCP is the federation home plane; it does not import federated keys"
        )

    # --- Cross-plane credit transfer ---------------------------------------
    #
    # See trusted_router.storage_gcp_credit_transfer for the implementation and
    # the two ways it necessarily differs from the Postgres one: the escrow
    # debit is a conditional PLAN over the sharded balance (Spanner has no
    # single authoritative row to decrement), and the refund's serialization is
    # re-derived from the insert-once row plus Spanner's read-set validation
    # rather than copied from `SELECT ... FOR UPDATE`, which Spanner does not
    # have. `tests/conformance/test_store_semantics.py` holds this store to the
    # SAME assertions as Postgres via the `spanner-fake` backend.

    def open_credit_transfer(
        self,
        *,
        transfer_id: str,
        workspace_id: str,
        amount_microdollars: int,
        destination: str,
    ) -> CreditTransfer:
        return spanner_credit_transfer.open_credit_transfer(
            database=self._database,
            param_types=self._param_types,
            read_entity_tx=self._read_entity_tx,
            transfer_id=transfer_id,
            workspace_id=workspace_id,
            amount_microdollars=amount_microdollars,
            destination=destination,
        )

    def get_credit_transfer(self, transfer_id: str) -> CreditTransfer | None:
        return spanner_credit_transfer.get_credit_transfer(
            read_entity=self._read_entity, transfer_id=transfer_id
        )

    def list_open_credit_transfers(
        self, limit: int = 100, *, after_id: str = ""
    ) -> list[CreditTransfer]:
        return spanner_credit_transfer.list_open_credit_transfers(
            database=self._database,
            param_types=self._param_types,
            read_entity_tx=self._read_entity_tx,
            limit=limit,
            after_id=after_id,
        )

    def resolve_credit_transfer(self, *, transfer_id: str, outcome: str) -> CreditTransfer:
        return spanner_credit_transfer.resolve_credit_transfer(
            database=self._database,
            param_types=self._param_types,
            read_entity_tx=self._read_entity_tx,
            transfer_id=transfer_id,
            outcome=outcome,
        )

    def claim_credit_transfer(
        self,
        *,
        transfer_id: str,
        workspace_id: str,
        amount_microdollars: int,
        source: str,
        accept: bool,
    ) -> str:
        return spanner_credit_transfer.claim_credit_transfer(
            database=self._database,
            param_types=self._param_types,
            read_entity_tx=self._read_entity_tx,
            transfer_id=transfer_id,
            workspace_id=workspace_id,
            amount_microdollars=amount_microdollars,
            source=source,
            accept=accept,
        )

    def apply_federated_usage(
        self,
        *,
        source_plane: str,
        authorization_id: str,
        workspace_id: str,
        cost_microdollars: int,
        daily_cap_microdollars: int,
    ) -> str:
        from trusted_router import storage_gcp_federated_settlement

        return storage_gcp_federated_settlement.apply_federated_usage(
            self._database,
            self._param_types,
            source_plane=source_plane,
            authorization_id=authorization_id,
            workspace_id=workspace_id,
            cost_microdollars=cost_microdollars,
            daily_cap_microdollars=daily_cap_microdollars,
        )

    def get_key_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None:
        return self.api_keys.get_by_lookup_hash(lookup_hash)

    def get_key_by_raw(self, raw_key: str) -> ApiKey | None:
        return self.api_keys.get_by_raw(raw_key)

    def api_key_auth_context(self, raw_key: str) -> ApiKeyAuthContext | None:
        """Resolve and verify an API key with its workspace in one strong RPC."""
        lookup_hash = lookup_hash_api_key(raw_key)
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    _API_KEY_AUTH_CONTEXT_SQL,
                    params={"lookup_hash": lookup_hash},
                    param_types={"lookup_hash": self._param_types.STRING},
                )
            )
        if not rows:
            return None
        api_key = _auth_record(str(rows[0][0]), ApiKey)
        if not verify_api_key(raw_key, api_key.salt, api_key.secret_hash):
            return None
        workspace = (
            _auth_record(str(rows[0][1]), Workspace)
            if rows[0][1] is not None
            else None
        )
        if workspace is not None and workspace.deleted:
            workspace = None
        return ApiKeyAuthContext(api_key=api_key, workspace=workspace)

    def list_keys(self, workspace_id: str) -> list[ApiKey]:
        return self.api_keys.list_for_workspace(workspace_id)

    def list_api_keys_with_usage(self, workspace_id: str) -> list[ApiKeyUsageSnapshot]:
        return self.api_keys.list_with_usage_for_workspace(workspace_id)

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

    def credit_workspace_typed_direct(
        self,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        lifetime_topup_user_id: str | None = None,
    ) -> bool:
        def txn(transaction: Any) -> bool:
            if self._read_entity_tx(transaction, "stripe_event", event_id, dict) is not None:
                return False
            amount = int(amount_microdollars)
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
            if account is None:
                raise ValueError("credit account not found")
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            created_at = now.isoformat().replace("+00:00", "Z")
            self._credit_workspace_balance_tx(
                transaction,
                workspace_id,
                amount,
                now=now,
                account=account,
            )
            if lifetime_topup_user_id is not None:
                self._increment_lifetime_topup_tx(
                    transaction,
                    lifetime_topup_user_id,
                    amount,
                    now=now,
                )

            insert_entity_dml_at(
                transaction,
                self._param_types,
                "stripe_event",
                event_id,
                _json_body({"created_at": created_at}),
                now,
            )
            return True

        return self._run_in_transaction(txn)

    def credit_workspace_once(
        self, workspace_id: str, amount_microdollars: int, event_id: str
    ) -> bool:
        return self.credit_workspace_typed_direct(workspace_id, amount_microdollars, event_id)

    # Earnings & movement primitives -----------------------------------------

    @staticmethod
    def _positive_money_amount(amount_microdollars: int) -> int:
        amount = int(amount_microdollars)
        if amount <= 0:
            raise ValueError("amount_must_be_positive")
        return amount

    def _credit_workspace_balance_tx(
        self,
        transaction: Any,
        workspace_id: str,
        amount_microdollars: int,
        *,
        now: dt.datetime,
        account: CreditAccount | None = None,
    ) -> None:
        if account is None:
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
        if account is None:
            raise ValueError("credit_account_not_found")
        deltas = distribute_credit_amount(
            int(amount_microdollars),
            credit_shard_count(account),
        )
        for shard, delta in enumerate(deltas):
            updated = credit_credit_shard(
                transaction,
                self._param_types,
                workspace_id,
                delta,
                shard=shard,
                now=now,
            )
            if updated != 1:
                raise RuntimeError(
                    "missing authoritative tr_credit_balance shard "
                    f"{shard} for workspace {workspace_id}"
                )

    def _increment_lifetime_topup_tx(
        self,
        transaction: Any,
        user_id: str,
        amount_microdollars: int,
        *,
        now: dt.datetime,
    ) -> None:
        pt = self._param_types
        updated = transaction.execute_update(
            "UPDATE tr_user_lifetime_topup "
            "SET total_microdollars = total_microdollars + @amount, updated_at=@now "
            "WHERE user_id=@user_id",
            params={"amount": int(amount_microdollars), "now": now, "user_id": user_id},
            param_types={
                "amount": pt.INT64,
                "now": pt.TIMESTAMP,
                "user_id": pt.STRING,
            },
        )
        if updated == 0:
            transaction.execute_update(
                "INSERT INTO tr_user_lifetime_topup "
                "(user_id, total_microdollars, updated_at) "
                "VALUES (@user_id, @amount, @now)",
                params={"user_id": user_id, "amount": int(amount_microdollars), "now": now},
                param_types={
                    "user_id": pt.STRING,
                    "amount": pt.INT64,
                    "now": pt.TIMESTAMP,
                },
            )

    def _insert_credit_movement_tx(
        self,
        transaction: Any,
        *,
        account_id: str,
        movement_id: str,
        kind: str,
        amount_microdollars: int,
        counterparty_account_id: str | None = None,
        custom_model_id: str | None = None,
        authorization_id: str | None = None,
        created_at: dt.datetime,
    ) -> None:
        pt = self._param_types
        transaction.execute_update(
            "INSERT INTO tr_credit_movement "
            "(account_id, movement_id, kind, amount_microdollars, "
            "counterparty_account_id, custom_model_id, authorization_id, created_at) "
            "VALUES (@account_id, @movement_id, @kind, @amount, @counterparty, "
            "@custom_model_id, @authorization_id, @created_at)",
            params={
                "account_id": account_id,
                "movement_id": movement_id,
                "kind": kind,
                "amount": int(amount_microdollars),
                "counterparty": counterparty_account_id,
                "custom_model_id": custom_model_id,
                "authorization_id": authorization_id,
                "created_at": created_at,
            },
            param_types={
                "account_id": pt.STRING,
                "movement_id": pt.STRING,
                "kind": pt.STRING,
                "amount": pt.INT64,
                "counterparty": pt.STRING,
                "custom_model_id": pt.STRING,
                "authorization_id": pt.STRING,
                "created_at": pt.TIMESTAMP,
            },
        )

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

        def txn(transaction: Any) -> tuple[str, int]:
            if self._read_entity_tx(transaction, "stripe_event", event_id, dict) is not None:
                return "duplicate", 1
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
            shard_count = 1 if account is None else credit_shard_count(account)
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            if not debit_workspace_credit(
                transaction,
                self._param_types,
                workspace_id,
                amount,
                now=now,
            ):
                return "insufficient", shard_count
            self._insert_credit_movement_tx(
                transaction,
                account_id=workspace_id,
                movement_id=event_id,
                kind=kind,
                amount_microdollars=-amount,
                custom_model_id=custom_model_id,
                authorization_id=authorization_id,
                created_at=now,
            )
            insert_entity_dml_at(
                transaction,
                self._param_types,
                "stripe_event",
                event_id,
                _json_body({"created_at": _iso_timestamp(now), "workspace_id": workspace_id}),
                now,
            )
            return "accepted", shard_count

        status, shard_count = self._run_in_transaction(txn)
        if status == "insufficient" and shard_count > 1:
            from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

            rebalance_mod.rebalance_credit_for_estimate(
                self._database,
                self._param_types,
                workspace_id=workspace_id,
                shard_count=shard_count,
                target_shard=0,
                estimate=amount,
            )
            status, _ = self._run_in_transaction(txn)
        return status

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

        def txn(transaction: Any) -> bool:
            if self._read_entity_tx(transaction, "stripe_event", event_id, dict) is not None:
                return False
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            pt = self._param_types
            updated = transaction.execute_update(
                "UPDATE tr_earnings_balance "
                "SET total_earned = total_earned + @amount, updated_at=@now "
                "WHERE user_id=@user_id AND shard=0",
                params={"amount": amount, "now": now, "user_id": user_id},
                param_types={
                    "amount": pt.INT64,
                    "now": pt.TIMESTAMP,
                    "user_id": pt.STRING,
                },
            )
            if updated == 0:
                transaction.execute_update(
                    "INSERT INTO tr_earnings_balance "
                    "(user_id, shard, total_earned, total_transferred, updated_at) "
                    "VALUES (@user_id, 0, @amount, 0, @now)",
                    params={"user_id": user_id, "amount": amount, "now": now},
                    param_types={
                        "user_id": pt.STRING,
                        "amount": pt.INT64,
                        "now": pt.TIMESTAMP,
                    },
                )
            self._insert_credit_movement_tx(
                transaction,
                account_id=f"user:{user_id}",
                movement_id=event_id,
                kind="custom_model_payout",
                amount_microdollars=amount,
                counterparty_account_id=payer_workspace_id,
                custom_model_id=custom_model_id,
                authorization_id=(
                    user_model_authorization_id_from_payout_event_id(event_id)
                ),
                created_at=now,
            )
            insert_entity_dml_at(
                transaction,
                pt,
                "stripe_event",
                event_id,
                _json_body({"created_at": _iso_timestamp(now), "user_id": user_id}),
                now,
            )
            return True

        return self._run_in_transaction(txn)

    def transfer_earnings_to_workspace(
        self,
        user_id: str,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> str:
        amount = self._positive_money_amount(amount_microdollars)
        user_account_id = f"user:{user_id}"

        def txn(transaction: Any) -> str:
            if self._read_entity_tx(transaction, "stripe_event", event_id, dict) is not None:
                return "duplicate"
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            pt = self._param_types
            updated = transaction.execute_update(
                "UPDATE tr_earnings_balance "
                "SET total_transferred = total_transferred + @amount, updated_at=@now "
                "WHERE user_id=@user_id AND shard=0 "
                "AND (total_earned - total_transferred) >= @amount",
                params={"amount": amount, "now": now, "user_id": user_id},
                param_types={
                    "amount": pt.INT64,
                    "now": pt.TIMESTAMP,
                    "user_id": pt.STRING,
                },
            )
            if updated == 0:
                return "insufficient"
            self._credit_workspace_balance_tx(
                transaction,
                workspace_id,
                amount,
                now=now,
            )
            self._insert_credit_movement_tx(
                transaction,
                account_id=user_account_id,
                movement_id=event_id,
                kind="earnings_transfer_out",
                amount_microdollars=-amount,
                counterparty_account_id=workspace_id,
                created_at=now,
            )
            self._insert_credit_movement_tx(
                transaction,
                account_id=workspace_id,
                movement_id=event_id,
                kind="earnings_transfer_in",
                amount_microdollars=amount,
                counterparty_account_id=user_account_id,
                created_at=now,
            )
            insert_entity_dml_at(
                transaction,
                pt,
                "stripe_event",
                event_id,
                _json_body({"created_at": _iso_timestamp(now), "user_id": user_id}),
                now,
            )
            return "accepted"

        return self._run_in_transaction(txn)

    def ensure_earnings_account(self, user_id: str) -> None:
        def txn(transaction: Any) -> None:
            pt = self._param_types
            rows = list(
                transaction.execute_sql(
                    "SELECT user_id FROM tr_earnings_balance WHERE user_id=@user_id AND shard=0",
                    params={"user_id": user_id},
                    param_types={"user_id": pt.STRING},
                )
            )
            if rows:
                return
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            transaction.execute_update(
                "INSERT INTO tr_earnings_balance "
                "(user_id, shard, total_earned, total_transferred, updated_at) "
                "VALUES (@user_id, 0, 0, 0, @now)",
                params={"user_id": user_id, "now": now},
                param_types={"user_id": pt.STRING, "now": pt.TIMESTAMP},
            )

        self._run_in_transaction(txn)

    def earnings_summary(
        self,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, int]:
        pt = self._param_types
        # Display callers may accept five seconds of staleness. The default is
        # strong because transfer POSTs return the just-committed balance (or
        # current insufficient-funds detail) from this same method.
        snapshot_options: dict[str, Any] = {}
        if allow_stale:
            snapshot_options["exact_staleness"] = dt.timedelta(seconds=5)
        with self._database.snapshot(**snapshot_options) as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT total_earned, total_transferred "
                    "FROM tr_earnings_balance WHERE user_id=@user_id AND shard=0",
                    params={"user_id": user_id},
                    param_types={"user_id": pt.STRING},
                )
            )
        earned, transferred = (0, 0) if not rows else (int(rows[0][0]), int(rows[0][1]))
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
        if kinds == []:
            return []
        pt = self._param_types
        where = "account_id=@account_id"
        params: dict[str, Any] = {
            "account_id": account_id,
            "limit": max(0, int(limit)),
        }
        param_types: dict[str, Any] = {
            "account_id": pt.STRING,
            "limit": pt.INT64,
        }
        if kinds is not None:
            where += " AND kind IN UNNEST(@kinds)"
            params["kinds"] = kinds
            param_types["kinds"] = pt.Array(pt.STRING)
        if before is not None:
            where += " AND created_at < @before"
            params["before"] = _parse_iso_timestamp(before)
            param_types["before"] = pt.TIMESTAMP
        # This is paginated ledger history and never authorizes a transfer.
        # Thirty seconds is imperceptible for history while keeping the read
        # local to a nearby Spanner replica.
        with self._database.snapshot(exact_staleness=dt.timedelta(seconds=30)) as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT account_id, movement_id, kind, amount_microdollars, "  # noqa: S608 -- fixed clauses only.
                    "counterparty_account_id, custom_model_id, authorization_id, created_at "
                    "FROM tr_credit_movement@{FORCE_INDEX=tr_credit_movement_by_time} "
                    f"WHERE {where} ORDER BY created_at DESC, movement_id DESC "
                    "LIMIT @limit",
                    params=params,
                    param_types=param_types,
                )
            )
        return [
            CreditMovement(
                account_id=str(row[0]),
                movement_id=str(row[1]),
                kind=str(row[2]),
                amount_microdollars=int(row[3]),
                counterparty_account_id=None if row[4] is None else str(row[4]),
                custom_model_id=None if row[5] is None else str(row[5]),
                authorization_id=None if row[6] is None else str(row[6]),
                created_at=_iso_timestamp(row[7]),
            )
            for row in rows
        ]

    def custom_model_earnings_by_model(
        self,
        user_id: str,
        *,
        since: str,
    ) -> dict[str, int]:
        pt = self._param_types
        # A 30-day display aggregate does not gate payout or transfer logic.
        # One minute of staleness is small relative to its reporting window.
        with self._database.snapshot(exact_staleness=dt.timedelta(seconds=60)) as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT custom_model_id, SUM(amount_microdollars) "
                    "FROM tr_credit_movement@{FORCE_INDEX=tr_credit_movement_by_time} "
                    "WHERE account_id=@account_id AND kind='custom_model_payout' "
                    "AND custom_model_id IS NOT NULL AND created_at>=@since "
                    "GROUP BY custom_model_id",
                    params={
                        "account_id": f"user:{user_id}",
                        "since": _parse_iso_timestamp(since),
                    },
                    param_types={"account_id": pt.STRING, "since": pt.TIMESTAMP},
                )
            )
        return {str(row[0]): int(row[1]) for row in rows}

    def get_lifetime_topup_microdollars(
        self,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> int:
        pt = self._param_types
        # Display callers may accept five seconds of staleness. The default is
        # strong because verification gates and the lifetime-top-up backfill
        # read immediately before/after a durable increment.
        snapshot_options: dict[str, Any] = {}
        if allow_stale:
            snapshot_options["exact_staleness"] = dt.timedelta(seconds=5)
        with self._database.snapshot(**snapshot_options) as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT total_microdollars FROM tr_user_lifetime_topup WHERE user_id=@user_id",
                    params={"user_id": user_id},
                    param_types={"user_id": pt.STRING},
                )
            )
        return 0 if not rows else int(rows[0][0])

    def add_lifetime_topup(
        self,
        user_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> bool:
        amount = self._positive_money_amount(amount_microdollars)

        def txn(transaction: Any) -> bool:
            if self._read_entity_tx(transaction, "stripe_event", event_id, dict) is not None:
                return False
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            self._increment_lifetime_topup_tx(transaction, user_id, amount, now=now)
            insert_entity_dml_at(
                transaction,
                self._param_types,
                "stripe_event",
                event_id,
                _json_body(
                    {
                        "created_at": now.isoformat().replace("+00:00", "Z"),
                        "lifetime_topup_user_id": user_id,
                    }
                ),
                now,
            )
            return True

        return self._run_in_transaction(txn)

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

        return self._run_in_transaction(txn)

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

        return self._run_in_transaction(txn)

    def clear_stripe_payment_method(self, workspace_id: str) -> CreditAccount | None:
        def txn(transaction: Any) -> CreditAccount | None:
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
            if account is None:
                return None
            account.stripe_payment_method_id = None
            account.auto_refill_enabled = False
            account.last_auto_refill_at = iso_now()
            account.last_auto_refill_status = "disabled:payment_method_removed"
            self._write_entity_tx(transaction, "credit", workspace_id, account)
            return account

        return self._run_in_transaction(txn)

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

        return self._run_in_transaction(txn)

    def reserve(
        self,
        workspace_id: str,
        key_hash: str,
        amount_microdollars: int,
        *,
        idempotency_key: str | None = None,
    ) -> Reservation:
        raise RuntimeError(
            "legacy JSON reserve path removed (C1); direct inference requires the memory store"
        )

    def settle(self, reservation_id: str, actual_microdollars: int) -> None:
        raise RuntimeError(
            "legacy JSON settle path removed (C1); direct inference requires the memory store"
        )

    def refund(self, reservation_id: str) -> None:
        raise RuntimeError(
            "legacy JSON refund path removed (C1); direct inference requires the memory store"
        )

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
            custom_model_id=custom_model_id,
            custom_model_revision=custom_model_revision,
            user_provided_model_id=user_provided_model_id,
            user_provided_model_revision=user_provided_model_revision,
            user_model_prompt_price_microdollars_per_m=(
                user_model_prompt_price_microdollars_per_m
            ),
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
        # Settlement and durable-outbox recovery consume this state. A stale
        # authorization could replay terminal work, so this point read is strong.
        with self._database.snapshot() as snapshot:
            typed = read_gateway_authorization(
                snapshot,
                self._param_types,
                authorization_id,
            )
        if typed is not None:
            return typed
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
        raise RuntimeError("legacy JSON finalize path removed (C1); use typed finalize on Spanner")

    # ── Typed-column billing (Step 3): thin wrappers over storage_gcp_authorize.
    # The atomic conditional-DML authorize/settle engine. Routes select this by
    # typed-store capability; the legacy cohort/denylist brake is gone after C1.
    def authorize_gateway_atomic(self, **kwargs: Any) -> dict:
        from trusted_router.storage_gcp_authorize import authorize_atomic

        return authorize_atomic(self._database, self._param_types, **kwargs)

    def typed_finalize_gateway(self, **kwargs: Any) -> dict:
        from trusted_router.storage_gcp_authorize import typed_finalize_atomic

        kwargs.setdefault(
            "operational_analytics_outbox",
            getattr(self, "_operational_analytics_outbox", None),
        )
        kwargs.setdefault(
            "persist_generation_record",
            getattr(self, "_generation_records_enabled", False),
        )
        return typed_finalize_atomic(self._database, self._param_types, **kwargs)

    def typed_finalize_gateway_authorization(
        self,
        authorization_id: str,
        *,
        success: bool,
        actual_microdollars: int,
        selected_usage_type: UsageType | str,
        generation: Generation | None = None,
        user_model_payout: UserModelPayout | None = None,
    ) -> bool:
        return self.typed_finalize_gateway_authorization_result(
            authorization_id,
            success=success,
            actual_microdollars=actual_microdollars,
            selected_usage_type=selected_usage_type,
            generation=generation,
            user_model_payout=user_model_payout,
        ).finalized

    def typed_finalize_gateway_authorization_result(
        self,
        authorization_id: str,
        *,
        success: bool,
        actual_microdollars: int,
        selected_usage_type: UsageType | str,
        generation: Generation | None = None,
        user_model_payout: UserModelPayout | None = None,
    ) -> TypedFinalizeResult:
        """Route-facing typed settle: same contract as
        finalize_gateway_authorization, with explicit activity-index status.

        The billing transaction atomically commits the bounded generation row
        and ClickHouse delivery intent. A false ``activity_indexed`` leaves the
        durable settle outbox pending for a no-double-charge repair replay.
        """
        from trusted_router.storage_gcp_authorize import SettleOutcome, typed_finalize_atomic

        authorization = self.get_gateway_authorization(authorization_id)
        if authorization is None or authorization.credit_reservation_id is None:
            return TypedFinalizeResult(finalized=False, activity_indexed=False)
        actual_usage_type = UsageType.coerce(selected_usage_type)
        generation_writes: list[tuple[str, str, str]] = []
        if success and generation is not None:
            generation_writes = [
                ("generation", generation.id, _json_body(generation)),
                (
                    "generation_by_workspace",
                    _generation_workspace_id(generation),
                    _json_body({"generation_id": generation.id}),
                ),
            ]
        authorization.record_finalization(
            success=success,
            actual_microdollars=actual_microdollars,
            selected_usage_type=actual_usage_type,
            generation=generation,
        )
        spanner_start = time.perf_counter()
        result = typed_finalize_atomic(
            self._database,
            self._param_types,
            reservation_id=authorization.credit_reservation_id,
            authorization_id=authorization_id,
            success=success,
            actual_micro=actual_microdollars,
            settled_usage_type=str(actual_usage_type),
            now=dt.datetime.now(dt.UTC),
            authorization=authorization,
            auth_body_settled=_json_body(authorization),
            generation_writes=generation_writes,
            generation=generation,
            persist_generation_record=getattr(
                self,
                "_generation_records_enabled",
                False,
            ),
            operational_analytics_outbox=getattr(
                self,
                "_operational_analytics_outbox",
                None,
            ),
            user_model_payout=user_model_payout,
        )
        spanner_ms = (time.perf_counter() - spanner_start) * 1000
        if result["outcome"] == SettleOutcome.ERROR:
            raise RuntimeError("typed finalize failed: release row-count != 1")
        if result["outcome"] == SettleOutcome.SETTLED:
            mirror_ms = 0.0
            activity_indexed = bool(result.get("activity_durable", generation is None))
            if success and generation is not None:
                mirror_start = time.perf_counter()
                if getattr(self, "_operational_analytics_outbox", None) is None:
                    activity_indexed = self.generation_store.index_after_commit(generation)
                else:
                    self.generation_store.mirror_after_commit(generation)
                mirror_ms = (time.perf_counter() - mirror_start) * 1000
            # Splits the settle-path finalize_ms hotspot (2026-07-05 investigation)
            # into the authoritative Spanner transaction and best-effort
            # analytics mirrors. Mirror latency never changes settle success.
            # attempts counts only OUTER wrapper retries; Spanner's own internal
            # Aborted retries are invisible, so attempts>1 is definitive severe
            # contention while attempts==1 does not rule out absorbed contention.
            log.info(
                # Keep index_ms for log-query compatibility. It now measures
                # optional post-commit mirrors rather than durable delivery.
                "typed finalize timing authorization_id=%s spanner_ms=%.1f "
                "index_ms=%.1f attempts=%d",
                authorization_id,
                spanner_ms,
                mirror_ms,
                result.get("attempts", 1),
            )
            return TypedFinalizeResult(
                finalized=True,
                activity_indexed=activity_indexed,
                request_record_typed=bool(result.get("request_record_typed")),
            )
        return TypedFinalizeResult(
            finalized=False,
            activity_indexed=False,
        )  # already_settled / not_found

    def authorize_gateway_typed(
        self,
        *,
        workspace_id: str,
        key_hash: str,
        authorization_id: str | None = None,
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
        key_usage_shards: int = 1,
        tags: dict[str, str] | None = None,
        custom_model_id: str | None = None,
        custom_model_revision: int | None = None,
        user_provided_model_id: str | None = None,
        user_provided_model_revision: int | None = None,
        user_model_prompt_price_microdollars_per_m: int | None = None,
        user_model_completion_price_microdollars_per_m: int | None = None,
        user_model_owner_user_id: str | None = None,
        additional_cost_reservation_microdollars: int = 0,
        native_batch_eligible: bool = False,
        expires_at: Any = None,
        window_limits: dict[str, int] | None = None,
    ) -> tuple[str, GatewayAuthorization | None]:
        """Route-facing typed authorize. Runs the atomic conditional-DML authorize
        (holds + reservation + gateway_authorization DML-insert) and returns
        (outcome, authorization). outcome in accepted/replay/insufficient_credits/
        key_limit_exceeded/key_missing/idempotency_mismatch, or
        "key_window_limit_exceeded:<daily|weekly|monthly>" when a per-window cap
        blocked (see authorize_atomic's window_limits contract)."""
        from trusted_router.storage_gcp_authorize import AuthorizeOutcome, authorize_atomic
        from trusted_router.storage_gcp_keys import (
            _gateway_authorization_idempotency_index_id,
        )

        usage = UsageType.coerce(reservation_usage_type)
        key_counter_shards = key_usage_shard_count({"usage_shard_count": key_usage_shards})
        key_shard_candidates = randomized_credit_shards(key_counter_shards)
        scope = (
            _gateway_authorization_idempotency_index_id(workspace_id, key_hash, idempotency_key)
            if idempotency_key is not None
            else None
        )

        def build_authorization(
            authorization_id: str,
            reservation_id: str,
        ) -> GatewayAuthorization:
            return GatewayAuthorization(
                id=authorization_id,
                workspace_id=workspace_id,
                key_hash=key_hash,
                model_id=model_id,
                provider=provider,
                usage_type=usage,
                estimated_microdollars=estimate,
                credit_reservation_id=reservation_id,
                requested_model_id=requested_model_id,
                candidate_model_ids=list(candidate_model_ids or []),
                region=region,
                endpoint_id=endpoint_id,
                candidate_endpoint_ids=list(candidate_endpoint_ids or []),
                idempotency_key=idempotency_key,
                tags=dict(tags or {}),
                idempotency_fingerprint=idempotency_fingerprint,
                custom_model_id=custom_model_id,
                custom_model_revision=custom_model_revision,
                user_provided_model_id=user_provided_model_id,
                user_provided_model_revision=user_provided_model_revision,
                user_model_prompt_price_microdollars_per_m=(
                    user_model_prompt_price_microdollars_per_m
                ),
                user_model_completion_price_microdollars_per_m=(
                    user_model_completion_price_microdollars_per_m
                ),
                user_model_owner_user_id=user_model_owner_user_id,
                additional_cost_reservation_microdollars=additional_cost_reservation_microdollars,
                native_batch_eligible=native_batch_eligible,
            )

        def build_body(authorization_id: str, reservation_id: str) -> str:
            return _json_body(build_authorization(authorization_id, reservation_id))

        if window_limits:
            # Lock-free snapshot check BEFORE the DML-only transaction (keeps
            # the authorize txn free of shared reads on the hot row — the
            # deadlock shape the typed migration removed). Replay-safe: an
            # existing same-fingerprint reservation passes through to the txn.
            from trusted_router.storage_gcp_authorize import check_key_window_limits

            blocked = check_key_window_limits(
                self._database,
                self._param_types,
                key_hash=key_hash,
                estimate=estimate,
                window_limits=window_limits,
                shard_count=key_counter_shards,
                idempotency_scope=scope,
                idempotency_fingerprint=idempotency_fingerprint,
            )
            if blocked is not None:
                # WHICH window rides as an outcome suffix so the
                # (outcome, authorization) tuple shape stays unchanged; the
                # gateway route splits on ':'.
                return f"{AuthorizeOutcome.KEY_WINDOW_LIMIT_EXCEEDED}:{blocked}", None

        credit_shard_candidates = (
            self._credit_shard_candidates(workspace_id) if has_credit_candidate else (UNSHARDED,)
        )

        def run_authorize(candidates: tuple[int, ...]) -> dict[str, Any]:
            return authorize_atomic(
                self._database,
                self._param_types,
                workspace_id=workspace_id,
                key_hash=key_hash,
                estimate=estimate,
                has_credit_candidate=has_credit_candidate,
                reservation_usage_type=str(usage),
                idempotency_scope=scope,
                idempotency_fingerprint=idempotency_fingerprint,
                expires_at=expires_at,
                build_authorization=build_authorization,
                build_auth_body=build_body,
                request_record_write_mode=self.request_record_write_mode,
                credit_shard_candidates=candidates,
                key_shard_candidates=key_shard_candidates,
                authorization_id=authorization_id,
            )

        result = run_authorize(credit_shard_candidates)
        if (
            result["outcome"] == AuthorizeOutcome.KEY_LIMIT_EXCEEDED
            and key_counter_shards > 1
        ):
            from trusted_router.storage_gcp_key_escrow import (
                rebalance_key_limit_headroom,
            )

            if rebalance_key_limit_headroom(
                self._database,
                self._param_types,
                key_hash=key_hash,
                shard_count=key_counter_shards,
                estimate=estimate,
                preferred_shard=key_shard_candidates[0],
            ):
                result = run_authorize(credit_shard_candidates)
        if result["outcome"] == AuthorizeOutcome.INSUFFICIENT_CREDITS and has_credit_candidate:
            from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

            # An all-shards rejection is cold. Refresh once so a remote
            # pause/drain split or unshard cannot produce a false 402 until the
            # normal TTL expires. The accepted hot path never pays this read.
            previous_count = len(credit_shard_candidates)
            try:
                refreshed_candidates = self._refresh_credit_shard_candidates(workspace_id)
            except Exception:
                log.warning(
                    "credit shard-count refresh failed on reject path; "
                    "keeping cached count workspace=%s",
                    workspace_id,
                    exc_info=True,
                )
                refreshed_candidates = credit_shard_candidates
            if len(refreshed_candidates) != previous_count:
                credit_shard_candidates = refreshed_candidates
                result = run_authorize(credit_shard_candidates)

            def rebalance(candidates: tuple[int, ...]) -> dict[str, int | str]:
                return rebalance_mod.rebalance_credit_for_estimate(
                    self._database,
                    self._param_types,
                    workspace_id=workspace_id,
                    shard_count=len(candidates),
                    target_shard=candidates[0],
                    estimate=estimate,
                )

            # A concurrent request can consume the newly consolidated target
            # between rebalance commit and our retry. Retry this cold path a
            # small bounded number of times; true aggregate exhaustion exits
            # from the snapshot precheck before entering the RW repair.
            forced_reload_done = False
            cooldown_passed = False
            for _attempt in range(3):
                if (
                    result["outcome"] != AuthorizeOutcome.INSUFFICIENT_CREDITS
                    or len(credit_shard_candidates) <= 1
                ):
                    break
                verdict = rebalance_mod.rebalance_precheck(
                    self._database,
                    self._param_types,
                    workspace_id=workspace_id,
                    shard_count=len(credit_shard_candidates),
                    target_shard=credit_shard_candidates[0],
                    estimate=estimate,
                )
                if verdict == rebalance_mod.RebalanceOutcome.INSUFFICIENT:
                    break
                if verdict == rebalance_mod.RebalanceOutcome.NOT_NEEDED:
                    result = run_authorize(credit_shard_candidates)
                    continue
                if verdict == rebalance_mod.RebalanceOutcome.INCOMPLETE and not forced_reload_done:
                    # The observed shard set doesn't match the count we hold —
                    # most likely a remote unshard/split behind the refresh's
                    # dedupe window, not corruption. Force ONE dedupe-bypassing
                    # reload before treating it as fail-closed: a real count
                    # change re-runs authorize on the true shard set (a clean
                    # 402/accept), while an unchanged count falls through to
                    # the authoritative RW rebalance on the next iteration.
                    forced_reload_done = True
                    try:
                        self._credit_shard_counts.invalidate(workspace_id)
                        reloaded_candidates = self._credit_shard_candidates(workspace_id)
                    except Exception:
                        log.warning(
                            "credit shard-count forced reload failed after "
                            "incomplete precheck workspace=%s",
                            workspace_id,
                            exc_info=True,
                        )
                        break
                    if len(reloaded_candidates) != len(credit_shard_candidates):
                        credit_shard_candidates = reloaded_candidates
                        result = run_authorize(credit_shard_candidates)
                    continue
                # The cooldown gates only a request's FIRST repair. Later loop
                # iterations of the same request are the steal-race retries this
                # loop exists for (a concurrent request drained our consolidated
                # target between rebalance commit and re-authorize) and must not
                # be blocked by our own just-recorded timestamp.
                if not cooldown_passed:
                    if not self._credit_rebalance_cooldown_allows(workspace_id):
                        break
                    cooldown_passed = True
                rebalance_result = rebalance(credit_shard_candidates)
                log.info(
                    "credit rebalance workspace=%s outcome=%s moved_micro=%s "
                    "estimate=%s attempt=%d",
                    workspace_id,
                    rebalance_result["outcome"],
                    rebalance_result.get("moved_micro", 0),
                    estimate,
                    _attempt + 1,
                )
                if rebalance_result["outcome"] == rebalance_mod.RebalanceOutcome.INCOMPLETE:
                    raise RuntimeError(
                        "configured credit shard set is incomplete after cache refresh"
                    )
                if rebalance_result["outcome"] in {
                    rebalance_mod.RebalanceOutcome.MOVED,
                    rebalance_mod.RebalanceOutcome.NOT_NEEDED,
                }:
                    result = run_authorize(credit_shard_candidates)
                    continue
                break
        outcome = result["outcome"]
        authorization: GatewayAuthorization | None = None
        if outcome in (AuthorizeOutcome.ACCEPTED, AuthorizeOutcome.REPLAY):
            authorization = self.get_gateway_authorization(result["authorization_id"])
        return outcome, authorization

    def reap_expired_reservations(self, *, now: Any, limit: int = 100) -> int:
        from trusted_router.storage_gcp_authorize import (
            reap_expired_reservations as _reap,
        )

        return _reap(self._database, self._param_types, now=now, limit=limit)

    def typed_key_usage(
        self,
        key_hash: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, Any] | None:
        """One point-read of the typed tr_key_limit row: live lifetime counters
        (post-flip the JSON api_key copies are frozen/stale) + the lazy window
        usage (stale windows read as zero). None when the row is missing —
        callers fall back to the JSON values."""
        from trusted_router.spend_windows import utcnow, window_floors

        pt = self._param_types
        # Display callers opt into five-second staleness. The default remains
        # strong because the same method decides whether to emit a one-shot
        # budget alert; a stale below-threshold read could suppress that email.
        snapshot_options: dict[str, Any] = {"multi_use": True}
        if allow_stale:
            snapshot_options["exact_staleness"] = dt.timedelta(seconds=5)
        with self._database.snapshot(**snapshot_options) as snapshot:
            key = self._read_entity_from(snapshot, "api_key", key_hash, ApiKey)
            if key is None:
                return None
            shard_count = key_usage_shard_count(key)
            rows = list(
                snapshot.execute_sql(
                    "SELECT shard, usage, byok_usage, reserved, day_usage, day_start, "
                    "week_usage, week_start, month_usage, month_start "
                    "FROM tr_key_limit WHERE key_hash=@pk AND shard>=0 "
                    "AND shard<@shard_count ORDER BY shard",
                    params={"pk": key_hash, "shard_count": shard_count},
                    param_types={"pk": pt.STRING, "shard_count": pt.INT64},
                )
            )
        if not rows:
            return None
        if [int(row[0]) for row in rows] != list(range(shard_count)):
            raise RuntimeError("configured tr_key_limit usage shard set is incomplete")
        floors = window_floors(utcnow())
        usage = sum(int(row[1]) for row in rows)
        byok = sum(int(row[2]) for row in rows)
        reserved = sum(int(row[3]) for row in rows)

        def current_window_usage(usage_index: int, start_index: int, window: str) -> int:
            return sum(
                int(row[usage_index] or 0)
                for row in rows
                if row[start_index] is not None and row[start_index] >= floors[window]
            )

        return {
            "usage": usage,
            "byok_usage": byok,
            "reserved": reserved,
            "windows": {
                "daily": current_window_usage(4, 5, "daily"),
                "weekly": current_window_usage(6, 7, "weekly"),
                "monthly": current_window_usage(8, 9, "monthly"),
            },
        }

    def typed_credit_snapshot(self, workspace_id: str) -> tuple[int, int, int] | None:
        """Sum the workspace's active authoritative credit sub-ledgers.

        Returns (total_credits, total_usage, reserved) microdollars. A workspace
        without a metadata account returns None. Any missing active typed shard
        is ledger corruption and fails closed; JSON money is never consulted.
        """
        pt = self._param_types
        # This exact snapshot also feeds auto-refill decisions. Do not reuse the
        # allow-stale authorize cache here: after a split, a stale smaller count
        # could understate available credit and charge a card prematurely.
        with self._database.snapshot(multi_use=True) as snapshot:
            account = self._read_entity_from(snapshot, "credit", workspace_id, CreditAccount)
            if account is None:
                return None
            shard_count = credit_shard_count(account)
            rows = list(
                snapshot.execute_sql(
                    "SELECT shard, total_credits, total_usage, reserved FROM tr_credit_balance "
                    "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count ORDER BY shard",
                    params={"pk": workspace_id, "shard_count": shard_count},
                    param_types={"pk": pt.STRING, "shard_count": pt.INT64},
                )
            )
        if not rows:
            raise RuntimeError(
                f"missing authoritative tr_credit_balance rows for workspace {workspace_id}"
            )
        observed_shards = [int(row[0]) for row in rows]
        if observed_shards != list(range(shard_count)):
            raise RuntimeError(
                "configured tr_credit_balance shard set is incomplete for "
                f"workspace {workspace_id}: expected {list(range(shard_count))}, "
                f"observed {observed_shards}"
            )
        return (
            sum(int(row[1]) for row in rows),
            sum(int(row[2]) for row in rows),
            sum(int(row[3]) for row in rows),
        )

    def read_typed_reservation(self, reservation_id: str) -> dict[str, Any] | None:
        """Point-read a typed reservation for outbox lost-claim disambiguation.

        The drain needs `actual_micro` to distinguish charged replays from
        free releases after claim_reservation reports an already-settled row.
        """
        from trusted_router.storage_gcp_counter_dml import read_reservation

        # Lost-claim recovery decides whether money was already booked; keep its
        # reservation read strong.
        with self._database.snapshot() as snapshot:
            return read_reservation(snapshot, self._param_types, reservation_id)

    def is_typed_reservation(self, reservation_id: str | None, authorization_id: str) -> bool:
        """Settle/refund origin detection (codex 3e): typed iff a tr_reservation
        row exists for this reservation id AND its authorization_id matches — so a
        request that reserved typed settles typed, and a JSON one settles JSON,
        regardless of the current cohort flag.

        Returns false when there is no reservation id."""
        if not reservation_id:
            return False
        from trusted_router.storage_gcp_counter_dml import read_reservation

        # This selects the settle/refund implementation, so an old answer could
        # route money through the wrong ledger. Keep it strong.
        with self._database.snapshot() as snapshot:
            res = read_reservation(snapshot, self._param_types, reservation_id)
        return res is not None and res.get("authorization_id") == authorization_id

    def get_typed_authorization_by_idempotency(
        self, workspace_id: str, key_hash: str, idempotency_key: str
    ) -> GatewayAuthorization | None:
        """Find a typed authorization by its scoped idempotency key so a retry
        replays the typed authorization instead of creating a second hold
        (codex 3e route review #2)."""
        from trusted_router.storage_gcp_counter_dml import read_reservation_by_idempotency
        from trusted_router.storage_gcp_keys import (
            _gateway_authorization_idempotency_index_id,
        )

        scope = _gateway_authorization_idempotency_index_id(workspace_id, key_hash, idempotency_key)
        # This is the authorize replay/double-hold guard, not display state.
        with self._database.snapshot() as snapshot:
            existing = read_reservation_by_idempotency(snapshot, self._param_types, scope)
        if existing is None:
            return None
        return self.get_gateway_authorization(existing["authorization_id"])

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

    def record_client_events_batch(self, payload: dict[str, Any]) -> None:
        outbox = self._operational_analytics_outbox
        if outbox is None:
            log.warning(
                "spanner.client_events_outbox_disabled_drop",
                extra={
                    "tenant": str(payload["tenant_id"])[:12],
                    "batch_id": payload["batch_id"],
                },
            )
            return
        try:
            outbox.enqueue_client_events(payload)
        except Exception as exc:
            if is_duplicate_key_error(exc):
                log.info(
                    "spanner.client_events_duplicate",
                    extra={
                        "tenant": str(payload["tenant_id"])[:12],
                        "batch_id": payload["batch_id"],
                    },
                )
                return
            log.exception(
                "spanner.client_events_enqueue_failed",
                extra={
                    "tenant": str(payload["tenant_id"])[:12],
                    "batch_id": payload["batch_id"],
                    "error_class": type(exc).__name__,
                },
            )
            raise

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
        return self._analytics_read(
            "provider_benchmark_samples",
            bigtable=lambda: self.generation_store.benchmark_samples(
                date=date,
                provider=provider,
                model=model,
                limit=limit,
            ),
            clickhouse=lambda: self._require_operational_analytics().benchmark_samples(
                date=date,
                provider=provider,
                model=model,
                limit=limit,
            ),
        )

    def provider_balanced_benchmark_samples(
        self,
        *,
        cutoff: str | None,
        per_provider_limit: int,
        limit: int,
    ) -> list[ProviderBenchmarkSample]:
        return self._require_operational_analytics().balanced_benchmark_samples(
            cutoff=cutoff,
            per_provider_limit=per_provider_limit,
            limit=limit,
        )

    def public_analytics_snapshot(self, name: str) -> dict[str, Any] | None:
        return self._require_operational_analytics().public_snapshot(name)

    def operational_analytics_outbox_freshness(self) -> OutboxFreshness:
        """The age of the oldest row the drain has not delivered yet.

        Failures are reported as `unreachable`, never as an empty outbox: the
        two are one value apart in the naive shape (`None`) and opposite in
        meaning, and this is the number an external check uses to decide the
        pipeline is alive.

        Bounded like `readiness_check` above, and for the sharper reason that
        this read is on the PUBLIC /status.json path inside an async handler --
        a blocking wait there stops the event loop, not one thread. The budget
        covers the whole 32-shard sweep rather than each statement, so the cap
        is a real ceiling on how long the status page can be held up; a timeout
        raises and degrades to `unreachable` here, and never propagates.
        """
        outbox = self._operational_analytics_outbox
        if outbox is None:
            return OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_NOT_CONFIGURED)
        try:
            oldest = outbox.oldest_enqueued_at(timeout=OUTBOX_FRESHNESS_TIMEOUT_SECONDS)
        except Exception as exc:
            log.exception(
                "spanner.operational_analytics_outbox_freshness_failed",
                extra={"error_class": type(exc).__name__, "error_message": str(exc)[:500]},
            )
            return OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_UNREACHABLE)
        return OutboxFreshness(backend=BACKEND_SPANNER, oldest_enqueued_at=oldest)

    def record_synthetic_probe_sample(self, sample: SyntheticProbeSample) -> None:
        if self._operational_analytics_outbox is not None:
            try:
                self._operational_analytics_outbox.enqueue_synthetic(sample)
            except Exception as exc:
                log.exception(
                    "spanner.operational_analytics_synthetic_enqueue_failed",
                    extra={
                        "sample_id": sample.id,
                        "probe_type": sample.probe_type,
                        "target": sample.target,
                        "error_class": type(exc).__name__,
                        "error_message": str(exc)[:500],
                        "retryable": True,
                    },
                )
                raise
        if not getattr(self, "_bigtable_writes_enabled", True):
            return
        try:
            _bt_write_synthetic_probe_sample(
                self._bt_table,
                self.synthetic_family,
                sample,
                rollup_family=self.synthetic_rollup_family,
                legacy_family=self.legacy_generation_family,
            )
        except Exception as exc:
            log.exception(
                "bigtable.synthetic_mirror_write_failed",
                extra={
                    "sample_id": sample.id,
                    "probe_type": sample.probe_type,
                    "target": sample.target,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "migration_mirror_only": True,
                },
            )

    def synthetic_probe_samples(
        self,
        *,
        date: str | None = None,
        target: str | None = None,
        probe_type: str | None = None,
        monitor_region: str | None = None,
        limit: int = 1000,
    ) -> list[SyntheticProbeSample]:
        return self._analytics_read(
            "synthetic_probe_samples",
            bigtable=lambda: _bt_synthetic_probe_samples(
                self._bt_table,
                (self.synthetic_family, self.legacy_generation_family),
                date=date,
                target=target,
                probe_type=probe_type,
                monitor_region=monitor_region,
                limit=limit,
            ),
            clickhouse=lambda: self._require_operational_analytics().synthetic_samples(
                date=date,
                target=target,
                probe_type=probe_type,
                monitor_region=monitor_region,
                limit=limit,
            ),
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
        return self._analytics_read(
            "synthetic_rollups",
            bigtable=lambda: _bt_synthetic_rollups(
                self._bt_table,
                (self.synthetic_rollup_family, self.legacy_generation_family),
                period=period,
                since=since,
                until=until,
                include_histograms=include_histograms,
                limit=limit,
            ),
            clickhouse=lambda: self._require_operational_analytics().synthetic_rollups(
                period=period,
                since=since,
                until=until,
                include_histograms=include_histograms,
                limit=limit,
            ),
        )

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
        return self.activity_result(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            tag_key=tag_key,
            tag_value=tag_value,
            group_by_tag=group_by_tag,
        ).data

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
        return self.activity_events_result(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=limit,
            tag_key=tag_key,
            tag_value=tag_value,
        ).data

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
        return self._analytics_read(
            "activity_result",
            bigtable=lambda: self.generation_store.activity_result(
                workspace_id,
                api_key_hash=api_key_hash,
                date=date,
                tag_key=tag_key,
                tag_value=tag_value,
                group_by_tag=group_by_tag,
            ),
            clickhouse=lambda: self._clickhouse_activity_result(
                workspace_id,
                api_key_hash=api_key_hash,
                date=date,
                tag_key=tag_key,
                tag_value=tag_value,
                group_by_tag=group_by_tag,
            ),
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
        return self._analytics_read(
            "activity_events_result",
            bigtable=lambda: self.generation_store.activity_events_result(
                workspace_id,
                api_key_hash=api_key_hash,
                date=date,
                limit=limit,
                tag_key=tag_key,
                tag_value=tag_value,
            ),
            clickhouse=lambda: self._clickhouse_activity_events_result(
                workspace_id,
                api_key_hash=api_key_hash,
                date=date,
                limit=limit,
                tag_key=tag_key,
                tag_value=tag_value,
            ),
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
        if granularity == "day":
            days = max(1, window_minutes // 1440)
            today = dt.datetime.now(dt.UTC).date()
            start_day = (today - dt.timedelta(days=days - 1)).isoformat()
            end_day = today.isoformat()
            min_created_at = None
        else:
            now = dt.datetime.now(dt.UTC)
            since = now - dt.timedelta(minutes=max(1, window_minutes))
            start_day = since.date().isoformat()
            end_day = now.date().isoformat()
            min_created_at = since.strftime("%Y-%m-%dT%H:%M:%S")
        return self._analytics_read(
            "usage_series",
            bigtable=lambda: self.generation_store.usage_series(
                workspace_id,
                start_day=start_day,
                end_day=end_day,
                granularity=granularity,
                api_key_hash=api_key_hash,
                by_model=by_model,
                min_created_at=min_created_at,
            ),
            clickhouse=lambda: self._clickhouse_usage_series(
                workspace_id,
                start_day=start_day,
                end_day=end_day,
                granularity=granularity,
                api_key_hash=api_key_hash,
                by_model=by_model,
                min_created_at=min_created_at,
            ),
        )

    def _require_operational_analytics(self) -> OperationalAnalyticsClient:
        if self._operational_analytics is None:
            raise RuntimeError("operational ClickHouse reader is not configured")
        return self._operational_analytics

    def _analytics_read(
        self,
        label: str,
        *,
        bigtable: Callable[[], T],
        clickhouse: Callable[[], T],
    ) -> T:
        if self._analytics_read_mode == "bigtable":
            return bigtable()
        if self._analytics_read_mode == "clickhouse-only":
            return clickhouse()
        if self._analytics_read_mode == "dual":
            primary = bigtable()
            try:
                shadow = clickhouse()
            except Exception as exc:
                self._log_analytics_read_error(label, "clickhouse", exc)
                return primary
            self._compare_analytics_reads(label, primary, shadow)
            return primary
        try:
            primary = clickhouse()
        except Exception as exc:
            self._log_analytics_read_error(label, "clickhouse", exc)
            return bigtable()
        try:
            shadow = bigtable()
        except Exception as exc:
            self._log_analytics_read_error(label, "bigtable", exc)
            return primary
        self._compare_analytics_reads(label, shadow, primary)
        return primary

    def _log_analytics_read_error(
        self,
        label: str,
        backend: str,
        exc: Exception,
    ) -> None:
        if not self._should_log_analytics_event(f"error:{label}:{backend}"):
            return
        log.error(
            "analytics_read_error surface=%s backend=%s error_class=%s error=%s",
            label,
            backend,
            type(exc).__name__,
            str(exc)[:300],
        )

    def _compare_analytics_reads(self, label: str, expected: Any, actual: Any) -> None:
        expected_signature = self._analytics_signature(expected)
        actual_signature = self._analytics_signature(actual)
        if expected_signature == actual_signature:
            return
        if not self._should_log_analytics_event(f"mismatch:{label}"):
            return
        log.warning(
            "analytics_dual_read_mismatch surface=%s expected_count=%s "
            "actual_count=%s expected_fingerprint=%s actual_fingerprint=%s",
            label,
            expected_signature[0],
            actual_signature[0],
            expected_signature[1],
            actual_signature[1],
        )

    def _analytics_signature(self, value: Any) -> tuple[int, str]:
        if isinstance(value, ActivityResult):
            return stable_rows_fingerprint(value.data, grace_seconds=0)
        if isinstance(value, list):
            return stable_rows_fingerprint(
                value,
                grace_seconds=self._analytics_dual_read_grace_seconds,
            )
        if isinstance(value, dict):
            return stable_rows_fingerprint([value], grace_seconds=0)
        return stable_rows_fingerprint([{"value": str(value)}], grace_seconds=0)

    def _should_log_analytics_event(self, key: str) -> bool:
        now = time.monotonic()
        with self._analytics_parity_log_lock:
            previous = self._analytics_last_parity_log.get(key, 0.0)
            if now - previous < 300.0:
                return False
            self._analytics_last_parity_log[key] = now
        return True

    def _clickhouse_activity_rows(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None,
        date: str | None,
        limit: int,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[Generation]:
        tenant_id = analytics_surrogate("workspace", workspace_id)
        key_id = analytics_surrogate("api-key", api_key_hash) if api_key_hash is not None else None
        rows = self._require_operational_analytics().activity_generations(
            tenant_id=tenant_id,
            key_id=key_id,
            tag_key=tag_key,
            tag_value=tag_value,
            date=date,
            limit=limit,
        )
        return filter_generations(
            rows,
            workspace_id=tenant_id,
            date=date,
        )

    def _clickhouse_activity_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None,
        date: str | None,
        tag_key: str | None,
        tag_value: str | None,
        group_by_tag: str | None,
    ) -> ActivityResult:
        scan_limit = 5000
        rows = self._clickhouse_activity_rows(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=scan_limit + 1,
            tag_key=tag_key,
            tag_value=tag_value,
        )
        truncated = len(rows) > scan_limit
        rows = rows[:scan_limit]
        scanned = len(rows)
        rows = filter_generations(
            rows,
            workspace_id=analytics_surrogate("workspace", workspace_id),
        )
        result = summarize_activity_result(
            rows,
            group_by_tag=group_by_tag,
            truncated=truncated,
            scan_limit=scan_limit,
        )
        return dataclasses.replace(result, scanned=scanned)

    def _clickhouse_activity_events_result(
        self,
        workspace_id: str,
        *,
        api_key_hash: str | None,
        date: str | None,
        limit: int,
        tag_key: str | None,
        tag_value: str | None,
    ) -> ActivityResult:
        normalized_limit = max(1, min(limit, 1000))
        rows = self._clickhouse_activity_rows(
            workspace_id,
            api_key_hash=api_key_hash,
            date=date,
            limit=normalized_limit + 1,
            tag_key=tag_key,
            tag_value=tag_value,
        )
        truncated = len(rows) > normalized_limit
        rows = rows[:normalized_limit]
        scanned = len(rows)
        rows = filter_generations(
            rows,
            workspace_id=analytics_surrogate("workspace", workspace_id),
        )
        return ActivityResult(
            data=generation_events(rows, limit=normalized_limit),
            truncated=truncated,
            scanned=scanned,
            scan_limit=normalized_limit,
        )

    def _clickhouse_usage_series(
        self,
        workspace_id: str,
        *,
        start_day: str,
        end_day: str,
        granularity: str,
        api_key_hash: str | None,
        by_model: bool,
        min_created_at: str | None,
    ) -> dict[str, Any]:
        tenant_id = analytics_surrogate("workspace", workspace_id)
        key_id = analytics_surrogate("api-key", api_key_hash) if api_key_hash is not None else None
        end_exclusive = (dt.date.fromisoformat(end_day) + dt.timedelta(days=1)).isoformat()
        rows = self._require_operational_analytics().activity_generations(
            tenant_id=tenant_id,
            key_id=key_id,
            start_at=min_created_at or start_day,
            end_at=end_exclusive,
            limit=200_001,
        )
        truncated = len(rows) > 200_000
        rows = rows[:200_000]
        buckets: dict[str, dict[str, Any]] = {}
        by_model_buckets: dict[str, dict[str, dict[str, Any]]] = {}
        for generation in rows:
            bucket_id = usage_bucket_key(generation.created_at, granularity)
            bucket = buckets.setdefault(bucket_id, _empty_usage_bucket(bucket_id))
            _add_usage_metrics(bucket, generation_metrics(generation))
            if by_model:
                model_bucket = by_model_buckets.setdefault(
                    generation.model,
                    {},
                ).setdefault(bucket_id, _empty_usage_bucket(bucket_id))
                _add_usage_metrics(model_bucket, generation_metrics(generation))
        result: dict[str, Any] = {
            "granularity": granularity,
            "start_day": start_day,
            "end_day": end_day,
            "truncated": truncated,
            "buckets": [buckets[key] for key in sorted(buckets)],
        }
        if by_model:
            result["by_model"] = {
                model: [model_buckets[key] for key in sorted(model_buckets)]
                for model, model_buckets in sorted(by_model_buckets.items())
            }
        return result

    def reconcile_generation_activity(
        self,
        workspace_id: str,
        *,
        date: str | None = None,
        limit: int = 1000,
    ) -> int:
        return self.generation_store.reconcile_activity(workspace_id, date=date, limit=limit)

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
        entity_id = f"{source}#{event_id}"

        def txn(transaction: Any) -> bool:
            existing = self._read_entity_tx(transaction, "webhook_event", entity_id, dict)
            if existing is not None:
                return False
            self._write_entity_tx(
                transaction,
                "webhook_event",
                entity_id,
                {"created_at": iso_now()},
            )
            return True

        return self._run_in_transaction(txn)

    def _resolve_user_identifier(self, identifier: str) -> str | None:
        user = self._read_entity("user", identifier, User)
        if user is not None:
            return user.id
        email_user = self._read_entity("email_user", _normalize_email(identifier), dict)
        if email_user:
            return str(email_user["user_id"])
        return None

    def _read_entity(self, kind: str, entity_id: str, cls: type[T]) -> T | None:
        # This generic helper serves membership, key, and workspace authorization
        # reads as well as display reads, so weakening it globally is unsafe.
        with self._database.snapshot() as snapshot:
            return self._read_entity_from(snapshot, kind, entity_id, cls)

    def _run_in_transaction(self, func: Any, *, attempts: int = 8) -> Any:
        """Bounded ABORTED-retry wrapper around Spanner run_in_transaction.

        Thin instance shim over storage_gcp_io.run_in_transaction_with_retry so
        the store's many call sites stay terse; the shared helper documents the
        hot-row-contention rationale and the caller-idempotency contract.
        """
        return run_in_transaction_with_retry(self._database, func, attempts=attempts)

    def _read_entity_tx(
        self, transaction: Any, kind: str, entity_id: str, cls: type[T]
    ) -> T | None:
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
        if dataclasses.is_dataclass(cls):
            known = {f.name for f in dataclasses.fields(cls)}
            unknown = data.keys() - known
            if unknown:
                # Forward/back-compat: newer releases may persist fields this
                # class does not know yet; drop extras instead of 500ing.
                data = {key: value for key, value in data.items() if key in known}
            return cls(**data)
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
        # Membership and provider-access checks share this generic list helper;
        # a blanket stale policy here could preserve revoked authorization.
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
