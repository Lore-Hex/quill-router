from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
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
from trusted_router.creator_identity import local_creator_username, validate_creator_username
from trusted_router.custom_model_billing import (
    user_model_authorization_id_from_payout_event_id,
)
from trusted_router.custom_model_markup_billing import (
    custom_model_markup_authorization_id_from_payout_event_id,
)
from trusted_router.money import DEFAULT_SIGNUP_CREDIT_MICRODOLLARS
from trusted_router.operational_analytics import (
    OperationalAnalyticsClient,
    stable_rows_fingerprint,
)
from trusted_router.operational_analytics_freshness import (
    BACKEND_DIRECT,
    BACKEND_SPANNER,
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
    OutboxFreshness,
)
from trusted_router.receipt_keys import (
    RECEIPT_KEY_KIND,
    ReceiptKeyWriteOutcome,
    merge_receipt_key_observation,
)
from trusted_router.routable_payouts import (
    EARNINGS_CASHOUT_EXTERNAL_KIND,
    EARNINGS_CASHOUT_IDEMPOTENCY_KIND,
    EARNINGS_CASHOUT_KIND,
    EARNINGS_CASHOUT_PAYABLE_KIND,
    ROUTABLE_PAID_STATUSES,
    ROUTABLE_PAYOUT_PROFILE_COMPANY_KIND,
    ROUTABLE_PAYOUT_PROFILE_KIND,
    ROUTABLE_PENDING_STATUSES,
    payout_entity_id,
    validate_routable_release_status,
)
from trusted_router.security import lookup_hash_api_key, verify_api_key
from trusted_router.spend_leases import (
    SPEND_LEASE_ACTIVE_GRANT_KIND,
    SPEND_LEASE_BOOT_KIND,
    SPEND_LEASE_GENERATION_KIND,
    FrozenSpendLeaseCatalog,
    SpendLeaseArtifact,
    SpendLeaseBoot,
)
from trusted_router.spend_windows import KeyLimitReserveResult
from trusted_router.storage import (
    AcquisitionAttribution,
    ActivationReminderTask,
    AdverseTrustEvent,
    AdverseTrustResult,
    ApiKey,
    ApiKeyUsageSnapshot,
    AuthSession,
    BroadcastDeliveryJob,
    BroadcastDestination,
    ByokProviderConfig,
    ConsentRequest,
    CreditAccount,
    CreditProvenance,
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
    Reservation,
    SignupResult,
    SyntheticProbeSample,
    SyntheticRollup,
    TrustEvent,
    TrustOverride,
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
from trusted_router.storage_errors import (
    StoreConflict,
    StoreUnavailable,
    is_duplicate_key_error,
    is_transient_store_error,
)
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
    update_entity_body_dml,
)
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_COLUMNS,
    CREDIT_BALANCE_TABLE,
    DEFAULT_NEW_BILLING_SHARDS,
    UNSHARDED,
    credit_balance_seed_rows,
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
from trusted_router.storage_gcp_oauth_apps import SpannerOAuthApps
from trusted_router.storage_gcp_oauth_codes import SpannerOAuthCodes
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
    analytics_surrogate,
)
from trusted_router.storage_gcp_rate_limits import SpannerRateLimits
from trusted_router.storage_gcp_request_records import (
    read_gateway_authorization,
    read_gateway_authorization_by_gateway_request_id,
)
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
from trusted_router.storage_gcp_trust import (
    TRUST_EVENT_COLUMNS,
    absorb_unrecovered_recovery_tx,
    apply_adverse_trust_event_tx,
    drain_matching_trust_inbox_tx,
    insert_credit_trust_event,
    insert_trust_inbox_tx,
    recompute_workspace_trust_tier_tx,
    trust_event_row,
)
from trusted_router.storage_gcp_user_models import SpannerUserProvidedModels
from trusted_router.storage_gcp_verification_tokens import SpannerVerificationTokens
from trusted_router.storage_gcp_video_jobs import SpannerVideoJobs
from trusted_router.storage_gcp_wallet_challenges import SpannerWalletChallenges
from trusted_router.storage_models import (
    ApiKeyAuthContext,
    AppMarkupPayout,
    BedrockGroupBuyAggregate,
    BedrockGroupBuyPledge,
    BedrockGroupBuyPublicMessage,
    CreditMovement,
    CustomModelMarkupPayout,
    EarningsCashout,
    ReceiptKey,
    RoutablePayoutProfile,
    SessionAuthContext,
    TypedFinalizeResult,
    UserModelPayout,
    _is_expired,
)
from trusted_router.storage_operational_analytics import (
    OperationalAnalyticsWriter,
)
from trusted_router.trust_ownership import (
    TRUST_OWNER_MUTATION_BUDGET,
    TRUST_REPLICATED_COLUMN_COUNT,
    WorkspaceOwnerLimitExceeded,
    require_owner_trust_budget,
)
from trusted_router.trust_tiers import compute_trust_tier, payment_or_grant_event
from trusted_router.types import IdentityVerificationStatus, UsageType

T = TypeVar("T")
log = logging.getLogger(__name__)


class _AuthorizationReplay(Exception):
    """Divert a rebuilt authorization into the transaction's replay read."""

    def __init__(self, authorization_id: str) -> None:
        super().__init__(authorization_id)
        self.authorization_id = authorization_id


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
        operational_analytics_sink: str = "outbox",
        operational_analytics_clickhouse_write_user: str = "tr",
        operational_analytics_clickhouse_write_password: str = "",
        operational_analytics_clickhouse_url: str = "",
        operational_analytics_clickhouse_user: str = "tr_control_read",
        operational_analytics_clickhouse_password: str = "",
        operational_analytics_clickhouse_database: str = "tr",
        analytics_read_mode: str = "bigtable",
        analytics_dual_read_grace_seconds: int = 30,
        regional_quota_leases_enabled: bool = False,
        regional_quota_bigtable_table: str = "trustedrouter-regional-quota",
        regional_quota_bigtable_app_profiles: dict[str, str] | None = None,
        regional_quota_ledger_timeout_seconds: float = 4.0,
        spend_lease_bigtable_table: str = "trustedrouter-spend-lease",
        spend_lease_bigtable_app_profiles: dict[str, str] | None = None,
        max_workspaces_per_owner: int = 25,
        trust_qualifying_providers: frozenset[str] = frozenset({"stripe", "x402"}),
        trust_tier3_min_days: int = 30,
        trust_tier3_min_paid_microdollars: int = 50_000_000,
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
        self.max_workspaces_per_owner = int(max_workspaces_per_owner)
        self.trust_qualifying_providers = trust_qualifying_providers
        self.trust_tier3_min_days = int(trust_tier3_min_days)
        self.trust_tier3_min_paid_microdollars = int(
            trust_tier3_min_paid_microdollars
        )
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
        # The production service admits eight small billing calls per instance.
        # Match that ceiling so a burst queues on Spanner itself only when the
        # database is saturated, not because this process preallocated too few
        # sessions. Deploys pin the value explicitly; eight remains a safe
        # standalone default under the 2 GiB container limit.
        pool_size = int(os.environ.get("TR_SPANNER_POOL_SIZE", "8"))
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
        self._regional_quota_ledger = None
        self._spend_lease_ledger = None
        self._regional_quota_lease_cache: dict[tuple[str, str, int], Any] = {}
        self._regional_quota_lease_cache_lock = threading.Lock()
        profiles = dict(regional_quota_bigtable_app_profiles or {})
        if regional_quota_leases_enabled:
            if not bigtable_instance_id or not profiles:
                raise ValueError(
                    "regional quota leases require a Bigtable instance and fixed app profiles"
                )
        # Issuance is region-local, but settlement and refund callbacks may land
        # on any control-plane region. Every serving process with the fixed
        # profile map therefore opens the ledger even when local issuance is off.
        if profiles:
            if not bigtable_instance_id:
                raise ValueError("regional quota app profiles require a Bigtable instance")
            try:
                from google.cloud import bigtable
            except ImportError as exc:  # pragma: no cover - production image.
                raise RuntimeError(
                    "Install google-cloud-bigtable when regional quota access is configured"
                ) from exc
            from trusted_router.regional_quota_ledger import (
                BigtableRegionalQuotaLedger,
            )

            quota_instance = bigtable.Client(
                project=project_id,
                credentials=credentials,
                admin=False,
            ).instance(bigtable_instance_id)
            self._regional_quota_ledger = BigtableRegionalQuotaLedger(
                {
                    region: quota_instance.table(
                        regional_quota_bigtable_table,
                        app_profile_id=profile,
                    )
                    for region, profile in profiles.items()
                },
                operation_timeout_seconds=regional_quota_ledger_timeout_seconds,
            )
        spend_profiles = dict(spend_lease_bigtable_app_profiles or {})
        if spend_profiles:
            if not bigtable_instance_id:
                raise ValueError("spend lease app profiles require a Bigtable instance")
            try:
                from google.cloud import bigtable
            except ImportError as exc:  # pragma: no cover - production image.
                raise RuntimeError(
                    "Install google-cloud-bigtable when spend lease access is configured"
                ) from exc
            from trusted_router.spend_lease_ledger import BigtableSpendLeaseLedger

            spend_instance = bigtable.Client(
                project=project_id,
                credentials=credentials,
                admin=False,
            ).instance(bigtable_instance_id)
            self._spend_lease_ledger = BigtableSpendLeaseLedger(
                {
                    profile_region: spend_instance.table(
                        spend_lease_bigtable_table,
                        app_profile_id=profile,
                    )
                    for profile_region, profile in spend_profiles.items()
                }
            )
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
        self._operational_analytics_outbox: OperationalAnalyticsWriter | None
        if operational_analytics_sink == "direct":
            # Telemetry stops touching Spanner entirely: canonical rows go
            # straight to ClickHouse from a bounded in-process buffer. The
            # sink duck-types the outbox writer, so every enqueue site below
            # is unchanged. See operational_analytics_direct.py for why.
            from trusted_router.operational_analytics_direct import (
                DirectOperationalAnalyticsSink,
            )

            self._operational_analytics_outbox = DirectOperationalAnalyticsSink(
                url=operational_analytics_clickhouse_url,
                database=operational_analytics_clickhouse_database,
                user=operational_analytics_clickhouse_write_user,
                password=operational_analytics_clickhouse_write_password,
            )
        else:
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
        self.oauth_app_store = SpannerOAuthApps(io)
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
        email_verified: bool = False,
    ) -> User:
        normalized_email = _normalize_email(email or user_id)

        def txn(transaction: Any) -> User:
            existing = self._read_entity_tx(transaction, "email_user", normalized_email, dict)
            if existing is not None:
                user = self._read_entity_tx(transaction, "user", existing["user_id"], User)
                if user is not None:
                    return user

            new_user = User(
                id=str(uuid.uuid4()),
                email=normalized_email,
                email_verified=email_verified,
                owner_workspace_count=1,
            )
            workspace = Workspace(
                id=str(uuid.uuid4()),
                name="Personal Workspace",
                owner_user_id=new_user.id,
            )
            member = Member(workspace_id=workspace.id, user_id=new_user.id, role="owner")
            initial_total = 0 if trial_credit_microdollars is None else trial_credit_microdollars
            credit = CreditAccount(
                workspace_id=workspace.id,
                shard_count=DEFAULT_NEW_BILLING_SHARDS,
            )
            self._write_entity_tx(transaction, "user", new_user.id, new_user)
            self._write_entity_tx(
                transaction, "email_user", normalized_email, {"user_id": new_user.id}
            )
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            self._write_entity_tx(
                transaction, "member", _member_id(workspace.id, new_user.id), member
            )
            self._write_entity_tx(transaction, "credit", workspace.id, credit)
            self._seed_credit_balance_on_create(
                transaction,
                workspace.id,
                initial_total,
                shard_count=credit.shard_count,
            )
            self._insert_owner_inventory_tx(transaction, new_user.id, workspace.id)
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
        email_verified: bool = False,
    ) -> SignupResult | None:
        if self.find_user_by_email(email) is not None:
            return None
        user = self.ensure_user(
            email,
            email=email,
            trial_credit_microdollars=trial_credit_microdollars,
            email_verified=email_verified,
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
        *,
        shard_count: int,
    ) -> None:
        writer.insert_or_update(
            table=CREDIT_BALANCE_TABLE,
            columns=CREDIT_BALANCE_COLUMNS,
            values=credit_balance_seed_rows(
                workspace_id,
                initial_total_micro,
                self._spanner.COMMIT_TIMESTAMP,
                shard_count=shard_count,
            ),
        )
        if initial_total_micro > 0:
            recorded_at = dt.datetime.now(dt.UTC)
            event = payment_or_grant_event(
                workspace_id,
                f"provisioning:{workspace_id}",
                initial_total_micro,
                CreditProvenance("provisioning", "system", None, recorded_at),
                recorded_at=recorded_at,
            )
            writer.insert_or_update(
                table="tr_trust_event",
                columns=TRUST_EVENT_COLUMNS,
                values=[trust_event_row(event)],
            )

    def _owner_workspace_ids_tx(self, transaction: Any, owner_user_id: str) -> list[str]:
        return [
            str(row[0])
            for row in transaction.execute_sql(
                "SELECT workspace_id FROM tr_owner_workspace "
                "WHERE owner_user_id=@owner ORDER BY workspace_id",
                params={"owner": owner_user_id},
                param_types={"owner": self._param_types.STRING},
            )
        ]

    def _owner_shard_counts_tx(
        self, transaction: Any, owner_user_id: str
    ) -> tuple[list[str], list[int]]:
        workspace_ids = self._owner_workspace_ids_tx(transaction, owner_user_id)
        counts: list[int] = []
        for workspace_id in workspace_ids:
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
            if account is None:
                raise RuntimeError("owner inventory references a missing credit account")
            counts.append(credit_shard_count(account))
        return workspace_ids, counts

    def _require_owner_growth_tx(
        self,
        transaction: Any,
        owner_user_id: str,
        *,
        added_shards: int,
        enforce_count: bool,
    ) -> tuple[list[str], User | None]:
        workspace_ids, shard_counts = self._owner_shard_counts_tx(
            transaction, owner_user_id
        )
        if enforce_count and len(workspace_ids) >= self.max_workspaces_per_owner:
            raise WorkspaceOwnerLimitExceeded(
                "owner has reached TR_MAX_WORKSPACES_PER_OWNER"
            )
        require_owner_trust_budget([*shard_counts, added_shards])
        owner = self._read_entity_tx(transaction, "user", owner_user_id, User)
        return workspace_ids, owner

    def _insert_owner_inventory_tx(
        self, transaction: Any, owner_user_id: str, workspace_id: str
    ) -> None:
        transaction.insert_or_update(
            table="tr_owner_workspace",
            columns=("owner_user_id", "workspace_id"),
            values=[(owner_user_id, workspace_id)],
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
        credit = CreditAccount(
            workspace_id=workspace.id,
            shard_count=DEFAULT_NEW_BILLING_SHARDS,
        )
        def txn(transaction: Any) -> Workspace:
            owned, owner = self._require_owner_growth_tx(
                transaction,
                owner_user_id,
                added_shards=credit.shard_count,
                enforce_count=True,
            )
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            self._write_entity_tx(
                transaction, "member", _member_id(workspace.id, owner_user_id), member
            )
            self._write_entity_tx(transaction, "credit", workspace.id, credit)
            self._seed_credit_balance_on_create(
                transaction,
                workspace.id,
                initial_total,
                shard_count=credit.shard_count,
            )
            self._insert_owner_inventory_tx(transaction, owner_user_id, workspace.id)
            if owner is not None:
                owner.owner_workspace_count = len(owned) + 1
                self._write_entity_tx(transaction, "user", owner.id, owner)
            return workspace

        return self._run_in_transaction(txn)

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
            owner: User | None = None
            account: CreditAccount | None = None
            owned: list[str] = []
            if deleted is not None and deleted != workspace.deleted:
                account = self._read_entity_tx(
                    transaction, "credit", workspace_id, CreditAccount
                )
                if account is None:
                    raise RuntimeError("workspace is missing its credit account")
                if deleted:
                    owned = self._owner_workspace_ids_tx(
                        transaction, workspace.owner_user_id
                    )
                    owner = self._read_entity_tx(
                        transaction, "user", workspace.owner_user_id, User
                    )
                    transaction.delete(
                        "tr_owner_workspace",
                        self._spanner.KeySet(
                            keys=[(workspace.owner_user_id, workspace_id)]
                        ),
                    )
                    now = dt.datetime.now(dt.UTC)
                    shard_rows = list(
                        transaction.execute_sql(
                            "SELECT shard, trust_latched_at FROM tr_credit_balance "
                            "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count "
                            "ORDER BY shard",
                            params={
                                "pk": workspace_id,
                                "shard_count": credit_shard_count(account),
                            },
                            param_types={
                                "pk": self._param_types.STRING,
                                "shard_count": self._param_types.INT64,
                            },
                        )
                    )
                    if len(shard_rows) != credit_shard_count(account):
                        raise RuntimeError(
                            "configured tr_credit_balance shard set is incomplete"
                        )
                    lease_rows = list(
                        transaction.execute_sql(
                            "SELECT kind, id, body FROM tr_entities WHERE "
                            "kind IN ('spend_lease','regional_quota_lease') "
                            "AND JSON_VALUE(body, '$.workspace_id')=@pk ORDER BY kind, id",
                            params={"pk": workspace_id},
                            param_types={"pk": self._param_types.STRING},
                        )
                    )
                    for lease_kind, lease_id, raw_body in lease_rows:
                        body = json.loads(str(raw_body))
                        if lease_kind == "spend_lease" and body.get("state") != "CLOSED":
                            body["state"] = "TOMBSTONED"
                            body["closing_at"] = now.isoformat()
                        elif (
                            lease_kind == "regional_quota_lease"
                            and body.get("state") != "closed"
                        ):
                            body["state"] = "quarantined"
                            body["last_error"] = "workspace_archived"
                            body["updated_at"] = now.isoformat()
                        else:
                            continue
                        self._write_entity_tx(
                            transaction,
                            str(lease_kind),
                            str(lease_id),
                            body,
                        )
                    transaction.insert_or_update(
                        table=CREDIT_BALANCE_TABLE,
                        columns=(
                            "workspace_id",
                            "shard",
                            "trust_tier",
                            "trust_latched_at",
                        ),
                        values=[
                            (workspace_id, int(shard), 0, latched_at or now)
                            for shard, latched_at in shard_rows
                        ],
                    )
                else:
                    owned, owner = self._require_owner_growth_tx(
                        transaction,
                        workspace.owner_user_id,
                        added_shards=credit_shard_count(account),
                        enforce_count=True,
                    )
                    self._insert_owner_inventory_tx(
                        transaction, workspace.owner_user_id, workspace_id
                    )
            if name is not None:
                workspace.name = name
            if deleted is not None:
                workspace.deleted = deleted
            if billing_paused is not None:
                causes = set(workspace.billing_pause_causes)
                if billing_paused:
                    causes.add("migration")
                else:
                    causes.discard("migration")
                workspace.billing_pause_causes = sorted(causes)
                workspace.billing_paused = bool(causes)
            if billing_pause_reason is not None:
                workspace.billing_pause_reason = billing_pause_reason
            if owner is not None and deleted is not None:
                owner.owner_workspace_count = (
                    max(0, len(owned) - 1) if deleted else len(owned) + 1
                )
                self._write_entity_tx(transaction, "user", owner.id, owner)
            self._write_entity_tx(transaction, "workspace", workspace.id, workspace)
            return None if workspace.deleted else workspace

        return self._run_in_transaction(txn)

    def transfer_workspace_ownership(
        self, workspace_id: str, new_owner_user_id: str
    ) -> Workspace:
        def txn(transaction: Any) -> Workspace:
            workspace = self._read_entity_tx(
                transaction, "workspace", workspace_id, Workspace
            )
            account = self._read_entity_tx(
                transaction, "credit", workspace_id, CreditAccount
            )
            if workspace is None or workspace.deleted or account is None:
                raise ValueError("workspace_not_found")
            old_owner_user_id = workspace.owner_user_id
            if old_owner_user_id == new_owner_user_id:
                return workspace
            new_owned, new_owner = self._require_owner_growth_tx(
                transaction,
                new_owner_user_id,
                added_shards=credit_shard_count(account),
                enforce_count=True,
            )
            old_owned = self._owner_workspace_ids_tx(transaction, old_owner_user_id)
            old_owner = self._read_entity_tx(
                transaction, "user", old_owner_user_id, User
            )
            transaction.delete(
                "tr_owner_workspace",
                self._spanner.KeySet(keys=[(old_owner_user_id, workspace_id)]),
            )
            self._insert_owner_inventory_tx(
                transaction, new_owner_user_id, workspace_id
            )
            workspace.owner_user_id = new_owner_user_id
            self._write_entity_tx(transaction, "workspace", workspace_id, workspace)
            old_member = self._read_entity_tx(
                transaction,
                "member",
                _member_id(workspace_id, old_owner_user_id),
                Member,
            )
            if old_member is not None:
                old_member.role = "admin"
                self._write_entity_tx(
                    transaction,
                    "member",
                    _member_id(workspace_id, old_owner_user_id),
                    old_member,
                )
            self._write_entity_tx(
                transaction,
                "member",
                _member_id(workspace_id, new_owner_user_id),
                Member(
                    workspace_id=workspace_id,
                    user_id=new_owner_user_id,
                    role="owner",
                ),
            )
            if old_owner is not None:
                old_owner.owner_workspace_count = max(0, len(old_owned) - 1)
                self._write_entity_tx(transaction, "user", old_owner.id, old_owner)
            if new_owner is not None:
                new_owner.owner_workspace_count = len(new_owned) + 1
                self._write_entity_tx(transaction, "user", new_owner.id, new_owner)
            return workspace

        return self._run_in_transaction(txn)

    def _workspace_trust_events_tx(
        self, transaction: Any, workspace_id: str
    ) -> list[TrustEvent]:
        rows = transaction.execute_sql(
            "SELECT " + ", ".join(TRUST_EVENT_COLUMNS) + " FROM tr_trust_event "  # noqa: S608
            "WHERE workspace_id=@pk",
            params={"pk": workspace_id},
            param_types={"pk": self._param_types.STRING},
        )
        return [TrustEvent(*row) for row in rows]

    def set_workspace_trust_override(
        self,
        workspace_id: str,
        *,
        tier: int,
        identity_bypass: bool,
        operator_identity: str,
        reason: str,
    ) -> TrustOverride:
        if isinstance(tier, bool) or not 0 <= int(tier) <= 3:
            raise ValueError("trust override tier must be between 0 and 3")
        if not operator_identity.strip() or not reason.strip():
            raise ValueError("operator identity and reason are required")

        def txn(transaction: Any) -> TrustOverride:
            workspace = self._read_entity_tx(
                transaction, "workspace", workspace_id, Workspace
            )
            account = self._read_entity_tx(
                transaction, "credit", workspace_id, CreditAccount
            )
            if workspace is None or workspace.deleted or account is None:
                raise ValueError("workspace_not_found")
            owner = self._read_entity_tx(
                transaction, "user", workspace.owner_user_id, User
            )
            shard_count = credit_shard_count(account)
            shard_rows = list(
                transaction.execute_sql(
                    "SELECT shard, trust_latched_at FROM tr_credit_balance "
                    "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count "
                    "ORDER BY shard",
                    params={"pk": workspace_id, "shard_count": shard_count},
                    param_types={
                        "pk": self._param_types.STRING,
                        "shard_count": self._param_types.INT64,
                    },
                )
            )
            if [int(row[0]) for row in shard_rows] != list(range(shard_count)):
                raise RuntimeError("configured tr_credit_balance shard set is incomplete")
            latches = {row[1] for row in shard_rows}
            if len(latches) != 1:
                raise RuntimeError("replicated trust latch diverged")
            now = dt.datetime.now(dt.UTC)
            decision = compute_trust_tier(
                self._workspace_trust_events_tx(transaction, workspace_id),
                owner_identity_status=owner.identity_status if owner else "none",
                trust_latched_at=shard_rows[0][1],
                trust_override_tier=int(tier),
                qualifying_providers=self.trust_qualifying_providers,
                tier3_min_days=self.trust_tier3_min_days,
                tier3_min_paid_microdollars=self.trust_tier3_min_paid_microdollars,
                now=now,
                identity_bypass=bool(identity_bypass),
            )
            record = TrustOverride(
                workspace_id=workspace_id,
                tier=int(tier),
                identity_bypass=bool(identity_bypass),
                operator_identity=operator_identity.strip(),
                reason=reason.strip(),
                set_at=now,
            )
            transaction.insert_or_update(
                table="tr_trust_override",
                columns=(
                    "workspace_id",
                    "tier",
                    "identity_bypass",
                    "operator_identity",
                    "reason",
                    "set_at",
                ),
                values=[(
                    record.workspace_id,
                    record.tier,
                    record.identity_bypass,
                    record.operator_identity,
                    record.reason,
                    record.set_at,
                )],
            )
            transaction.insert_or_update(
                table=CREDIT_BALANCE_TABLE,
                columns=(
                    "workspace_id",
                    "shard",
                    "trust_tier",
                    "trust_computed_at",
                    "trust_override_tier",
                ),
                values=[
                    (
                        workspace_id,
                        int(shard),
                        decision.effective_tier,
                        now,
                        int(tier),
                    )
                    for shard, _latch in shard_rows
                ],
            )
            return record

        return self._run_in_transaction(txn)

    def _existing_operator_abuse(self, abuse_ref: str) -> tuple[str, str] | None:
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT workspace_id, kind FROM tr_trust_event "
                    "WHERE provider=@provider AND adverse_ref=@adverse_ref",
                    params={"provider": "operator", "adverse_ref": abuse_ref},
                    param_types={
                        "provider": self._param_types.STRING,
                        "adverse_ref": self._param_types.STRING,
                    },
                )
            )
        if len(rows) > 1:
            raise RuntimeError("operator abuse reference dedup invariant violated")
        return None if not rows else (str(rows[0][0]), str(rows[0][1]))

    def record_workspace_abuse_and_demote(
        self,
        workspace_id: str,
        *,
        abuse_ref: str,
        operator_identity: str,
        reason: str,
    ) -> bool:
        abuse_ref = abuse_ref.strip()
        operator_identity = operator_identity.strip()
        reason = reason.strip()
        if not abuse_ref or not operator_identity or not reason:
            raise ValueError("abuse_ref, operator identity and reason are required")

        def txn(transaction: Any) -> bool:
            existing = list(
                transaction.execute_sql(
                    "SELECT workspace_id, kind FROM tr_trust_event "
                    "WHERE provider=@provider AND adverse_ref=@adverse_ref",
                    params={"provider": "operator", "adverse_ref": abuse_ref},
                    param_types={
                        "provider": self._param_types.STRING,
                        "adverse_ref": self._param_types.STRING,
                    },
                )
            )
            if existing:
                if str(existing[0][0]) != workspace_id or str(existing[0][1]) != "abuse":
                    raise ValueError("abuse_ref_conflict")
                return False
            workspace = self._read_entity_tx(
                transaction, "workspace", workspace_id, Workspace
            )
            account = self._read_entity_tx(
                transaction, "credit", workspace_id, CreditAccount
            )
            if workspace is None or workspace.deleted or account is None:
                raise ValueError("workspace_not_found")
            shard_count = credit_shard_count(account)
            shard_rows = list(
                transaction.execute_sql(
                    "SELECT shard, trust_latched_at, billing_pause_causes, pause_epoch "
                    "FROM tr_credit_balance WHERE workspace_id=@pk AND shard>=0 "
                    "AND shard<@shard_count ORDER BY shard",
                    params={"pk": workspace_id, "shard_count": shard_count},
                    param_types={
                        "pk": self._param_types.STRING,
                        "shard_count": self._param_types.INT64,
                    },
                )
            )
            if [int(row[0]) for row in shard_rows] != list(range(shard_count)):
                raise RuntimeError("configured tr_credit_balance shard set is incomplete")
            replicated = {
                (row[1], tuple(sorted(row[2] or ())), int(row[3] or 0))
                for row in shard_rows
            }
            if len(replicated) != 1:
                raise RuntimeError("replicated trust controls diverged")
            latched_at, old_causes, _pause_epoch = next(iter(replicated))
            now = dt.datetime.now(dt.UTC)
            event = TrustEvent(
                workspace_id=workspace_id,
                event_id=f"abuse:{abuse_ref}",
                kind="abuse",
                provider="operator",
                amount_micro=0,
                original_payment_ref=None,
                adverse_ref=abuse_ref,
                occurred_at=now,
                recorded_at=now,
                payment_amount_micro=None,
                currency=None,
                credited_micro=None,
                recovered_micro=None,
                provider_subtype="operator",
                lifecycle_status="succeeded",
                cumulative_refunded=None,
                recovery_target=0,
                debit_status="debited",
                unrecovered_micro=0,
                provider_ordering_watermark=None,
            )
            if not insert_credit_trust_event(
                transaction, self._param_types, event
            ):
                return False
            insert_entity_dml_at(
                transaction,
                self._param_types,
                "trust_abuse",
                f"{workspace_id}#{abuse_ref}",
                _json_body(
                    {
                        "workspace_id": workspace_id,
                        "abuse_ref": abuse_ref,
                        "operator_identity": operator_identity,
                        "reason": reason,
                        "recorded_at": now.isoformat(),
                    }
                ),
                now,
            )
            causes = sorted({*old_causes, "abuse"})
            updated = transaction.execute_update(
                "UPDATE tr_credit_balance SET trust_tier=0, "
                "trust_latched_at=COALESCE(trust_latched_at,@now), "
                "billing_pause_causes=@causes, pause_epoch=COALESCE(pause_epoch,0)+1, "
                "updated_at=@now WHERE workspace_id=@pk AND shard>=0 "
                "AND shard<@shard_count",
                params={
                    "now": now,
                    "causes": causes,
                    "pk": workspace_id,
                    "shard_count": shard_count,
                },
                param_types={
                    "now": self._param_types.TIMESTAMP,
                    "causes": self._param_types.Array(self._param_types.STRING),
                    "pk": self._param_types.STRING,
                    "shard_count": self._param_types.INT64,
                },
            )
            if int(updated) != shard_count:
                raise RuntimeError("abuse latch did not cover every active shard")
            workspace.billing_pause_causes = causes
            workspace.billing_paused = True
            workspace.billing_pause_reason = reason
            if update_entity_body_dml(
                transaction,
                self._param_types,
                "workspace",
                workspace_id,
                _json_body(workspace),
                now,
            ) != 1:
                raise RuntimeError("workspace disappeared during abuse latch")
            return True

        try:
            return self._run_in_transaction(txn)
        except Exception as exc:
            if not is_duplicate_key_error(exc):
                raise
            existing = self._existing_operator_abuse(abuse_ref)
            if existing == (workspace_id, "abuse"):
                return False
            raise ValueError("abuse_ref_conflict") from exc

    def clear_workspace_abuse_pause(
        self,
        workspace_id: str,
        *,
        abuse_ref: str,
        operator_identity: str,
        reason: str,
    ) -> bool:
        abuse_ref = abuse_ref.strip()
        operator_identity = operator_identity.strip()
        reason = reason.strip()
        if not abuse_ref or not operator_identity or not reason:
            raise ValueError("abuse_ref, operator identity and reason are required")
        audit_id = f"{workspace_id}#{abuse_ref}"

        def txn(transaction: Any) -> bool:
            if self._read_entity_tx(
                transaction, "trust_abuse_clear", audit_id, dict
            ) is not None:
                return False
            workspace = self._read_entity_tx(
                transaction, "workspace", workspace_id, Workspace
            )
            account = self._read_entity_tx(
                transaction, "credit", workspace_id, CreditAccount
            )
            if workspace is None or workspace.deleted or account is None:
                raise ValueError("workspace_not_found")
            shard_count = credit_shard_count(account)
            shard_rows = list(
                transaction.execute_sql(
                    "SELECT shard, billing_pause_causes, pause_epoch "
                    "FROM tr_credit_balance WHERE workspace_id=@pk AND shard>=0 "
                    "AND shard<@shard_count ORDER BY shard",
                    params={"pk": workspace_id, "shard_count": shard_count},
                    param_types={
                        "pk": self._param_types.STRING,
                        "shard_count": self._param_types.INT64,
                    },
                )
            )
            if [int(row[0]) for row in shard_rows] != list(range(shard_count)):
                raise RuntimeError("configured tr_credit_balance shard set is incomplete")
            controls = {
                (tuple(sorted(row[1] or ())), int(row[2] or 0))
                for row in shard_rows
            }
            if len(controls) != 1:
                raise RuntimeError("replicated pause controls diverged")
            old_causes, _pause_epoch = next(iter(controls))
            causes = sorted(set(old_causes) - {"abuse"})
            now = dt.datetime.now(dt.UTC)
            insert_entity_dml_at(
                transaction,
                self._param_types,
                "trust_abuse_clear",
                audit_id,
                _json_body(
                    {
                        "workspace_id": workspace_id,
                        "abuse_ref": abuse_ref,
                        "operator_identity": operator_identity,
                        "reason": reason,
                        "cleared_at": now.isoformat(),
                    }
                ),
                now,
            )
            updated = transaction.execute_update(
                "UPDATE tr_credit_balance SET billing_pause_causes=@causes, "
                "pause_epoch=COALESCE(pause_epoch,0)+1, updated_at=@now "
                "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count",
                params={
                    "causes": causes,
                    "now": now,
                    "pk": workspace_id,
                    "shard_count": shard_count,
                },
                param_types={
                    "causes": self._param_types.Array(self._param_types.STRING),
                    "now": self._param_types.TIMESTAMP,
                    "pk": self._param_types.STRING,
                    "shard_count": self._param_types.INT64,
                },
            )
            if int(updated) != shard_count:
                raise RuntimeError("abuse clear did not cover every active shard")
            workspace.billing_pause_causes = causes
            workspace.billing_paused = bool(causes)
            workspace.billing_pause_reason = reason
            if update_entity_body_dml(
                transaction,
                self._param_types,
                "workspace",
                workspace_id,
                _json_body(workspace),
                now,
            ) != 1:
                raise RuntimeError("workspace disappeared during abuse clear")
            return True

        try:
            return self._run_in_transaction(txn)
        except Exception as exc:
            if is_duplicate_key_error(exc):
                return False
            raise

    def backfill_owner_inventory(self, *, source_version: str, environment: str) -> int:
        source_version = source_version.strip()
        environment = environment.strip()
        if not source_version or not environment:
            raise ValueError("source_version and environment are required")

        def txn(transaction: Any) -> int:
            workspace_rows = list(
                transaction.execute_sql(
                    "SELECT id, body FROM tr_entities WHERE kind='workspace' ORDER BY id"
                )
            )
            expected: set[tuple[str, str]] = set()
            for workspace_id, raw in workspace_rows:
                workspace = _auth_record(str(raw), Workspace)
                if not workspace.deleted and not workspace.federated_home:
                    expected.add((workspace.owner_user_id, str(workspace_id)))
            actual = {
                (str(owner), str(workspace_id))
                for owner, workspace_id in transaction.execute_sql(
                    "SELECT owner_user_id, workspace_id FROM tr_owner_workspace "
                    "ORDER BY owner_user_id, workspace_id"
                )
            }
            for owner_user_id, workspace_id in sorted(expected - actual):
                self._insert_owner_inventory_tx(
                    transaction, owner_user_id, workspace_id
                )
            extra = sorted(actual - expected)
            if extra:
                transaction.delete(
                    "tr_owner_workspace", self._spanner.KeySet(keys=extra)
                )
            owners = {owner for owner, _workspace_id in expected | actual}
            for owner_user_id in sorted(owners):
                user = self._read_entity_tx(
                    transaction, "user", owner_user_id, User
                )
                if user is not None:
                    user.owner_workspace_count = sum(
                        owner == owner_user_id for owner, _workspace_id in expected
                    )
                    self._write_entity_tx(transaction, "user", user.id, user)
            now = dt.datetime.now(dt.UTC)
            transaction.insert_or_update(
                table="tr_trust_backfill",
                columns=(
                    "provider",
                    "account_id",
                    "environment",
                    "source",
                    "source_version",
                    "history_start",
                    "closed_through",
                    "consistency_delay_seconds",
                    "unmatched_count",
                    "semantic_mismatch_count",
                    "completed_at",
                ),
                values=[(
                    "owner_inventory",
                    "local",
                    environment,
                    "tr_entities.workspace",
                    source_version,
                    now,
                    now,
                    0,
                    0,
                    0,
                    now,
                )],
            )
            return len(expected ^ actual)

        return self._run_in_transaction(txn)

    def process_trust_demotion_remainders(self, *, limit: int = 25) -> int:
        if limit <= 0:
            return 0
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT owner_user_id, workspace_id, target_identity_ceiling "
                    "FROM tr_trust_demotion_remainder ORDER BY created_at LIMIT @limit",
                    params={"limit": int(limit)},
                    param_types={"limit": self._param_types.INT64},
                )
            )
        completed = 0
        for owner_user_id, workspace_id, ceiling in rows:
            owner_id = str(owner_user_id)
            remainder_workspace_id = str(workspace_id)
            target_ceiling = int(ceiling)

            def txn(
                transaction: Any,
                *,
                owner_id: str = owner_id,
                remainder_workspace_id: str = remainder_workspace_id,
                target_ceiling: int = target_ceiling,
            ) -> bool:
                current = list(
                    transaction.execute_sql(
                        "SELECT target_identity_ceiling FROM tr_trust_demotion_remainder "
                        "WHERE owner_user_id=@owner AND workspace_id=@workspace",
                        params={
                            "owner": owner_id,
                            "workspace": remainder_workspace_id,
                        },
                        param_types={
                            "owner": self._param_types.STRING,
                            "workspace": self._param_types.STRING,
                        },
                    )
                )
                if not current:
                    return False
                override_rows = list(
                    transaction.execute_sql(
                        "SELECT identity_bypass FROM tr_trust_override "
                        "WHERE workspace_id=@pk",
                        params={"pk": remainder_workspace_id},
                        param_types={"pk": self._param_types.STRING},
                    )
                )
                identity_bypass = bool(override_rows and override_rows[0][0])
                account = self._read_entity_tx(
                    transaction, "credit", remainder_workspace_id, CreditAccount
                )
                if account is None:
                    raise RuntimeError("demotion remainder references missing credit")
                shard_count = credit_shard_count(account)
                shard_rows = list(
                    transaction.execute_sql(
                        "SELECT shard, trust_tier FROM tr_credit_balance "
                        "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count "
                        "ORDER BY shard",
                        params={
                            "pk": remainder_workspace_id,
                            "shard_count": shard_count,
                        },
                        param_types={
                            "pk": self._param_types.STRING,
                            "shard_count": self._param_types.INT64,
                        },
                    )
                )
                if [int(row[0]) for row in shard_rows] != list(range(shard_count)):
                    raise RuntimeError(
                        "configured tr_credit_balance shard set is incomplete"
                    )
                now = dt.datetime.now(dt.UTC)
                if not identity_bypass:
                    transaction.insert_or_update(
                        table=CREDIT_BALANCE_TABLE,
                        columns=(
                            "workspace_id",
                            "shard",
                            "trust_tier",
                            "trust_computed_at",
                        ),
                        values=[
                            (
                                remainder_workspace_id,
                                int(shard),
                                min(int(tier or 0), target_ceiling),
                                now,
                            )
                            for shard, tier in shard_rows
                        ],
                    )
                transaction.delete(
                    "tr_trust_demotion_remainder",
                    self._spanner.KeySet(
                        keys=[(owner_id, remainder_workspace_id)]
                    ),
                )
                return True

            completed += int(self._run_in_transaction(txn))
        return completed

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

    def find_user_by_username(self, username: str) -> User | None:
        try:
            normalized = validate_creator_username(username)
        except ValueError:
            return None
        record = self._read_entity("username_user", normalized, dict)
        if not record:
            return None
        return self.get_user(str(record["user_id"]))

    def claim_user_username(self, user_id: str, username: str) -> User:
        normalized = validate_creator_username(username)

        def txn(transaction: Any) -> User:
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None:
                raise ValueError("user_not_found")
            if user.username:
                if user.username == normalized:
                    return user
                raise ValueError("creator_username_immutable")
            existing = self._read_entity_tx(
                transaction, "username_user", normalized, dict
            )
            if existing is not None and existing.get("user_id") != user_id:
                raise ValueError("creator_username_taken")
            user.username = normalized
            self._write_entity_tx(transaction, "user", user.id, user)
            self._write_entity_tx(
                transaction,
                "username_user",
                normalized,
                {"user_id": user.id},
            )
            return user

        return self._run_in_transaction(txn)

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
            new_user = User(
                id=str(uuid.uuid4()),
                email=None,
                wallet_address=normalized,
                owner_workspace_count=1,
            )
            workspace = Workspace(
                id=str(uuid.uuid4()),
                name="Personal Workspace",
                owner_user_id=new_user.id,
            )
            member = Member(workspace_id=workspace.id, user_id=new_user.id, role="owner")
            credit = CreditAccount(
                workspace_id=workspace.id,
                shard_count=DEFAULT_NEW_BILLING_SHARDS,
            )
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
                shard_count=credit.shard_count,
            )
            self._insert_owner_inventory_tx(transaction, new_user.id, workspace.id)
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

    def _demote_owner_trust_tx(
        self, transaction: Any, owner_user_id: str, *, now: dt.datetime
    ) -> None:
        workspace_ids, shard_counts = self._owner_shard_counts_tx(
            transaction, owner_user_id
        )
        total_cost = sum(shard_counts) * TRUST_REPLICATED_COLUMN_COUNT
        remaining_budget = TRUST_OWNER_MUTATION_BUDGET
        for workspace_id, shard_count in zip(
            workspace_ids, shard_counts, strict=True
        ):
            cost = shard_count * TRUST_REPLICATED_COLUMN_COUNT
            if total_cost > TRUST_OWNER_MUTATION_BUDGET and cost > remaining_budget:
                transaction.insert_or_update(
                    table="tr_trust_demotion_remainder",
                    columns=(
                        "owner_user_id",
                        "workspace_id",
                        "target_identity_ceiling",
                        "created_at",
                        "attempts",
                        "last_error",
                    ),
                    values=[(owner_user_id, workspace_id, 1, now, 0, None)],
                )
                log.error(
                    "trust.identity_demotion_remainder owner=%s workspace=%s",
                    owner_user_id,
                    workspace_id,
                )
                continue
            remaining_budget -= cost
            bypass_rows = list(
                transaction.execute_sql(
                    "SELECT identity_bypass FROM tr_trust_override "
                    "WHERE workspace_id=@pk",
                    params={"pk": workspace_id},
                    param_types={"pk": self._param_types.STRING},
                )
            )
            identity_bypass = bool(bypass_rows and bypass_rows[0][0])
            rows = list(
                transaction.execute_sql(
                    "SELECT shard, trust_tier FROM tr_credit_balance "
                    "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count "
                    "ORDER BY shard",
                    params={"pk": workspace_id, "shard_count": shard_count},
                    param_types={
                        "pk": self._param_types.STRING,
                        "shard_count": self._param_types.INT64,
                    },
                )
            )
            if [int(row[0]) for row in rows] != list(range(shard_count)):
                raise RuntimeError("configured tr_credit_balance shard set is incomplete")
            transaction.insert_or_update(
                table=CREDIT_BALANCE_TABLE,
                columns=(
                    "workspace_id",
                    "shard",
                    "trust_tier",
                    "trust_computed_at",
                ),
                values=[
                    (
                        workspace_id,
                        int(shard),
                        int(tier or 0) if identity_bypass else min(int(tier or 0), 1),
                        now,
                    )
                    for shard, tier in rows
                ],
            )

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
            was_approved = user.identity_status == "approved"
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
            if was_approved and normalized is not IdentityVerificationStatus.APPROVED:
                self._demote_owner_trust_tx(
                    transaction, user_id, now=dt.datetime.now(dt.UTC)
                )
            return user

        return self._run_in_transaction(txn)

    def apply_veriff_identity_decision(
        self,
        user_id: str,
        *,
        event_id: str,
        session_id: str,
        status: str,
        decision_code: int,
        decision_reason: str | None = None,
        decision_reason_code: int | None = None,
        verified_name: str | None = None,
    ) -> str:
        marker_id = f"veriff#{event_id}"

        def txn(transaction: Any) -> str:
            if self._read_entity_tx(transaction, "webhook_event", marker_id, dict):
                return "replayed"
            now = dt.datetime.now(dt.UTC)
            self._write_entity_tx(
                transaction, "webhook_event", marker_id, {"created_at": iso_now()}
            )
            user = self._read_entity_tx(transaction, "user", user_id, User)
            if user is None or user.veriff_session_id != session_id:
                return "ignored"
            was_approved = user.identity_status == "approved"
            normalized = IdentityVerificationStatus.coerce(status)
            if normalized is IdentityVerificationStatus.APPROVED and not user.identity_verified_at:
                user.identity_verified_at = iso_now()
            user.identity_status = normalized.value
            user.veriff_decision_code = decision_code
            if decision_reason is not None:
                user.veriff_decision_reason = decision_reason
            if decision_reason_code is not None:
                user.veriff_decision_reason_code = decision_reason_code
            if verified_name is not None:
                user.identity_verified_name = verified_name
            self._write_entity_tx(transaction, "user", user.id, user)
            if was_approved and normalized is not IdentityVerificationStatus.APPROVED:
                self._demote_owner_trust_tx(transaction, user_id, now=now)
            return "applied"

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
        client_app_id: str = "",
        scopes: list[str] | None = None,
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
            scopes=scopes,
        )

    def consume_oauth_authorization_code(self, raw_code: str) -> OAuthAuthorizationCode | None:
        return self.oauth_code_store.consume(raw_code)

    def create_consent_request(self, consent: ConsentRequest) -> ConsentRequest:
        self._write_entity("consent_request", consent.id, consent)
        return consent

    def get_consent_request(self, consent_id: str) -> ConsentRequest | None:
        return self._read_entity("consent_request", consent_id, ConsentRequest)

    def consume_consent_request(
        self, consent_id: str, *, user_id: str, workspace_id: str, csrf_token: str
    ) -> ConsentRequest | None:
        def txn(transaction: Any) -> ConsentRequest | None:
            consent = self._read_entity_tx(
                transaction, "consent_request", consent_id, ConsentRequest
            )
            if (
                consent is None
                or consent.consumed_at is not None
                or _is_expired(consent.consent_expires_at)
                or consent.user_id != user_id
                or consent.workspace_id != workspace_id
                or not hmac.compare_digest(consent.csrf_token, csrf_token)
            ):
                return None
            consent.consumed_at = iso_now()
            self._write_entity_tx(transaction, "consent_request", consent.id, consent)
            return consent

        return self._run_in_transaction(txn)

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
        scopes: list[str] | None = None,
        app_id: str = "",
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
            scopes=scopes,
            app_id=app_id,
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
        workspace = _auth_record(str(rows[0][1]), Workspace) if rows[0][1] is not None else None
        if workspace is not None and workspace.deleted:
            workspace = None
        return ApiKeyAuthContext(api_key=api_key, workspace=workspace)

    def list_keys(self, workspace_id: str) -> list[ApiKey]:
        return self.api_keys.list_for_workspace(workspace_id)

    def list_api_keys_with_usage(self, workspace_id: str) -> list[ApiKeyUsageSnapshot]:
        return self.api_keys.list_with_usage_for_workspace(workspace_id)

    def delete_key(self, key_hash: str) -> bool:
        return self.api_keys.delete(key_hash)

    def supports_key_writes(self) -> bool:
        return True

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
        owner_username: str | None = None,
        markup_basis_points: int = 0,
        enabled: bool = True,
        slug: str | None = None,
    ) -> CustomModel:
        user = self.get_user(owner_user_id)
        resolved_username = owner_username or (
            local_creator_username(user) if user is not None else "dev-creator"
        )
        return self.custom_model_store.create(
            owner_user_id=owner_user_id,
            owner_workspace_id=owner_workspace_id,
            owner_username=resolved_username,
            name=name,
            base_model_id=base_model_id,
            hidden_prompt=hidden_prompt,
            markup_basis_points=markup_basis_points,
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
        owner_username: str | None = None,
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
        user = self.get_user(owner_user_id)
        resolved_username = owner_username or (
            local_creator_username(user) if user is not None else "dev-creator"
        )
        return self.user_model_store.create(
            owner_user_id=owner_user_id,
            owner_workspace_id=owner_workspace_id,
            owner_username=resolved_username,
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
        provenance: CreditProvenance,
        payment_amount_microdollars: int | None = None,
        currency: str | None = None,
        lifetime_topup_user_id: str | None = None,
    ) -> bool:
        def txn(transaction: Any) -> tuple[bool, tuple[AdverseTrustResult, ...]]:
            if self._read_entity_tx(transaction, "stripe_event", event_id, dict) is not None:
                return False, ()
            amount = int(amount_microdollars)
            account = self._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
            if account is None:
                raise ValueError("credit account not found")
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            created_at = now.isoformat().replace("+00:00", "Z")
            trust_event_inserted = insert_credit_trust_event(
                transaction,
                self._param_types,
                payment_or_grant_event(
                    workspace_id,
                    event_id,
                    amount,
                    provenance,
                    recorded_at=now,
                    payment_amount_microdollars=payment_amount_microdollars,
                    currency=currency,
                ),
            )
            if not trust_event_inserted:
                return False, ()
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
            drained: tuple[AdverseTrustResult, ...] = ()
            if provenance.external_ref is not None:
                drained = drain_matching_trust_inbox_tx(
                    transaction,
                    self._param_types,
                    provider=provenance.provider,
                    original_payment_ref=provenance.external_ref,
                    now=now,
                    read_entity_tx=self._read_entity_tx,
                    write_entity_tx=self._write_entity_trust_dml_tx,
                )
            return True, drained

        credited, drained = self._run_in_transaction(txn)
        for result in drained:
            self._alert_unrecovered_principal(result)
        return credited

    @staticmethod
    def _alert_unrecovered_principal(result: AdverseTrustResult) -> None:
        from trusted_router.services.trust_recovery import alert_unrecovered_principal

        alert_unrecovered_principal(result)

    def record_adverse_trust_event(self, event: AdverseTrustEvent) -> AdverseTrustResult:
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)

        def txn(transaction: Any) -> AdverseTrustResult:
            resolved_event = event
            result = apply_adverse_trust_event_tx(
                transaction,
                self._param_types,
                event,
                now=now,
                read_entity_tx=self._read_entity_tx,
                write_entity_tx=self._write_entity_trust_dml_tx,
            )
            if result is None and event.provider == "stripe":
                x402_event = dataclasses.replace(event, provider="x402")
                x402_result = apply_adverse_trust_event_tx(
                    transaction,
                    self._param_types,
                    x402_event,
                    now=now,
                    read_entity_tx=self._read_entity_tx,
                    write_entity_tx=self._write_entity_trust_dml_tx,
                )
                if x402_result is not None:
                    resolved_event = x402_event
                    result = x402_result
            if result is not None:
                return result
            insert_trust_inbox_tx(
                transaction,
                self._param_types,
                resolved_event,
                received_at=now,
            )
            return AdverseTrustResult("inbox", provider=resolved_event.provider)

        result = self._run_in_transaction(txn)
        if result.outcome in {"stale", "illegal"}:
            log.warning(
                "trust.adverse_transition_%s provider=%s adverse_ref=%s status=%s",
                result.outcome,
                event.provider,
                event.adverse_ref,
                event.lifecycle_status,
            )
        self._alert_unrecovered_principal(result)
        return result

    def list_stale_trust_inbox(
        self, *, older_than: dt.datetime
    ) -> tuple[Any, ...]:
        from trusted_router.storage_models import TrustInboxRow

        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                "SELECT provider, adverse_ref, payload, received_at "
                "FROM tr_trust_inbox WHERE received_at<@older_than "
                "ORDER BY received_at, provider, adverse_ref",
                params={"older_than": older_than},
                param_types={"older_than": self._param_types.TIMESTAMP},
            )
            return tuple(TrustInboxRow(*row) for row in rows)

    def credit_workspace_once(
        self, workspace_id: str, amount_microdollars: int, event_id: str
    ) -> bool:
        return self.credit_workspace_typed_direct(
            workspace_id,
            amount_microdollars,
            event_id,
            provenance=CreditProvenance(
                source="grant",
                provider="system",
                external_ref=None,
                occurred_at=dt.datetime.now(dt.UTC),
            ),
        )

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
        amount = int(amount_microdollars)
        shard_count = credit_shard_count(account)
        absorbed = absorb_unrecovered_recovery_tx(
            transaction,
            self._param_types,
            workspace_id=workspace_id,
            amount_micro=amount,
            shard_count=shard_count,
            now=now,
            read_entity_tx=self._read_entity_tx,
            write_entity_tx=self._write_entity_trust_dml_tx,
        )
        deltas = distribute_credit_amount(
            amount - absorbed,
            shard_count,
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

    def _write_entity_trust_dml_tx(
        self,
        transaction: Any,
        kind: str,
        entity_id: str,
        value: Any,
    ) -> None:
        updated = update_entity_body_dml(
            transaction,
            self._param_types,
            kind,
            entity_id,
            _json_body(value),
            dt.datetime.now(dt.UTC),
        )
        if int(updated) != 1:
            raise RuntimeError("trust transaction lost its workspace compatibility row")

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
        is_app_markup = event_id.startswith("app_markup_payout:")
        is_custom_markup = event_id.startswith("custom_model_markup_payout:")

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
                kind=(
                    "app_markup_payout"
                    if is_app_markup
                    else "custom_model_markup_payout"
                    if is_custom_markup
                    else "custom_model_payout"
                ),
                amount_microdollars=amount,
                counterparty_account_id=payer_workspace_id,
                custom_model_id=custom_model_id,
                authorization_id=(
                    event_id.split(":", 1)[1]
                    if is_app_markup
                    else custom_model_markup_authorization_id_from_payout_event_id(
                        event_id
                    )
                    if is_custom_markup
                    else user_model_authorization_id_from_payout_event_id(event_id)
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
            insert_credit_trust_event(
                transaction,
                pt,
                payment_or_grant_event(
                    workspace_id,
                    event_id,
                    amount,
                    CreditProvenance(
                        source="grant",
                        provider="system",
                        external_ref=None,
                        occurred_at=now,
                    ),
                    recorded_at=now,
                ),
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

    def get_routable_payout_profile(
        self,
        user_id: str,
    ) -> RoutablePayoutProfile | None:
        return self._read_entity(
            ROUTABLE_PAYOUT_PROFILE_KIND,
            user_id,
            RoutablePayoutProfile,
        )

    def get_routable_payout_profile_by_company(
        self,
        routable_company_id: str,
    ) -> RoutablePayoutProfile | None:
        link = self._read_entity(
            ROUTABLE_PAYOUT_PROFILE_COMPANY_KIND,
            routable_company_id,
            dict,
        )
        if link is None:
            return None
        user_id = str(link.get("user_id") or "")
        return self.get_routable_payout_profile(user_id) if user_id else None

    def upsert_routable_payout_profile(
        self,
        profile: RoutablePayoutProfile,
    ) -> RoutablePayoutProfile:
        def txn(transaction: Any) -> RoutablePayoutProfile:
            company_link = self._read_entity_tx(
                transaction,
                ROUTABLE_PAYOUT_PROFILE_COMPANY_KIND,
                profile.routable_company_id,
                dict,
            )
            if company_link is not None and company_link.get("user_id") != profile.user_id:
                raise StoreConflict("Routable company is already linked to another user")
            previous = self._read_entity_tx(
                transaction,
                ROUTABLE_PAYOUT_PROFILE_KIND,
                profile.user_id,
                RoutablePayoutProfile,
            )
            if (
                previous is not None
                and previous.routable_company_id != profile.routable_company_id
            ):
                self._delete_entities_tx(
                    transaction,
                    ROUTABLE_PAYOUT_PROFILE_COMPANY_KIND,
                    [previous.routable_company_id],
                )
            self._write_entity_tx(
                transaction,
                ROUTABLE_PAYOUT_PROFILE_KIND,
                profile.user_id,
                profile,
            )
            self._write_entity_tx(
                transaction,
                ROUTABLE_PAYOUT_PROFILE_COMPANY_KIND,
                profile.routable_company_id,
                {"user_id": profile.user_id},
            )
            return profile

        return cast(RoutablePayoutProfile, self._run_in_transaction(txn))

    def reserve_earnings_cashout(
        self,
        cashout: EarningsCashout,
        *,
        idempotency_entity_id: str,
    ) -> tuple[str, EarningsCashout | None]:
        amount = self._positive_money_amount(cashout.amount_microdollars)
        entity_id = payout_entity_id(cashout.user_id, cashout.id)

        def txn(transaction: Any) -> tuple[str, EarningsCashout | None]:
            previous = self._read_entity_tx(
                transaction,
                EARNINGS_CASHOUT_IDEMPOTENCY_KIND,
                idempotency_entity_id,
                dict,
            )
            if previous is not None:
                if previous.get("fingerprint") != cashout.idempotency_fingerprint:
                    return "conflict", None
                existing = self._read_entity_tx(
                    transaction,
                    EARNINGS_CASHOUT_KIND,
                    payout_entity_id(
                        str(previous.get("user_id") or ""),
                        str(previous.get("payout_id") or ""),
                    ),
                    EarningsCashout,
                )
                if existing is None:
                    raise StoreConflict("cash-out idempotency record lost its payout")
                return "duplicate", existing
            if (
                self._read_entity_tx(
                    transaction,
                    EARNINGS_CASHOUT_KIND,
                    entity_id,
                    EarningsCashout,
                )
                is not None
            ):
                return "conflict", None
            external_link = self._read_entity_tx(
                transaction,
                EARNINGS_CASHOUT_EXTERNAL_KIND,
                cashout.external_id,
                dict,
            )
            if external_link is not None:
                return "conflict", None
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            pt = self._param_types
            updated = transaction.execute_update(
                "UPDATE tr_earnings_balance "
                "SET total_transferred = total_transferred + @amount, updated_at=@now "
                "WHERE user_id=@user_id AND shard=0 "
                "AND (total_earned - total_transferred) >= @amount",
                params={"amount": amount, "now": now, "user_id": cashout.user_id},
                param_types={
                    "amount": pt.INT64,
                    "now": pt.TIMESTAMP,
                    "user_id": pt.STRING,
                },
            )
            if updated == 0:
                return "insufficient", None
            self._insert_credit_movement_tx(
                transaction,
                account_id=f"user:{cashout.user_id}",
                movement_id=f"earnings_cashout:{cashout.id}",
                kind="earnings_cashout_reserved",
                amount_microdollars=-amount,
                counterparty_account_id="routable",
                created_at=now,
            )
            insert_entity_dml_at(
                transaction,
                pt,
                EARNINGS_CASHOUT_KIND,
                entity_id,
                _json_body(cashout),
                now,
            )
            insert_entity_dml_at(
                transaction,
                pt,
                EARNINGS_CASHOUT_IDEMPOTENCY_KIND,
                idempotency_entity_id,
                _json_body(
                    {
                        "fingerprint": cashout.idempotency_fingerprint,
                        "payout_id": cashout.id,
                        "user_id": cashout.user_id,
                    }
                ),
                now,
            )
            insert_entity_dml_at(
                transaction,
                pt,
                EARNINGS_CASHOUT_EXTERNAL_KIND,
                cashout.external_id,
                _json_body(
                    {"user_id": cashout.user_id, "payout_id": cashout.id}
                ),
                now,
            )
            return "accepted", cashout

        return cast(
            tuple[str, EarningsCashout | None],
            self._run_in_transaction(txn),
        )

    def get_earnings_cashout(
        self,
        user_id: str,
        payout_id: str,
    ) -> EarningsCashout | None:
        return self._read_entity(
            EARNINGS_CASHOUT_KIND,
            payout_entity_id(user_id, payout_id),
            EarningsCashout,
        )

    def get_earnings_cashout_by_routable_payable(
        self,
        routable_payable_id: str,
    ) -> EarningsCashout | None:
        link = self._read_entity(
            EARNINGS_CASHOUT_PAYABLE_KIND,
            routable_payable_id,
            dict,
        )
        if link is None:
            return None
        user_id = str(link.get("user_id") or "")
        payout_id = str(link.get("payout_id") or "")
        return self.get_earnings_cashout(user_id, payout_id) if user_id and payout_id else None

    def get_earnings_cashout_by_external_id(
        self,
        external_id: str,
    ) -> EarningsCashout | None:
        link = self._read_entity(
            EARNINGS_CASHOUT_EXTERNAL_KIND,
            external_id,
            dict,
        )
        if link is None:
            return None
        user_id = str(link.get("user_id") or "")
        payout_id = str(link.get("payout_id") or "")
        return self.get_earnings_cashout(user_id, payout_id) if user_id and payout_id else None

    def list_earnings_cashouts(
        self,
        user_id: str,
        *,
        limit: int = 50,
    ) -> list[EarningsCashout]:
        return self._list_entities(
            EARNINGS_CASHOUT_KIND,
            cls=EarningsCashout,
            prefix=f"{user_id}#",
            limit=max(0, min(int(limit), 100)),
        )

    def mark_earnings_cashout(
        self,
        user_id: str,
        payout_id: str,
        *,
        state: str,
        routable_payable_id: str | None = None,
        routable_status: str | None = None,
        error_code: str | None = None,
        increment_attempts: bool = False,
    ) -> EarningsCashout | None:
        entity_id = payout_entity_id(user_id, payout_id)

        def txn(transaction: Any) -> EarningsCashout | None:
            existing = self._read_entity_tx(
                transaction,
                EARNINGS_CASHOUT_KIND,
                entity_id,
                EarningsCashout,
            )
            if existing is None:
                return None
            payable_id = routable_payable_id or existing.routable_payable_id
            link: dict[str, Any] | None = None
            if payable_id:
                link = self._read_entity_tx(
                    transaction,
                    EARNINGS_CASHOUT_PAYABLE_KIND,
                    payable_id,
                    dict,
                )
                if link is not None and (
                    link.get("user_id") != user_id or link.get("payout_id") != payout_id
                ):
                    raise StoreConflict("Routable payable is already linked")
            balance_status = (
                "paid"
                if routable_status in ROUTABLE_PAID_STATUSES
                else existing.balance_status
            )
            balance_revision = existing.balance_revision
            if (
                existing.balance_status == "released"
                and routable_status in ROUTABLE_PENDING_STATUSES | ROUTABLE_PAID_STATUSES
            ):
                now = dt.datetime.now(dt.UTC).replace(microsecond=0)
                pt = self._param_types
                updated_rows = transaction.execute_update(
                    "UPDATE tr_earnings_balance "
                    "SET total_transferred = total_transferred + @amount, updated_at=@now "
                    "WHERE user_id=@user_id AND shard=0",
                    params={
                        "amount": existing.amount_microdollars,
                        "now": now,
                        "user_id": user_id,
                    },
                    param_types={
                        "amount": pt.INT64,
                        "now": pt.TIMESTAMP,
                        "user_id": pt.STRING,
                    },
                )
                if updated_rows != 1:
                    raise StoreConflict("cash-out earnings account is missing")
                balance_revision += 1
                balance_status = (
                    "paid" if routable_status in ROUTABLE_PAID_STATUSES else "reserved"
                )
                self._insert_credit_movement_tx(
                    transaction,
                    account_id=f"user:{user_id}",
                    movement_id=(
                        f"earnings_cashout_reinstated:{payout_id}:{balance_revision}"
                    ),
                    kind="earnings_cashout_reinstated",
                    amount_microdollars=-existing.amount_microdollars,
                    counterparty_account_id="routable",
                    created_at=now,
                )
            updated = dataclasses.replace(
                existing,
                state=state,
                balance_status=balance_status,
                routable_payable_id=payable_id,
                routable_status=routable_status or existing.routable_status,
                error_code=error_code,
                attempts=existing.attempts + int(increment_attempts),
                balance_revision=balance_revision,
                updated_at=iso_now(),
            )
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            pt = self._param_types
            if (
                update_entity_body_dml(
                    transaction,
                    pt,
                    EARNINGS_CASHOUT_KIND,
                    entity_id,
                    _json_body(updated),
                    now,
                )
                != 1
            ):
                raise StoreConflict("cash-out disappeared during update")
            if payable_id and link is None:
                insert_entity_dml_at(
                    transaction,
                    pt,
                    EARNINGS_CASHOUT_PAYABLE_KIND,
                    payable_id,
                    _json_body({"user_id": user_id, "payout_id": payout_id}),
                    now,
                )
            return updated

        return cast(EarningsCashout | None, self._run_in_transaction(txn))

    def release_earnings_cashout(
        self,
        user_id: str,
        payout_id: str,
        *,
        state: str,
        routable_status: str | None = None,
        error_code: str | None = None,
    ) -> tuple[str, EarningsCashout | None]:
        validate_routable_release_status(routable_status)
        entity_id = payout_entity_id(user_id, payout_id)

        def txn(transaction: Any) -> tuple[str, EarningsCashout | None]:
            existing = self._read_entity_tx(
                transaction,
                EARNINGS_CASHOUT_KIND,
                entity_id,
                EarningsCashout,
            )
            if existing is None:
                return "not_found", None
            if existing.balance_status == "released":
                return "duplicate", existing
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            pt = self._param_types
            updated_rows = transaction.execute_update(
                "UPDATE tr_earnings_balance "
                "SET total_transferred = total_transferred - @amount, updated_at=@now "
                "WHERE user_id=@user_id AND shard=0 AND total_transferred >= @amount",
                params={
                    "amount": existing.amount_microdollars,
                    "now": now,
                    "user_id": user_id,
                },
                param_types={
                    "amount": pt.INT64,
                    "now": pt.TIMESTAMP,
                    "user_id": pt.STRING,
                },
            )
            if updated_rows != 1:
                raise StoreConflict("cash-out reservation exceeds transferred earnings")
            balance_revision = existing.balance_revision + 1
            updated = dataclasses.replace(
                existing,
                state=state,
                balance_status="released",
                routable_status=routable_status or existing.routable_status,
                error_code=error_code,
                balance_revision=balance_revision,
                updated_at=iso_now(),
            )
            self._insert_credit_movement_tx(
                transaction,
                account_id=f"user:{user_id}",
                movement_id=(
                    f"earnings_cashout_reversal:{payout_id}:{balance_revision}"
                ),
                kind="earnings_cashout_reversed",
                amount_microdollars=existing.amount_microdollars,
                counterparty_account_id="routable",
                created_at=now,
            )
            if (
                update_entity_body_dml(
                    transaction,
                    pt,
                    EARNINGS_CASHOUT_KIND,
                    entity_id,
                    _json_body(updated),
                    now,
                )
                != 1
            ):
                raise StoreConflict("cash-out disappeared during release")
            return "released", updated

        return cast(
            tuple[str, EarningsCashout | None],
            self._run_in_transaction(txn),
        )

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
        key_reserved_microdollars: int,
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
        app_markup_basis_points: int = 0,
        receipt_fee_basis_points: int = 0,
        app_owner_user_id: str = "",
        custom_model_id: str | None = None,
        custom_model_revision: int | None = None,
        custom_model_markup_basis_points: int = 0,
        custom_model_owner_user_id: str = "",
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
        spend_lease: SpendLeaseArtifact | None = None,
        invocation_nonce: str | None = None,
    ) -> GatewayAuthorization:
        return self.api_keys.create_gateway_authorization(
            workspace_id=workspace_id,
            key_hash=key_hash,
            model_id=model_id,
            provider=provider,
            usage_type=usage_type,
            estimated_microdollars=estimated_microdollars,
            credit_reservation_id=credit_reservation_id,
            key_reserved_microdollars=key_reserved_microdollars,
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
            app_markup_basis_points=app_markup_basis_points,
            receipt_fee_basis_points=receipt_fee_basis_points,
            app_owner_user_id=app_owner_user_id,
            custom_model_id=custom_model_id,
            custom_model_revision=custom_model_revision,
            custom_model_markup_basis_points=custom_model_markup_basis_points,
            custom_model_owner_user_id=custom_model_owner_user_id,
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
            spend_lease=spend_lease,
            invocation_nonce=invocation_nonce,
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

    def get_gateway_authorization_by_gateway_request_id(
        self, gateway_request_id: str
    ) -> GatewayAuthorization | None:
        # The evidence endpoint is deliberately a strong indexed read. A stale
        # 404 after settlement would make the cross-repository gate report a
        # false failure, while scanning one row per request is not viable.
        with self._database.snapshot(multi_use=True) as snapshot:
            return read_gateway_authorization_by_gateway_request_id(
                snapshot,
                self._param_types,
                gateway_request_id,
            )

    def heartbeat_gateway_typed(self, **kwargs: Any) -> Any:
        from trusted_router.storage_gcp_stage_d import heartbeat_gateway_atomic

        return heartbeat_gateway_atomic(
            self._database,
            self._param_types,
            **kwargs,
        )

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
        app_markup_payout: AppMarkupPayout | None = None,
        custom_model_markup_payout: CustomModelMarkupPayout | None = None,
    ) -> bool:
        return self.typed_finalize_gateway_authorization_result(
            authorization_id,
            success=success,
            actual_microdollars=actual_microdollars,
            selected_usage_type=selected_usage_type,
            generation=generation,
            user_model_payout=user_model_payout,
            app_markup_payout=app_markup_payout,
            custom_model_markup_payout=custom_model_markup_payout,
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
        app_markup_payout: AppMarkupPayout | None = None,
        custom_model_markup_payout: CustomModelMarkupPayout | None = None,
        settle_outbox_done: tuple[str, str] | None = None,
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
        regional_hold_unknown = False
        if authorization.settlement == "regional_lease":
            from trusted_router.services.regional_quota_leases import (
                LeaseState,
                UnknownRegionalReservationError,
            )

            try:
                self._finalize_regional_quota_hold(
                    authorization,
                    success=success,
                    actual_microdollars=actual_microdollars,
                )
            except UnknownRegionalReservationError:
                # A pre-CAS-fix stale Bigtable writer could erase a newer hold
                # while the request's Spanner reservation remained durable.
                # Disable further issuance from the damaged lease before the
                # direct booking; otherwise its erased capacity could be spent
                # again during the remaining TTL. Claim the Spanner reservation
                # below as the exactly-once boundary. Every failure to durably
                # drain remains retryable/fail-closed.
                ledger = self._regional_quota_ledger
                assert ledger is not None
                local = ledger.get(
                    str(authorization.regional_lease_id),
                    region=str(authorization.region),
                )
                if local is None:
                    from trusted_router.regional_quota_ledger import (
                        RegionalLeaseNotFound,
                    )

                    raise RegionalLeaseNotFound("regional lease was not found") from None
                if local.state == LeaseState.ACTIVE:
                    local = ledger.begin_drain(
                        local.lease_id,
                        region=local.region,
                        fencing_token=local.fencing_token,
                    )
                if local.state not in {
                    LeaseState.DRAINING,
                    LeaseState.CLOSED,
                    LeaseState.QUARANTINED,
                }:
                    raise RuntimeError("damaged regional lease remained available") from None
                regional_hold_unknown = True
                log.error(
                    "regional quota hold missing; using typed reservation fallback "
                    "authorization_id=%s lease_id=%s",
                    authorization.id,
                    authorization.regional_lease_id,
                )
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
            app_markup_payout=app_markup_payout,
            custom_model_markup_payout=custom_model_markup_payout,
            regional_hold_unknown=regional_hold_unknown,
            settle_outbox_done=settle_outbox_done,
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
                outbox_marked=result.get("outbox_marked"),
            )
        return TypedFinalizeResult(
            finalized=False,
            activity_indexed=False,
        )  # already_settled / not_found

    def authorize_gateway_regional(
        self,
        *,
        authorization_id: str,
        workspace_id: str,
        key_hash: str,
        key_usage_shards: int,
        estimate: int,
        model_id: str,
        provider: str,
        requested_model_id: str | None,
        candidate_model_ids: list[str],
        region: str,
        endpoint_id: str | None,
        candidate_endpoint_ids: list[str],
        idempotency_key: str | None,
        idempotency_fingerprint: str | None,
        tags: dict[str, str] | None,
        expires_at: dt.datetime,
        lease_ttl_seconds: int,
        lease_max_microdollars: int,
        lease_max_available_basis_points: int,
        lease_shard_count: int,
        app_id: str = "",
        app_markup_basis_points: int = 0,
        receipt_fee_basis_points: int = 0,
        app_owner_user_id: str = "",
        invocation_nonce: str | None = None,
    ) -> tuple[str, GatewayAuthorization | None]:
        """Authorize from bounded regional escrow without touching hot counters."""

        ledger = self._regional_quota_ledger
        # Do not reserve global Spanner escrow for a region that has no fixed
        # transactional Bigtable app profile. Unsupported regions remain on
        # the exact Spanner path without creating a lease to quarantine later.
        if ledger is None or not ledger.supports_region(region):
            return "unavailable", None
        from trusted_router.regional_quota_ledger import (
            RegionalLeaseLedgerError,
            RegionalLeaseNotFound,
        )
        from trusted_router.services.regional_quota_leases import (
            LeaseExhaustedError,
            LeaseUnavailableError,
        )
        from trusted_router.storage_gcp_keys import (
            _gateway_authorization_idempotency_index_id,
        )
        from trusted_router.storage_gcp_regional_quota import (
            activate_regional_quota_lease,
            active_regional_quota_leases,
            grant_regional_quota_lease,
            quarantine_regional_quota_lease,
            record_regional_gateway_authorization,
            regional_lease_from_global,
        )

        scope = (
            _gateway_authorization_idempotency_index_id(
                workspace_id,
                key_hash,
                idempotency_key,
            )
            if idempotency_key is not None
            else None
        )
        if lease_shard_count <= 0:
            return "unavailable", None
        shard_source = idempotency_fingerprint or authorization_id
        quota_shard = (
            int.from_bytes(
                hashlib.sha256(shard_source.encode("utf-8")).digest()[:4],
                "big",
            )
            % lease_shard_count
        )
        cache_key = (workspace_id, region, quota_shard)
        with self._regional_quota_lease_cache_lock:
            cached = self._regional_quota_lease_cache.get(cache_key)
        candidates = []
        if cached is not None and cached.expires_datetime > dt.datetime.now(dt.UTC):
            candidates.append(cached)
        else:
            candidates.extend(
                active_regional_quota_leases(
                    self,
                    workspace_id=workspace_id,
                    region=region,
                    quota_shard=quota_shard,
                )
            )

        selected_global = None
        selected_local = None
        key_shard = randomized_credit_shards(
            key_usage_shard_count({"usage_shard_count": key_usage_shards})
        )[0]
        for candidate in candidates:
            try:
                local = ledger.get(candidate.lease_id, region=region)
                if local is None:
                    quarantine_regional_quota_lease(
                        self,
                        candidate,
                        reason="active global lease has no regional row",
                    )
                    continue
                selected_local = ledger.reserve(
                    candidate.lease_id,
                    region=region,
                    hold_id=authorization_id,
                    fingerprint=idempotency_fingerprint or authorization_id,
                    amount_microdollars=estimate,
                    fencing_token=candidate.fencing_token,
                    key_hash=key_hash,
                    key_shard=key_shard,
                    hold_expires_at=expires_at,
                )
                selected_global = candidate
                break
            except (LeaseExhaustedError, LeaseUnavailableError):
                continue
            except RegionalLeaseNotFound:
                # The row was readable a moment ago and is gone at reserve
                # time: ledger inconsistency, not latency. Keep the traceback.
                log.warning(
                    "regional quota lease vanished between read and reserve "
                    "workspace_id=%s region=%s",
                    workspace_id,
                    region,
                    exc_info=True,
                )
                return "unavailable", None
            except RegionalLeaseLedgerError as exc:
                # Expected under cross-region latency or row contention: the
                # request continues on the exact Spanner path. One line, no
                # traceback — a traceback here is what fed Error Reporting.
                log.warning(
                    "regional quota lease read/reserve failed workspace_id=%s region=%s "
                    "error=%s cause=%s",
                    workspace_id,
                    region,
                    exc,
                    type(exc.__cause__).__name__ if exc.__cause__ else "-",
                )
                return "unavailable", None

        if selected_global is None:
            # These caps describe the whole regional pool. Divide both across
            # the independently fenced rows so sharding removes contention
            # without multiplying the globally escrowed exposure.
            per_shard_cap = max(1, lease_max_microdollars // lease_shard_count)
            per_shard_basis_points = max(
                1,
                lease_max_available_basis_points // lease_shard_count,
            )
            global_lease = grant_regional_quota_lease(
                self,
                workspace_id=workspace_id,
                region=region,
                quota_shard=quota_shard,
                requested_microdollars=per_shard_cap,
                per_lease_cap_microdollars=per_shard_cap,
                max_available_basis_points=per_shard_basis_points,
                ttl_seconds=lease_ttl_seconds,
                minimum_grant_microdollars=estimate,
            )
            if global_lease is None:
                return "unavailable", None
            try:
                ledger.initialize(regional_lease_from_global(global_lease))
                global_lease = activate_regional_quota_lease(self, global_lease)
                selected_local = ledger.reserve(
                    global_lease.lease_id,
                    region=region,
                    hold_id=authorization_id,
                    fingerprint=idempotency_fingerprint or authorization_id,
                    amount_microdollars=estimate,
                    fencing_token=global_lease.fencing_token,
                    key_hash=key_hash,
                    key_shard=key_shard,
                    hold_expires_at=expires_at,
                )
                selected_global = global_lease
            except Exception as exc:
                try:
                    quarantine_regional_quota_lease(
                        self,
                        global_lease,
                        reason=f"regional initialization ambiguity: {type(exc).__name__}",
                    )
                except Exception:
                    log.error(
                        "regional quota quarantine failed lease_id=%s",
                        global_lease.lease_id,
                        exc_info=True,
                    )
                log.warning(
                    "regional quota lease initialization failed workspace_id=%s region=%s",
                    workspace_id,
                    region,
                    exc_info=True,
                )
                return "unavailable", None

        assert selected_global is not None and selected_local is not None
        with self._regional_quota_lease_cache_lock:
            self._regional_quota_lease_cache[cache_key] = selected_global
        authorization = GatewayAuthorization(
            id=authorization_id,
            workspace_id=workspace_id,
            key_hash=key_hash,
            model_id=model_id,
            provider=provider,
            usage_type=UsageType.CREDITS,
            estimated_microdollars=estimate,
            requested_model_id=requested_model_id,
            candidate_model_ids=list(candidate_model_ids),
            region=region,
            endpoint_id=endpoint_id,
            candidate_endpoint_ids=list(candidate_endpoint_ids),
            idempotency_key=idempotency_key,
            tags=dict(tags or {}),
            idempotency_fingerprint=idempotency_fingerprint,
            app_id=app_id,
            settlement="regional_lease",
            regional_lease_id=selected_global.lease_id,
            regional_fencing_token=selected_global.fencing_token,
            regional_hold_id=authorization_id,
            stage_d_reason="pricing_kind",
            invocation_nonce=invocation_nonce,
        )
        try:
            result = record_regional_gateway_authorization(
                self,
                authorization=authorization,
                idempotency_scope=scope,
                idempotency_fingerprint=idempotency_fingerprint,
                expires_at=expires_at,
            )
        except Exception:
            self._refund_regional_quota_hold_safely(authorization)
            raise
        if result["outcome"] == "accepted":
            authorization.credit_reservation_id = str(result["reservation_id"])
            return "accepted", authorization
        self._refund_regional_quota_hold_safely(authorization)
        if result["outcome"] == "idempotency_mismatch":
            return "idempotency_mismatch", None
        replay = self.get_gateway_authorization(str(result["authorization_id"]))
        return "replay", replay

    def _finalize_regional_quota_hold(
        self,
        authorization: GatewayAuthorization,
        *,
        success: bool,
        actual_microdollars: int,
    ) -> None:
        ledger = self._regional_quota_ledger
        if ledger is None:
            from trusted_router.regional_quota_ledger import (
                RegionalLeaseLedgerError,
            )

            raise RegionalLeaseLedgerError("regional quota ledger is unavailable")
        lease_id = authorization.regional_lease_id
        fencing_token = authorization.regional_fencing_token
        hold_id = authorization.regional_hold_id
        region = authorization.region
        if not lease_id or not fencing_token or not hold_id or not region:
            raise RuntimeError("regional authorization is missing lease identity")
        if not 0 <= actual_microdollars <= authorization.estimated_microdollars:
            raise RuntimeError("regional settlement exceeds its exact reservation")
        if success:
            ledger.settle(
                lease_id,
                region=region,
                hold_id=hold_id,
                actual_microdollars=actual_microdollars,
                fencing_token=fencing_token,
            )
        else:
            ledger.refund(
                lease_id,
                region=region,
                hold_id=hold_id,
                fencing_token=fencing_token,
            )

    def _refund_regional_quota_hold_safely(
        self,
        authorization: GatewayAuthorization,
    ) -> None:
        try:
            self._finalize_regional_quota_hold(
                authorization,
                success=False,
                actual_microdollars=0,
            )
        except Exception:
            log.error(
                "regional quota hold compensation failed authorization_id=%s lease_id=%s",
                authorization.id,
                authorization.regional_lease_id,
                exc_info=True,
            )

    def reconcile_regional_quota_leases(
        self,
        *,
        limit: int = 100,
        now: dt.datetime | None = None,
    ) -> dict[str, int]:
        """Reconcile bounded local escrow; expired open holds refund first."""

        ledger = self._regional_quota_ledger
        if ledger is None:
            return {"inspected": 0, "reconciled": 0, "closed": 0, "errors": 0}
        from trusted_router.services.regional_quota_leases import HoldState
        from trusted_router.storage_gcp_regional_quota import (
            GlobalRegionalQuotaLease,
            OpenRegionalQuotaLease,
            close_expired_uninitialized_regional_quota_lease,
            delete_closed_regional_quota_open_index,
            reconcile_regional_quota_lease,
        )

        now = dt.datetime.now(dt.UTC) if now is None else now
        bounded_limit = max(1, min(limit, 1000))
        open_leases = self._list_entities(
            "regional_quota_lease_open",
            cls=OpenRegionalQuotaLease,
            limit=bounded_limit,
        )
        result = {"inspected": 0, "reconciled": 0, "closed": 0, "errors": 0}
        for open_lease in open_leases:
            result["inspected"] += 1
            try:
                record = self._read_entity(
                    "regional_quota_lease",
                    open_lease.lease_entity_id,
                    GlobalRegionalQuotaLease,
                )
                if record is None:
                    raise RuntimeError("indexed global regional lease is missing")
                if record.state == "closed":
                    if not delete_closed_regional_quota_open_index(
                        self,
                        record,
                        open_lease,
                    ):
                        raise RuntimeError("closed regional lease index cleanup lost its fence")
                    result["closed"] += 1
                    continue
                local = ledger.get(record.lease_id, region=record.region)
                if local is None:
                    # Granting is intentionally two-phase: Spanner first
                    # reserves the bounded escrow and publishes a pending
                    # lease, then the regional ledger row is initialized and
                    # the global lease becomes active. The reconciler can
                    # observe that short, valid gap. Leave live pending leases
                    # alone so the initializer can finish; only expired or
                    # quarantined missing rows belong to recovery.
                    if record.state == "pending" and record.expires_datetime > now:
                        continue
                    closed = close_expired_uninitialized_regional_quota_lease(
                        self,
                        record,
                        now=now,
                    )
                    result["reconciled"] += 1
                    if closed.closed:
                        with self._regional_quota_lease_cache_lock:
                            self._regional_quota_lease_cache.pop(
                                (
                                    record.workspace_id,
                                    record.region,
                                    record.quota_shard,
                                ),
                                None,
                            )
                        result["closed"] += 1
                    continue
                for hold in local.holds:
                    if (
                        hold.state == HoldState.RESERVED
                        and hold.expires_at is not None
                        and hold.expires_at <= now
                    ):
                        local = ledger.refund(
                            record.lease_id,
                            region=record.region,
                            hold_id=hold.hold_id,
                            fencing_token=record.fencing_token,
                        )
                if local.expires_at <= now and local.state.value == "active":
                    local = ledger.begin_drain(
                        record.lease_id,
                        region=record.region,
                        fencing_token=record.fencing_token,
                    )
                should_close = local.state.value == "draining" and local.reserved_microdollars == 0
                reconcile_regional_quota_lease(
                    self,
                    record,
                    local,
                    close=should_close,
                    now=now,
                )
                result["reconciled"] += 1
                if should_close:
                    ledger.close(
                        record.lease_id,
                        region=record.region,
                        fencing_token=record.fencing_token,
                    )
                    with self._regional_quota_lease_cache_lock:
                        self._regional_quota_lease_cache.pop(
                            (
                                record.workspace_id,
                                record.region,
                                record.quota_shard,
                            ),
                            None,
                        )
                    result["closed"] += 1
            except Exception:
                result["errors"] += 1
                log.error(
                    "regional quota reconciliation failed lease_id=%s workspace_id=%s",
                    open_lease.lease_id,
                    open_lease.workspace_id,
                    exc_info=True,
                )
        return result

    def acquire_regional_quota_reconciler_lock(
        self,
        *,
        owner: str,
        ttl_seconds: int,
        now: dt.datetime | None = None,
    ) -> Any | None:
        from trusted_router.storage_gcp_regional_quota import (
            acquire_regional_quota_reconciler_lock,
        )

        return acquire_regional_quota_reconciler_lock(
            self,
            owner=owner,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def release_regional_quota_reconciler_lock(
        self,
        *,
        owner: str,
        fencing_token: int,
        now: dt.datetime | None = None,
    ) -> bool:
        from trusted_router.storage_gcp_regional_quota import (
            release_regional_quota_reconciler_lock,
        )

        return release_regional_quota_reconciler_lock(
            self,
            owner=owner,
            fencing_token=fencing_token,
            now=now,
        )

    def verify_regional_quota_ledger(self) -> tuple[str, ...]:
        """Prove conditional writes and reads through every fixed app profile."""

        ledger = self._regional_quota_ledger
        if ledger is None:
            raise RuntimeError("regional quota ledger is disabled")
        return ledger.health_check()

    def verify_spend_lease_ledger(self) -> tuple[str, ...]:
        """Prove conditional writes and reads through every fixed app profile."""

        ledger = self._spend_lease_ledger
        if ledger is None:
            raise RuntimeError("spend lease ledger is disabled")
        return ledger.health_check()

    def acquire_spend_lease_reconciler_lock(
        self,
        *,
        owner: str,
        ttl_seconds: int,
        now: dt.datetime | None = None,
    ) -> Any | None:
        from trusted_router.storage_gcp_spend_lease_reconcile import (
            acquire_spend_lease_reconciler_lock,
        )

        return acquire_spend_lease_reconciler_lock(
            self, owner=owner, ttl_seconds=ttl_seconds, now=now
        )

    def release_spend_lease_reconciler_lock(
        self,
        *,
        owner: str,
        fencing_token: int,
        now: dt.datetime | None = None,
    ) -> bool:
        from trusted_router.storage_gcp_spend_lease_reconcile import (
            release_spend_lease_reconciler_lock,
        )

        return release_spend_lease_reconciler_lock(
            self, owner=owner, fencing_token=fencing_token, now=now
        )

    def reconcile_spend_leases(
        self,
        *,
        limit: int = 25,
        max_attempts: int = 12,
        now: dt.datetime | None = None,
    ) -> dict[str, int | float]:
        from trusted_router.storage_gcp_spend_lease_reconcile import (
            reconcile_spend_leases,
        )

        return reconcile_spend_leases(
            self, limit=limit, max_attempts=max_attempts, now=now
        )

    def requeue_dead_spend_leases(
        self,
        *,
        lease_ids: tuple[str, ...] = (),
        limit: int = 1000,
    ) -> int:
        from trusted_router.storage_gcp_spend_lease_reconcile import (
            requeue_dead_spend_leases,
        )

        return requeue_dead_spend_leases(self, lease_ids=lease_ids, limit=limit)

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
        app_id: str = "",
        app_markup_basis_points: int = 0,
        receipt_fee_basis_points: int = 0,
        app_owner_user_id: str = "",
        key_usage_shards: int = 1,
        tags: dict[str, str] | None = None,
        custom_model_id: str | None = None,
        custom_model_revision: int | None = None,
        custom_model_markup_basis_points: int = 0,
        custom_model_owner_user_id: str = "",
        user_provided_model_id: str | None = None,
        user_provided_model_revision: int | None = None,
        user_model_prompt_price_microdollars_per_m: int | None = None,
        user_model_completion_price_microdollars_per_m: int | None = None,
        user_model_owner_user_id: str | None = None,
        additional_cost_reservation_microdollars: int = 0,
        native_batch_eligible: bool = False,
        expires_at: Any = None,
        window_limits: dict[str, int] | None = None,
        spend_lease: SpendLeaseArtifact | None = None,
        spend_lease_binding_plan: Any = None,
        pricing_snapshot: str | None = None,
        stage_d_reason: str | None = None,
        stage_d_prompt_tokens: int | None = None,
        stage_d_max_output_tokens: int | None = None,
        spend_lease_admission_receipt: str | None = None,
        spend_lease_receipt_hash: str | None = None,
        credit_escrowed_by_spend_lease: bool = False,
        spend_lease_admission_replay_protection: bool = False,
        stage_d_boot_kid: str | None = None,
        invocation_nonce: str | None = None,
    ) -> tuple[str, GatewayAuthorization | None]:
        """Route-facing typed authorize. Runs the atomic conditional-DML authorize
        (holds + reservation + gateway_authorization DML-insert) and returns
        (outcome, authorization). outcome in accepted/replay/insufficient_credits/
        key_limit_exceeded/key_missing/idempotency_mismatch, or
        "key_window_limit_exceeded:<daily|weekly|monthly>" when a per-window cap
        blocked (see authorize_atomic's window_limits contract)."""
        from trusted_router.storage_gcp_authorize import (
            AuthorizeOutcome,
            authorize_atomic,
            bounded_credit_shard_candidates,
        )
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

        built_authorizations: dict[str, GatewayAuthorization] = {}
        base_authorizations: dict[str, GatewayAuthorization] = {}

        def build_authorization(
            authorization_id: str,
            reservation_id: str,
        ) -> GatewayAuthorization:
            existing = built_authorizations.get(authorization_id)
            if existing is not None:
                if existing.credit_reservation_id != reservation_id:
                    # A cold-path retry can race an already-committed attempt
                    # after the route's pre-transaction idempotency probe was
                    # removed. Do not let the fresh reservation reach the hold
                    # DML with an authorization object tied to the old hold.
                    # The typed signal below rebuilds once, then the atomic
                    # transaction's first read returns the stored winner.
                    raise _AuthorizationReplay(authorization_id)
                return existing
            built = GatewayAuthorization(
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
                app_id=app_id,
                app_markup_basis_points=app_markup_basis_points,
                receipt_fee_basis_points=receipt_fee_basis_points,
                app_owner_user_id=app_owner_user_id,
                custom_model_id=custom_model_id,
                custom_model_revision=custom_model_revision,
                custom_model_markup_basis_points=custom_model_markup_basis_points,
                custom_model_owner_user_id=custom_model_owner_user_id,
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
                spend_lease_token=spend_lease.token if spend_lease else None,
                spend_lease_id=spend_lease.lease_id if spend_lease else None,
                spend_lease_cap_micro=spend_lease.cap_micro if spend_lease else None,
                spend_lease_gen=spend_lease.gen if spend_lease else None,
                spend_lease_iat=spend_lease.iat if spend_lease else None,
                spend_lease_exp=spend_lease.exp if spend_lease else None,
                spend_lease_issuer_kid=spend_lease.issuer_kid if spend_lease else None,
                spend_lease_boot_kid=spend_lease.boot_kid if spend_lease else None,
                spend_lease_catalog_version=(spend_lease.catalog_version if spend_lease else None),
                spend_lease_status=spend_lease.lease_status if spend_lease else None,
                heartbeat_seq=0 if pricing_snapshot is not None else None,
                pricing_snapshot=pricing_snapshot,
                stage_d_reason=stage_d_reason,
                stage_d_prompt_tokens=stage_d_prompt_tokens,
                stage_d_max_output_tokens=stage_d_max_output_tokens,
                spend_lease_admission_receipt=spend_lease_admission_receipt,
                spend_lease_receipt_hash=spend_lease_receipt_hash,
                stage_d_boot_kid=stage_d_boot_kid,
                invocation_nonce=invocation_nonce,
            )
            built_authorizations[authorization_id] = built
            base_authorizations[authorization_id] = built
            return built

        def build_authorization_for_lease(
            authorization_id: str,
            reservation_id: str,
            bound: bool,
        ) -> GatewayAuthorization:
            base = base_authorizations.get(authorization_id)
            if base is None:
                base = build_authorization(authorization_id, reservation_id)
            if not bound:
                selected = dataclasses.replace(
                    base,
                    spend_lease_token=None,
                    spend_lease_id=None,
                    spend_lease_cap_micro=None,
                    spend_lease_gen=None,
                    spend_lease_iat=None,
                    spend_lease_exp=None,
                    spend_lease_issuer_kid=None,
                    spend_lease_boot_kid=None,
                    spend_lease_catalog_version=None,
                    spend_lease_status=None,
                    spend_lease_allocated_micro=None,
                )
            else:
                artifact = spend_lease_binding_plan.artifact
                selected = dataclasses.replace(
                    base,
                    settlement="spend_lease",
                    spend_lease_token=artifact.token,
                    spend_lease_id=artifact.lease_id,
                    spend_lease_cap_micro=artifact.cap_micro,
                    spend_lease_gen=artifact.gen,
                    spend_lease_iat=artifact.iat,
                    spend_lease_exp=artifact.exp,
                    spend_lease_issuer_kid=artifact.issuer_kid,
                    spend_lease_boot_kid=artifact.boot_kid,
                    spend_lease_catalog_version=artifact.catalog_version,
                    spend_lease_status=artifact.lease_status,
                    spend_lease_allocated_micro=spend_lease_binding_plan.allocation_micro,
                    spend_lease_admission_receipt=spend_lease_admission_receipt,
                    spend_lease_receipt_hash=spend_lease_receipt_hash,
                )
            built_authorizations[authorization_id] = selected
            return selected

        def build_body(authorization_id: str, reservation_id: str) -> str:
            return _json_body(build_authorization(authorization_id, reservation_id))

        window_decision = None
        if window_limits:
            # Lock-free snapshot check BEFORE the DML-only transaction (keeps
            # the authorize txn free of shared reads on the hot row — the
            # deadlock shape the typed migration removed). Replay-safe: an
            # existing same-fingerprint reservation passes through to the txn.
            from trusted_router.storage_gcp_authorize import (
                AuthorizeVerdict,
                key_window_limit_decision,
            )

            window_decision = key_window_limit_decision(
                self._database,
                self._param_types,
                key_hash=key_hash,
                estimate=estimate,
                window_limits=window_limits,
                shard_count=key_counter_shards,
                idempotency_scope=scope,
                idempotency_fingerprint=idempotency_fingerprint,
            )
            if window_decision is not None and not window_decision.allowed:
                # WHICH window rides as an outcome suffix so the
                # (outcome, authorization) tuple shape stays unchanged; the
                # gateway route splits on ':'.
                return (
                    AuthorizeVerdict(
                        f"{AuthorizeOutcome.KEY_WINDOW_LIMIT_EXCEEDED}:{window_decision.window}",
                        rate_limit=window_decision,
                    ),
                    None,
                )

        credit_shard_candidates = (
            self._credit_shard_candidates(workspace_id) if has_credit_candidate else (UNSHARDED,)
        )

        def run_authorize(candidates: tuple[int, ...]) -> dict[str, Any]:
            def invoke(*, use_binding: bool = True) -> dict[str, Any]:
                spend_hook = None
                retry_types: tuple[type[BaseException], ...] = ()
                if spend_lease_binding_plan is not None and use_binding:
                    from trusted_router.spend_lease_authorize import (
                        SpendLeaseArbitrationConflict,
                    )

                    def spend_hook(transaction: Any, shard: int) -> dict[str, Any]:
                        return spend_lease_binding_plan.transaction_hook(
                            transaction,
                            self._param_types,
                            workspace_id,
                            shard,
                        )
                    retry_types = (SpendLeaseArbitrationConflict,)
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
                    # Retain `candidates` in full outside this transaction for the
                    # lock-free aggregate check and cold rebalance below. A depleted
                    # workspace must never make one rejected transaction lock every
                    # configured shard.
                    credit_shard_candidates=bounded_credit_shard_candidates(candidates),
                    key_shard_candidates=key_shard_candidates,
                    authorization_id=authorization_id,
                    spend_lease_hook=spend_hook,
                    build_authorization_for_lease=(
                        build_authorization_for_lease
                        if spend_lease_binding_plan is not None and use_binding
                        else None
                    ),
                    also_retry=retry_types,
                    spend_lease_receipt_hash=spend_lease_receipt_hash,
                    credit_escrowed_by_spend_lease=credit_escrowed_by_spend_lease,
                    spend_lease_admission_replay_protection=(
                        spend_lease_admission_replay_protection
                    ),
                )

            try:
                return invoke()
            except _AuthorizationReplay as replay:
                # authorize_atomic builds its response object before opening
                # the transaction. Drop that stale object and enter once more:
                # the transaction's first operation is the scoped idempotency
                # read, so a winner replays before any new key/credit hold DML.
                built_authorizations.pop(replay.authorization_id, None)
                return invoke()
            except Exception as exc:
                if spend_lease_binding_plan is None:
                    raise
                from trusted_router.storage_gcp_spend_lease_authorize import (
                    SpendLeaseMintLost,
                    SpendLeaseReuseLost,
                )

                if isinstance(exc, SpendLeaseMintLost):
                    built_authorizations.pop(
                        spend_lease_binding_plan.provisional_id, None
                    )
                    base_authorizations.pop(
                        spend_lease_binding_plan.provisional_id, None
                    )
                    result = invoke(use_binding=False)
                    result.update(
                        bound=False,
                        no_lease_reason="mint_lost",
                        spend_lease_outcome="mint_lost",
                    )
                    return result
                if isinstance(exc, SpendLeaseReuseLost):
                    if spend_lease_receipt_hash is not None:
                        return {
                            "outcome": "admission_rejected:reuse_lost",
                            "bound": False,
                        }
                    built_authorizations.pop(
                        spend_lease_binding_plan.provisional_id, None
                    )
                    base_authorizations.pop(
                        spend_lease_binding_plan.provisional_id, None
                    )
                    result = invoke(use_binding=False)
                    result.update(
                        bound=False,
                        no_lease_reason=exc.reason,
                        spend_lease_outcome="ordinary",
                        compensate_reuse=True,
                    )
                    return result
                from trusted_router.spend_lease_authorize import (
                    SpendLeaseArbitrationConflict,
                )
                from trusted_router.storage_errors import StoreConflict

                if isinstance(exc, SpendLeaseArbitrationConflict):
                    raise StoreConflict(
                        "spend-lease scope arbitration remained contended"
                    ) from exc
                raise

        result = run_authorize(credit_shard_candidates)
        if result["outcome"] == AuthorizeOutcome.KEY_LIMIT_EXCEEDED and key_counter_shards > 1:
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

            # A bounded write-set rejection is cold. Refresh once so a remote
            # pause/drain split or unshard cannot produce a false 402 until the
            # normal TTL expires. Requests accepted inside the bounded prefix
            # never pay this read; a later funded shard needs one snapshot.
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
            aggregate_exhaustion_proven = False
            for _attempt in range(3):
                if (
                    result["outcome"] != AuthorizeOutcome.INSUFFICIENT_CREDITS
                    or len(credit_shard_candidates) <= 1
                ):
                    break
                precheck = rebalance_mod.credit_headroom_precheck(
                    self._database,
                    self._param_types,
                    workspace_id=workspace_id,
                    shard_count=len(credit_shard_candidates),
                    shard_candidates=credit_shard_candidates,
                    estimate=estimate,
                )
                verdict = precheck.outcome
                if verdict == rebalance_mod.RebalanceOutcome.INSUFFICIENT:
                    aggregate_exhaustion_proven = True
                    break
                if verdict == rebalance_mod.RebalanceOutcome.NOT_NEEDED:
                    if precheck.candidate_shard is None:  # pragma: no cover - invariant
                        raise RuntimeError("credit headroom precheck omitted its candidate")
                    # The bounded random prefix missed a funded later shard.
                    # Retry that exact row instead of either returning a false
                    # 402 or expanding into an all-shard write transaction.
                    result = run_authorize((precheck.candidate_shard,))
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
                        # Aggregate funds exist but are genuinely fragmented.
                        # The cooldown protects Spanner from a rebalance
                        # stampede; reporting 402 here would be an accounting
                        # lie, so ask the caller to retry instead.
                        raise StoreUnavailable("credit escrow rebalance is busy; retry")
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
                if rebalance_result["outcome"] == rebalance_mod.RebalanceOutcome.INSUFFICIENT:
                    # This verdict comes from a locked read of every configured
                    # shard and is at least as authoritative as the snapshot
                    # precheck. Preserve the honest 402 instead of turning a
                    # concurrent final-credit race into a spurious 503.
                    aggregate_exhaustion_proven = True
                break
            if (
                result["outcome"] == AuthorizeOutcome.INSUFFICIENT_CREDITS
                and len(credit_shard_candidates) > 1
                and not aggregate_exhaustion_proven
            ):
                # A bounded retry race or incomplete precheck is not proof that
                # the customer's aggregate balance is exhausted. Preserve the
                # accounting contract by returning retryable unavailability;
                # only the explicit read-only aggregate verdict above may 402.
                raise StoreUnavailable("credit headroom changed concurrently; retry")
        outcome = result["outcome"]
        authorization: GatewayAuthorization | None = None
        if outcome == AuthorizeOutcome.ACCEPTED:
            # authorize_atomic stamps one client timestamp onto both the object
            # and the inserted typed row/payload. No commit-generated response
            # field exists, so the just-inserted object is byte-for-byte the
            # response record and a strong post-commit point read adds no truth.
            authorization = built_authorizations[result["authorization_id"]]
        elif outcome == AuthorizeOutcome.REPLAY:
            # A replay must respond from the winner's stored authorization, not
            # this call's provisional object.
            authorization = self.get_gateway_authorization(result["authorization_id"])
        from trusted_router.storage_gcp_authorize import AuthorizeVerdict

        verdict = AuthorizeVerdict(
            outcome,
            rate_limit=window_decision,
            spend_lease_bound=bool(result.get("bound")),
            no_lease_reason=result.get("no_lease_reason"),
            spend_lease_outcome=result.get("spend_lease_outcome"),
        )
        if outcome == AuthorizeOutcome.REPLAY and authorization is not None:
            verdict.spend_lease_bound = bool(
                authorization.spend_lease_token
                and authorization.spend_lease_id
                and authorization.spend_lease_gen
                and authorization.spend_lease_allocated_micro
            )
        if spend_lease_binding_plan is not None and outcome == AuthorizeOutcome.ACCEPTED:
            if verdict.spend_lease_bound:
                spend_lease_binding_plan.bind_after_commit()
            elif result.get("compensate_reuse") or (
                result.get("spend_lease_outcome")
                in {
                    "escrow_refused",
                    "scope_claimed",
                    "fence_lost_race",
                    "fence_stale_advisory",
                    "fence_count_exhausted",
                    "fence_window_open",
                }
            ):
                spend_lease_binding_plan.compensate_with_claim(
                    self._database,
                    self._param_types,
                )
        return verdict, authorization

    def prepare_gateway_spend_lease_binding(
        self,
        *,
        workspace_id: str,
        key_hash: str,
        authorization_id: str,
        idempotency_key: str | None,
        idempotency_fingerprint: str,
        estimate: int,
        boot_kid: str,
        region: str,
        signer: Any,
        catalog: dict[str, Any],
        ttl_seconds: int,
        skew_seconds: int,
        max_microdollars: int,
        max_available_basis_points: int,
        echo_lease_id: str | None,
        echo_state: str | None,
        local_admission_allowed: bool = False,
        routing_policy_hash: str | None = None,
        trust_eligibility_enabled: bool = False,
    ) -> tuple[Any | None, str | None]:
        """Decision 31/46 preparation, called only behind the binding flag."""
        from trusted_router.spend_lease_ledger import SpendLeaseLedgerError
        from trusted_router.spend_lease_state import (
            Created,
            ExistingLocal,
            SpendLeaseUnavailableError,
            is_authoritative_exhaustion,
        )
        from trusted_router.storage_gcp_keys import (
            _gateway_authorization_idempotency_index_id,
        )
        from trusted_router.storage_gcp_spend_lease_authorize import (
            BindingPlan,
            prepare_candidate,
            reservation_exists,
        )

        if idempotency_key is None:
            return None, "no_idempotency_key"
        scope = _gateway_authorization_idempotency_index_id(
            workspace_id, key_hash, idempotency_key
        )
        if reservation_exists(self._database, self._param_types, scope):
            return None, None
        expected_trust_tier: int | None = None
        if trust_eligibility_enabled:
            trust_snapshot = self.typed_credit_trust_snapshot(workspace_id)
            if trust_snapshot is None:
                return None, "ledger_unavailable"
            expected_trust_tier, trust_latched_at = trust_snapshot
            if expected_trust_tier < 1 or trust_latched_at is not None:
                return None, "unpaid_workspace"
        ledger = self._spend_lease_ledger
        if ledger is None or not ledger.supports_region(region):
            return None, "ledger_unavailable"
        now = dt.datetime.now(dt.UTC)
        active = self.get_active_spend_lease(key_hash, boot_kid)
        if (
            active is not None
            and echo_lease_id is not None
            and echo_lease_id != active.lease_id
        ):
            return None, "lease_transferred"
        authoritative_exhaustion = bool(
            active is not None
            and echo_lease_id == active.lease_id
            and echo_state in {"exhausted", "terminal"}
        )
        window_closed = active is None or now >= dt.datetime.fromtimestamp(
            active.exp + skew_seconds, tz=dt.UTC
        )
        if active is not None and not window_closed and not authoritative_exhaustion:
            try:
                result = ledger.allocate(
                    None,
                    active.lease_id,
                    region=region,
                    idempotency_scope=scope,
                    provisional_authorization_id=authorization_id,
                    request_fingerprint=idempotency_fingerprint,
                    allocated_micro=estimate,
                    abandon_after=dt.datetime.fromtimestamp(
                        active.exp + skew_seconds, tz=dt.UTC
                    ),
                    now=now,
                )
            except SpendLeaseLedgerError:
                global_lease = self._read_entity(
                    "spend_lease", active.lease_id, dict
                )
                if global_lease is None:
                    log.error(
                        "spend_lease.local_missing_without_global_record",
                        extra={"workspace_id": workspace_id, "region": region},
                    )
                    return None, "ledger_unavailable"
                raw_global_exp = global_lease.get("expires_at")
                if isinstance(raw_global_exp, str):
                    try:
                        global_deadline = dt.datetime.fromisoformat(
                            raw_global_exp.replace("Z", "+00:00")
                        )
                    except ValueError:
                        return None, "ledger_unavailable"
                else:
                    global_deadline = dt.datetime.fromtimestamp(
                        int(global_lease.get("exp", 0)), tz=dt.UTC
                    )
                global_skew = int(global_lease.get("skew_seconds", skew_seconds))
                global_state = str(global_lease.get("state") or "")
                if now >= global_deadline + dt.timedelta(seconds=global_skew):
                    window_closed = True
                    authoritative_exhaustion = True
                elif global_state in {"CLOSED", "TOMBSTONED"}:
                    window_closed = True
                    authoritative_exhaustion = True
                else:
                    log.error(
                        "spend_lease.local_global_inconsistency",
                        extra={"workspace_id": workspace_id, "region": region},
                    )
                    return None, "ledger_unavailable"
            except SpendLeaseUnavailableError as exc:
                authoritative_exhaustion = is_authoritative_exhaustion(exc)
                window_closed = exc.reason.value in {
                    "frozen_draining", "frozen_tombstoned", "closed", "window_expired"
                }
                if not window_closed and not authoritative_exhaustion:
                    return None, "window_open"
            else:
                if isinstance(result, ExistingLocal):
                    return None, None
                if isinstance(result, Created):
                    return (
                        BindingPlan(
                            ledger=ledger,
                            scope=scope,
                            fence_id=self._spend_lease_pair_id(key_hash, boot_kid),
                            region=region,
                            provisional_id=authorization_id,
                            artifact=active,
                            allocation_micro=estimate,
                            admission_deadline=dt.datetime.fromtimestamp(
                                active.exp + skew_seconds, tz=dt.UTC
                            ),
                            mode="reuse",
                            candidate=None,
                            observed_gen=active.gen,
                            incumbent_lease_id=active.lease_id,
                            incumbent_window_closed=False,
                            authoritative_exhaustion=False,
                            trust_eligibility_enabled=trust_eligibility_enabled,
                            expected_trust_tier=expected_trust_tier,
                        ),
                        None,
                    )
                return None, None

        snapshot = self.typed_credit_snapshot(workspace_id)
        if snapshot is None:
            return None, "ledger_unavailable"
        available = max(0, int(snapshot[0]) - int(snapshot[1]) - int(snapshot[2]))
        cap_micro = min(
            max_microdollars,
            max(0, available - estimate) * max_available_basis_points // 10_000,
        )
        if cap_micro <= 0:
            return None, "escrow_headroom"
        observed_gen = active.gen if active is not None else 0
        if active is None:
            from trusted_router.storage_gcp_spend_lease_authorize import ensure_initial_fence

            ensure_initial_fence(
                self._database,
                self._param_types,
                self._spend_lease_pair_id(key_hash, boot_kid),
            )
        return (
            prepare_candidate(
                database=self._database,
                param_types=self._param_types,
                ledger=ledger,
                signer=signer,
                scope=scope,
                fence_id=self._spend_lease_pair_id(key_hash, boot_kid),
                provisional_id=authorization_id,
                workspace_id=workspace_id,
                key_hash=key_hash,
                boot_kid=boot_kid,
                region=region,
                gen=observed_gen + 1,
                cap_micro=cap_micro,
                allocation_micro=estimate,
                ttl_seconds=ttl_seconds,
                skew_seconds=skew_seconds,
                request_fingerprint=idempotency_fingerprint,
                catalog=catalog,
                observed_gen=observed_gen,
                observed_predecessor_count=(
                    active.open_predecessor_count if active is not None else 0
                ),
                incumbent_lease_id=active.lease_id if active is not None else None,
                incumbent_window_closed=window_closed,
                authoritative_exhaustion=authoritative_exhaustion,
                local_admission_allowed=local_admission_allowed,
                routing_policy_hash=routing_policy_hash,
                trust_eligibility_enabled=trust_eligibility_enabled,
                expected_trust_tier=expected_trust_tier,
            ),
            None,
        )

    def get_spend_lease_for_admission(
        self,
        lease_id: str,
        workspace_id: str,
        key_hash: str,
    ) -> SpendLeaseArtifact | None:
        """Return the immutable Stage C lease token while ACTIVE or DRAINING."""

        payload = self._read_entity("spend_lease", lease_id, dict)
        if payload is None or payload.get("state") not in {"ACTIVE", "DRAINING"}:
            return None
        required = {
            "token",
            "lease_id",
            "cap_micro",
            "gen",
            "iat",
            "exp",
            "issuer_kid",
            "boot_kid",
            "key_hash",
            "workspace_id",
            "catalog_version",
            "routing_policy_hash",
            "catalog",
        }
        if (
            not required.issubset(payload)
            or payload.get("local_admission_allowed") is not True
            or payload.get("workspace_id") != workspace_id
            or payload.get("key_hash") != key_hash
        ):
            return None
        try:
            return SpendLeaseArtifact(
                token=str(payload["token"]),
                lease_id=str(payload["lease_id"]),
                cap_micro=int(payload["cap_micro"]),
                gen=int(payload["gen"]),
                iat=int(payload["iat"]),
                exp=int(payload["exp"]),
                issuer_kid=str(payload["issuer_kid"]),
                boot_kid=str(payload["boot_kid"]),
                catalog_version=str(payload["catalog_version"]),
                lease_status=cast(Any, str(payload["state"]).lower()),
                local_admission_allowed=True,
                routing_policy_hash=str(payload["routing_policy_hash"]),
                catalog=cast(FrozenSpendLeaseCatalog, dict(payload["catalog"])),
            )
        except (TypeError, ValueError):
            return None

    def prepare_gateway_spend_lease_admission(
        self,
        *,
        artifact: SpendLeaseArtifact,
        workspace_id: str,
        key_hash: str,
        authorization_id: str,
        idempotency_key: str,
        idempotency_fingerprint: str,
        estimate: int,
        region: str,
        receipt_hash: str,
        skew_seconds: int,
        trust_eligibility_enabled: bool = False,
    ) -> tuple[Any | None, str | None, GatewayAuthorization | None]:
        """Prepare only direct reuse of the exact presented Stage C lease."""

        from trusted_router.spend_lease_admission import classify_receipt_replay
        from trusted_router.spend_lease_ledger import SpendLeaseLedgerError
        from trusted_router.spend_lease_state import (
            Created,
            ExistingLocal,
            SpendLeaseExhaustedError,
            SpendLeaseUnavailableError,
        )
        from trusted_router.storage_gcp_counter_dml import read_reservation_by_idempotency
        from trusted_router.storage_gcp_keys import _gateway_authorization_idempotency_index_id
        from trusted_router.storage_gcp_request_records import (
            read_gateway_authorization,
            read_gateway_authorization_admission_columns,
        )
        from trusted_router.storage_gcp_spend_lease_authorize import BindingPlan

        scope = _gateway_authorization_idempotency_index_id(
            workspace_id,
            key_hash,
            idempotency_key,
        )
        # Replay site one: authorization id and receipt hash share one strong
        # snapshot. This prevents a local allocation before replay truth is known.
        with self._database.snapshot(multi_use=True) as snapshot:
            existing = read_reservation_by_idempotency(snapshot, self._param_types, scope)
            if existing is not None:
                existing_id = str(existing["authorization_id"])
                admission = read_gateway_authorization_admission_columns(
                    snapshot,
                    self._param_types,
                    existing_id,
                )
                stored_hash = (
                    str(admission["spend_lease_receipt_hash"])
                    if admission is not None
                    and admission["spend_lease_receipt_hash"] is not None
                    else None
                )
                replay = classify_receipt_replay(receipt_hash, stored_hash)
                if replay != "replay":
                    return None, "scope_conflict", None
                authorization = read_gateway_authorization(
                    snapshot,
                    self._param_types,
                    existing_id,
                )
                return None, None, authorization

        expected_trust_tier: int | None = None
        if trust_eligibility_enabled:
            trust_snapshot = self.typed_credit_trust_snapshot(workspace_id)
            if trust_snapshot is None:
                return None, "reuse_lost", None
            expected_trust_tier, trust_latched_at = trust_snapshot
            if expected_trust_tier < 1 or trust_latched_at is not None:
                return None, "hold_refused", None
        ledger = self._spend_lease_ledger
        if ledger is None or not ledger.supports_region(region):
            return None, "reuse_lost", None
        now = dt.datetime.now(dt.UTC)
        try:
            result = ledger.allocate(
                None,
                artifact.lease_id,
                region=region,
                idempotency_scope=scope,
                provisional_authorization_id=authorization_id,
                request_fingerprint=idempotency_fingerprint,
                allocated_micro=estimate,
                abandon_after=dt.datetime.fromtimestamp(
                    artifact.exp + skew_seconds,
                    tz=dt.UTC,
                ),
                now=now,
                admission_receipt=True,
            )
        except SpendLeaseExhaustedError:
            return None, "capacity", None
        except SpendLeaseUnavailableError:
            return None, "lease_not_open", None
        except SpendLeaseLedgerError:
            return None, "reuse_lost", None
        if isinstance(result, ExistingLocal):
            return None, "reuse_lost", None
        if not isinstance(result, Created):
            return None, "scope_conflict", None
        return (
            BindingPlan(
                ledger=ledger,
                scope=scope,
                fence_id=self._spend_lease_pair_id(key_hash, artifact.boot_kid),
                region=region,
                provisional_id=authorization_id,
                artifact=artifact,
                allocation_micro=estimate,
                admission_deadline=dt.datetime.fromtimestamp(
                    artifact.exp + skew_seconds,
                    tz=dt.UTC,
                ),
                mode="reuse",
                candidate=None,
                observed_gen=artifact.gen,
                incumbent_lease_id=artifact.lease_id,
                incumbent_window_closed=False,
                authoritative_exhaustion=False,
                remaining_micro=result.lease.available_micro,
                trust_eligibility_enabled=trust_eligibility_enabled,
                expected_trust_tier=expected_trust_tier,
            ),
            None,
            None,
        )

    def spend_lease_remaining_micro(self, lease_id: str, region: str) -> int | None:
        ledger = self._spend_lease_ledger
        if ledger is None or not ledger.supports_region(region):
            return None
        lease = ledger.get(lease_id, region=region)
        return lease.available_micro if lease is not None else None

    def reap_expired_reservations(self, *, now: Any, limit: int = 100) -> int:
        from trusted_router.storage_gcp_authorize import (
            reap_expired_reservations as _reap,
        )

        return _reap(self._database, self._param_types, now=now, limit=limit)

    def reap_expired_reservations_result(
        self,
        *,
        now: Any,
        limit: int = 100,
        snapshot_booking_enabled: bool = False,
    ) -> Any:
        from trusted_router.storage_gcp_authorize import (
            reap_expired_reservations_result as _reap_result,
        )

        return _reap_result(
            self._database,
            self._param_types,
            now=now,
            limit=limit,
            snapshot_booking_enabled=snapshot_booking_enabled,
            operational_analytics_outbox=getattr(
                self,
                "_operational_analytics_outbox",
                None,
            ),
        )

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

    def typed_credit_trust_snapshot(
        self, workspace_id: str
    ) -> tuple[int, dt.datetime | None] | None:
        account = self.get_credit_account(workspace_id)
        if account is None:
            return None
        expected = credit_shard_count(account)
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT shard, trust_tier, trust_latched_at FROM tr_credit_balance "
                    "WHERE workspace_id=@pk ORDER BY shard",
                    params={"pk": workspace_id},
                    param_types={"pk": self._param_types.STRING},
                )
            )
        if [int(row[0]) for row in rows] != list(range(expected)):
            return None
        values = {(int(row[1] or 0), row[2]) for row in rows}
        if len(values) != 1:
            return None
        return values.pop()

    def list_trust_tier_workspace_ids(self) -> tuple[str, ...]:
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                "SELECT DISTINCT workspace_id FROM tr_credit_balance ORDER BY workspace_id"
            )
            return tuple(str(row[0]) for row in rows)

    def recompute_workspace_trust_tier(
        self,
        workspace_id: str,
        *,
        qualifying_providers: frozenset[str],
        tier3_min_days: int,
        tier3_min_paid_microdollars: int,
        now: dt.datetime,
    ) -> int:
        return recompute_workspace_trust_tier_tx(
            run_in_transaction=self._run_in_transaction,
            param_types=self._param_types,
            read_entity_tx=self._read_entity_tx,
            workspace_id=workspace_id,
            qualifying_providers=qualifying_providers,
            tier3_min_days=tier3_min_days,
            tier3_min_paid_microdollars=tier3_min_paid_microdollars,
            now=now,
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
    ) -> KeyLimitReserveResult:
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

    def record_spend_lease_shadow(self, event_id: str, payload: dict[str, Any]) -> None:
        outbox = self._operational_analytics_outbox
        if outbox is None:
            log.warning("spanner.spend_lease_shadow_outbox_disabled_drop")
            return
        outbox.enqueue_spend_lease_shadow(event_id, payload)

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

    def provider_route_benchmark_samples(
        self,
        *,
        cutoff: str,
        per_route_limit: int,
        limit: int,
    ) -> list[ProviderBenchmarkSample]:
        # This is one derived monitoring sweep, not a customer read. Exact
        # parity is owned by the background verifier; synchronously shadowing
        # every route to Bigtable recreated the N x 2 fanout this method exists
        # to remove.
        return self._require_operational_analytics().route_benchmark_samples(
            cutoff=cutoff,
            per_route_limit=per_route_limit,
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
        from trusted_router.operational_analytics_direct import (
            DirectOperationalAnalyticsSink,
        )

        if isinstance(outbox, DirectOperationalAnalyticsSink):
            oldest, stats = outbox.freshness_snapshot()
            seconds_since_last_delivery = (
                None
                if stats.last_success_unix <= 0
                else max(0.0, time.time() - stats.last_success_unix)
            )
            return OutboxFreshness(
                backend=BACKEND_DIRECT,
                oldest_enqueued_at=oldest,
                seconds_since_last_delivery=seconds_since_last_delivery,
                dropped_total=stats.dropped,
                flush_failures=stats.flush_failures,
            )
        try:
            oldest = outbox.oldest_enqueued_at(timeout=OUTBOX_FRESHNESS_TIMEOUT_SECONDS)
        except Exception as exc:
            context = {
                "error_class": type(exc).__name__,
                "error_message": str(exc)[:500],
                "retryable": is_transient_store_error(exc),
            }
            if context["retryable"]:
                # This read is advisory and fail-closed: /status.json publishes
                # `unreachable`, while the fleet freshness checker owns paging
                # after a sustained failure. A cold regional Spanner session can
                # exceed this deliberately short budget without breaking a
                # customer request, so do not manufacture two Error Reporting
                # groups from gRPC's chained timeout traceback.
                log.warning(
                    "spanner.operational_analytics_outbox_freshness_degraded",
                    extra=context,
                )
            else:
                log.exception(
                    "spanner.operational_analytics_outbox_freshness_failed",
                    extra=context,
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

    def observe_receipt_key(self, record: ReceiptKey) -> ReceiptKeyWriteOutcome:
        def txn(transaction: Any) -> ReceiptKeyWriteOutcome:
            existing = self._read_entity_tx(transaction, RECEIPT_KEY_KIND, record.kid, ReceiptKey)
            merged, outcome = merge_receipt_key_observation(existing, record)
            if merged is not None and outcome in {"appended", "refreshed"}:
                self._write_entity_tx(transaction, RECEIPT_KEY_KIND, record.kid, merged)
            return outcome

        return cast(ReceiptKeyWriteOutcome, self._run_in_transaction(txn))

    def list_receipt_keys(self, *, limit: int = 5_000) -> list[ReceiptKey]:
        return self._list_entities(
            RECEIPT_KEY_KIND,
            cls=ReceiptKey,
            limit=max(0, min(limit, 10_000)),
        )

    def observe_spend_lease_boot(self, record: SpendLeaseBoot) -> SpendLeaseBoot:
        def txn(transaction: Any) -> SpendLeaseBoot:
            existing = self._read_entity_tx(
                transaction, SPEND_LEASE_BOOT_KIND, record.kid, SpendLeaseBoot
            )
            if existing is not None and (
                existing.jwk != record.jwk
                or existing.image_digest != record.image_digest
                or existing.attestation_kind != record.attestation_kind
            ):
                raise ValueError("spend-lease boot kid collision")
            merged = record
            if existing is not None:
                merged = dataclasses.replace(
                    existing,
                    approved=existing.approved or record.approved,
                    verified=existing.verified or record.verified,
                    image_digest=record.image_digest or existing.image_digest,
                )
            self._write_entity_tx(transaction, SPEND_LEASE_BOOT_KIND, record.kid, merged)
            return merged

        return cast(SpendLeaseBoot, self._run_in_transaction(txn))

    def get_spend_lease_boot(self, kid: str) -> SpendLeaseBoot | None:
        return self._read_entity(SPEND_LEASE_BOOT_KIND, kid, SpendLeaseBoot)

    def advance_stage_d_policy_watermark(
        self,
        *,
        plane: str,
        sequence: int,
        updated_at: dt.datetime,
    ) -> bool:
        from trusted_router.storage_gcp_stage_d_policy import (
            advance_stage_d_policy_watermark,
        )

        return advance_stage_d_policy_watermark(
            self._database,
            self._param_types,
            plane=plane,
            sequence=sequence,
            updated_at=updated_at,
        )

    def get_stage_d_policy_watermark(self, *, plane: str) -> int | None:
        from trusted_router.storage_gcp_stage_d_policy import (
            get_stage_d_policy_watermark,
        )

        return get_stage_d_policy_watermark(
            self._database,
            self._param_types,
            plane=plane,
        )

    def next_spend_lease_generation(self, key_hash: str, boot_kid: str) -> int:
        entity_id = hashlib.sha256(f"{key_hash}\0{boot_kid}".encode()).hexdigest()

        def txn(transaction: Any) -> int:
            existing = self._read_entity_tx(
                transaction, SPEND_LEASE_GENERATION_KIND, entity_id, dict
            )
            generation = int((existing or {}).get("generation", 0)) + 1
            self._write_entity_tx(
                transaction,
                SPEND_LEASE_GENERATION_KIND,
                entity_id,
                {"key_hash": key_hash, "boot_kid": boot_kid, "generation": generation},
            )
            return generation

        return int(self._run_in_transaction(txn))

    @staticmethod
    def _spend_lease_pair_id(key_hash: str, boot_kid: str) -> str:
        return hashlib.sha256(f"{key_hash}\0{boot_kid}".encode()).hexdigest()

    def get_active_spend_lease(self, key_hash: str, boot_kid: str) -> SpendLeaseArtifact | None:
        payload = self._read_entity(
            SPEND_LEASE_ACTIVE_GRANT_KIND,
            self._spend_lease_pair_id(key_hash, boot_kid),
            dict,
        )
        if payload is None or not payload.get("token"):
            return None
        known = {field.name for field in dataclasses.fields(SpendLeaseArtifact)}
        return SpendLeaseArtifact(
            **{key: value for key, value in payload.items() if key in known}
        )

    def retain_spend_lease(
        self,
        key_hash: str,
        boot_kid: str,
        candidate: SpendLeaseArtifact,
        *,
        replace: bool,
    ) -> SpendLeaseArtifact:
        entity_id = self._spend_lease_pair_id(key_hash, boot_kid)

        def txn(transaction: Any) -> SpendLeaseArtifact:
            existing = self._read_entity_tx(
                transaction,
                SPEND_LEASE_ACTIVE_GRANT_KIND,
                entity_id,
                SpendLeaseArtifact,
            )
            if existing is None or (replace and candidate.gen > existing.gen):
                self._write_entity_tx(
                    transaction,
                    SPEND_LEASE_ACTIVE_GRANT_KIND,
                    entity_id,
                    candidate,
                )
                return candidate
            return existing

        return cast(SpendLeaseArtifact, self._run_in_transaction(txn))

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
