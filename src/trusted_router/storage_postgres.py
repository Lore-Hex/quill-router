"""Postgres implementation of the portable Store contract, increment 1.

The generic ``tr_entities`` table mirrors the Spanner adapter's entity model.
Money remains in typed tables and is changed only with conditional DML.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Never, TypeVar, cast

import psycopg
from psycopg_pool import ConnectionPool

from trusted_router import credit_transfer
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
    BACKEND_POSTGRES,
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
    OutboxFreshness,
)
from trusted_router.postgres_dsn import (
    aws_dsql_connection_details,
    dsql_token_is_admin,
)
from trusted_router.security import (
    hash_api_key,
    key_label,
    lookup_hash_api_key,
    new_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.spend_windows import KeyWindowLimitExceeded, window_floors
from trusted_router.storage_auth_context import build_session_auth_context
from trusted_router.storage_codec import json_body
from trusted_router.storage_errors import (
    DeferredSettlementCapReached,
    StoreConflict,
    StoreUnavailable,
    is_duplicate_key_error,
)
from trusted_router.storage_gcp_codec import (
    byok_id,
    member_id,
    normalize_email,
    workspace_key_id,
)
from trusted_router.storage_gcp_counters import credit_shard_count, distribute_credit_amount
from trusted_router.storage_models import (
    FUTURE_SAMPLE_SKEW_SECONDS,
    AcquisitionAttribution,
    ActivationReminderTask,
    ApiKey,
    ApiKeyAuthContext,
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
    UserProvidedModel,
    VerificationToken,
    VideoJob,
    WalletChallenge,
    Workspace,
    _is_byok,
    _is_expired,
    federated_api_key_from_record,
    federated_workspace_from_record,
    iso_now,
    normalize_provider_access_role,
    normalize_provider_access_slug,
    utcnow,
)
from trusted_router.storage_postgres_group_buy import PostgresBedrockGroupBuy
from trusted_router.storage_postgres_operational_analytics_outbox import (
    PostgresOperationalAnalyticsOutbox,
)
from trusted_router.storage_video_jobs import (
    VIDEO_CONTENT_RETENTION_SECONDS,
    VIDEO_SUBMISSION_TIMEOUT_SECONDS,
    _is_due,
    _iso_after_seconds,
)
from trusted_router.synthetic.rollups import (
    RAW_SYNTHETIC_RETENTION_DAYS,
    ROLLUP_RETENTION_MONTHS,
    apply_sample_to_rollup,
    new_rollup_for_sample,
    sample_rollup_ids,
)
from trusted_router.types import IdentityVerificationStatus, UsageType

T = TypeVar("T")

log = logging.getLogger(__name__)

_AWS_DSQL_IAM_AUTH = "aws-dsql"

# SQLSTATEs meaning "the server rolled your transaction back whole; replaying
# it is the correct response". _run_transaction retries exactly these.
#
# Keyed on the SQLSTATE string rather than on a psycopg exception class, and
# that is the entire point. psycopg generates ONE FLAT CLASS PER SQLSTATE, each
# deriving straight from its DB-API base, so `SerializationFailure` (40001) and
# `DeadlockDetected` (40P01) are SIBLINGS of `TransactionRollback` (40000), not
# subclasses of it. `except psycopg.errors.TransactionRollback` therefore
# caught only a bare 40000 -- which no server in this fleet emits -- and every
# real abort fell through to the generic handler as a caller-visible
# StoreUnavailable. That is how a routine concurrent reserve came back as
# "Postgres could not service the storage operation" instead of retrying and
# reporting "insufficient credits" (CI run 31996299784). SQLSTATE is the
# DATABASE's contract; psycopg's class layout is not.
#
# The other two class-40 codes are deliberately absent:
#   40002 transaction_integrity_constraint_violation -- deterministic, so a
#         replay fails identically forever.
#   40003 statement_completion_unknown -- the statement may in fact have
#         COMMITTED, so replaying it could double-apply money.
_RETRYABLE_ROLLBACK_SQLSTATES = frozenset(
    {
        "40000",  # transaction_rollback
        "40001",  # serialization_failure -- Spanner ABORTED, DSQL OCC abort
        "40P01",  # deadlock_detected -- ordinary Postgres lock ordering
    }
)


#: Hard cap on the /status.json outbox-lag read, applied to BOTH the pool wait
#: and the statement. Matches `readiness_check`'s 3s, and for the same reason:
#: a public page must degrade rather than wait. It is far above the read's real
#: cost -- an index seek on tr_operational_analytics_outbox_enqueued_at_idx --
#: so hitting it means the database is not answering, which is exactly the
#: state that should publish `unreachable` instead of hanging the event loop.
OUTBOX_FRESHNESS_TIMEOUT_SECONDS = 3.0


class _IamTokenConnectionPool(ConnectionPool):
    """Connection pool that obtains a new password for every connection.

    ``psycopg_pool`` resolves ``self.kwargs`` immediately before opening each
    physical connection. Updating the password here therefore covers initial
    fill, growth, idle replacement, and reconnects without reaching into
    psycopg internals.
    """

    def __init__(
        self,
        *args: Any,
        token_provider: Callable[[], str],
        **kwargs: Any,
    ) -> None:
        self._token_provider = token_provider
        connection_kwargs = kwargs.get("kwargs")
        if connection_kwargs is None:
            kwargs["kwargs"] = {}
        elif not isinstance(connection_kwargs, dict):
            raise TypeError("IAM token pool requires static connection kwargs")
        else:
            kwargs["kwargs"] = dict(connection_kwargs)
        super().__init__(*args, **kwargs)

    def _connect(self, timeout: float | None = None) -> Any:
        if not isinstance(self.kwargs, dict):  # Defensive: fixed in __init__.
            raise TypeError("IAM token pool requires mutable connection kwargs")
        self.kwargs["password"] = self._token_provider()
        return super()._connect(timeout)


def _aws_dsql_connection_details(
    dsn: str,
    *,
    region_override: str = "",
) -> tuple[str, str]:
    # Thin alias kept for callers/tests; the parser itself is shared with the
    # ClickHouse drain so the two cannot disagree about what a DSN means.
    return aws_dsql_connection_details(dsn, region_override=region_override)


def _aws_dsql_token_provider(
    hostname: str,
    region: str,
    *,
    admin: bool = True,
) -> Callable[[], str]:
    # Infrastructure SDK imports stay inside the selected adapter path so
    # ordinary Postgres deployments do not import or initialize boto3.
    import boto3

    client = boto3.client("dsql", region_name=region)
    # DSQL mints a token per role. The control plane IS the system of record and
    # connects as `admin`; anything connecting as a lesser role (the analytics
    # drain) must get the non-admin token or authentication fails.
    mint = (
        client.generate_db_connect_admin_auth_token
        if admin
        else client.generate_db_connect_auth_token
    )

    def generate_token() -> str:
        return cast(
            str,
            mint(
                Hostname=hostname,
                Region=region,
                ExpiresIn=900,
            ),
        )

    return generate_token


_GATEWAY_AUTHORIZATION_KIND = "gateway_authorization"
_GATEWAY_IDEMPOTENCY_KIND = "gateway_authorization_idempotency"
_GATEWAY_FINALIZATION_KIND = "gateway_authorization_finalization"
#: GatewayAuthorization.settlement value meaning "this spend is debt owed to
#: the home plane's ledger", not a debit against this plane's balance.
_DEFERRED_HOME_SETTLEMENT = "deferred_home"
_RESERVATION_KIND = "reservation"
_RESERVATION_IDEMPOTENCY_KIND = "reservation_idempotency"
_RESERVATION_FINALIZATION_KIND = "reservation_finalization"
# Cross-plane credit transfer (see trusted_router.credit_transfer).
_CREDIT_TRANSFER_KIND = "credit_transfer"
# Bounded recovery queue: written at escrow, DELETED at resolution, so the
# "which transfers are still unresolved?" scan is a PK-prefix range that
# shrinks to empty in steady state. Same reasoning as the analytics outbox,
# which deletes what it has delivered rather than advancing a cursor over a
# table that grows forever.
_CREDIT_TRANSFER_OPEN_KIND = "credit_transfer_open"
# DESTINATION side. Insert-once; the row IS the verdict and is never rewritten.
_CREDIT_TRANSFER_CLAIM_KIND = "credit_transfer_claim"
# SOURCE side. Insert-once; the row IS the authority to apply a verdict's
# balance change, exactly once. Without it the refund on RETURNED is decided by
# a read-then-write over the transfer row, and two callers that both read
# ESCROWED (an operator cancel racing the recovery pass) both refund — the one
# transition in this design that was not guarded by an insert-once row, and the
# only place the module's own conservation claim was false.
_CREDIT_TRANSFER_RESOLUTION_KIND = "credit_transfer_resolution"

_SESSION_AUTH_CONTEXT_SQL = """
    /* auth_session_context */
    WITH resolved_session AS (
      SELECT
        session_record.body AS session_body,
        session_record.body ->> 'user_id' AS user_id
      FROM tr_entities AS lookup_record
      JOIN tr_entities AS session_record
        ON session_record.kind = 'auth_session'
       AND session_record.id = lookup_record.body ->> 'session_id'
      WHERE lookup_record.kind = 'auth_session_lookup'
        AND lookup_record.id = %s
    )
    SELECT
      resolved.session_body,
      user_record.body,
      workspace_record.body,
      member_record.body
    FROM resolved_session AS resolved
    LEFT JOIN tr_entities AS user_record
      ON user_record.kind = 'user' AND user_record.id = resolved.user_id
    LEFT JOIN tr_entities AS member_record
      ON member_record.kind = 'member'
     AND member_record.body ->> 'user_id' = resolved.user_id
     AND member_record.id = ((member_record.body ->> 'workspace_id') || '#' || resolved.user_id)
    LEFT JOIN tr_entities AS workspace_record
      ON workspace_record.kind = 'workspace'
     AND workspace_record.id = member_record.body ->> 'workspace_id'
    ORDER BY member_record.id
"""

_API_KEY_AUTH_CONTEXT_SQL = """
    /* api_key_auth_context */
    SELECT key_record.body, workspace_record.body
    FROM tr_entities AS lookup_record
    JOIN tr_entities AS key_record
      ON key_record.kind = 'api_key'
     AND key_record.id = lookup_record.body ->> 'key_id'
    LEFT JOIN tr_entities AS workspace_record
      ON workspace_record.kind = 'workspace'
     AND workspace_record.id = key_record.body ->> 'workspace_id'
    WHERE lookup_record.kind = 'api_key_lookup'
      AND lookup_record.id = %s
"""


def _split_sql_statements(schema: str) -> list[str]:
    """Split a schema file into statements, ignoring semicolons in comments.

    Splitting the raw text on ';' looks fine until a `--` comment contains one,
    at which point the comment's tail is handed to the server as a statement and
    fails with a syntax error naming a random English word. That is a genuinely
    confusing way to discover you cannot write prose in your own schema file, so
    strip line comments first.

    Deliberately not a full SQL parser: this file is ours, it has no dollar-quoted
    bodies or string literals containing semicolons, and DSQL forbids the stored
    procedures that would introduce them.
    """
    without_comments = "\n".join(line.split("--", 1)[0] for line in schema.splitlines())
    return [stmt.strip() for stmt in without_comments.split(";") if stmt.strip()]


def _video_due_id(job: VideoJob) -> str:
    """Ordering key for the video due-index: `<next_poll_at>#<job_id>`.

    Lexicographic order over this string is chronological order over
    next_poll_at (ISO-8601, fixed width, Z-suffixed), which is what lets
    `ORDER BY id LIMIT n` in claim_video_jobs walk the queue in due order and
    stop at the first entry that is not yet due.  Matches
    storage_gcp_video_jobs._due_id; the two indexes never share a database, so
    they only have to be internally consistent.
    """
    return f"{job.next_poll_at}#{job.id}"


class PostgresStore:
    """Psycopg-backed Store implementation for the first portability increment."""

    entity_table = "tr_entities"

    def __init__(
        self,
        dsn: str,
        *,
        pool_min_size: int = 0,
        pool_max_size: int = 4,
        transaction_attempts: int = 8,
        postgres_iam_auth: str = "",
        postgres_iam_region: str = "",
        operational_analytics_outbox_enabled: bool = False,
    ) -> None:
        if not dsn:
            raise ValueError("Postgres DSN is required")
        if transaction_attempts < 1:
            raise ValueError("transaction_attempts must be positive")
        self._transaction_attempts = transaction_attempts
        if not postgres_iam_auth:
            self._pool = ConnectionPool(
                conninfo=dsn,
                min_size=pool_min_size,
                max_size=pool_max_size,
                open=True,
            )
        elif postgres_iam_auth == _AWS_DSQL_IAM_AUTH:
            hostname, region = _aws_dsql_connection_details(
                dsn,
                region_override=postgres_iam_region,
            )
            self._pool = _IamTokenConnectionPool(
                conninfo=dsn,
                token_provider=_aws_dsql_token_provider(
                    hostname,
                    region,
                    admin=dsql_token_is_admin(dsn),
                ),
                min_size=pool_min_size,
                max_size=pool_max_size,
                open=True,
            )
        else:
            raise ValueError(
                "Unsupported TR_POSTGRES_IAM_AUTH value "
                f"{postgres_iam_auth!r}; expected 'aws-dsql' or empty"
            )
        # Durable ClickHouse hand-off for tenant activity and synthetic status.
        # Off by default and gated on the same config flag as the Spanner path
        # (`operational_analytics_outbox_enabled`), so enabling delivery is one
        # decision made in one place rather than per-cloud.
        self._operational_analytics_outbox = (
            PostgresOperationalAnalyticsOutbox(self._run_transaction)
            if operational_analytics_outbox_enabled
            else None
        )
        self.bedrock_group_buy_store = PostgresBedrockGroupBuy(
            run_transaction=self._run_transaction,
            read_entity_tx=self._read_entity_tx,
            write_entity_tx=self._write_entity_tx,
            delete_entity_tx=self._delete_entity_tx,
            read_entity=self._read_entity,
            list_entities=self._list_entities,
        )

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()

    def readiness_check(self) -> None:
        """Verify the billing database without permitting an unbounded wait."""

        with self._pool.connection(timeout=3.0) as conn:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout = '3s'")
                conn.execute("SELECT 1 FROM tr_credit_balance LIMIT 1").fetchone()

    def apply_schema(self) -> None:
        """Apply the package-owned schema idempotently.

        DDL runs statement-by-statement in **autocommit**, not inside a
        transaction. Stock Postgres has transactional DDL and would accept
        either, but Spanner's PostgreSQL dialect rejects DDL inside an explicit
        transaction outright ("DDL statements are only allowed outside explicit
        transactions"). Doing it the portable way costs nothing here and is the
        difference between this backend running on all three clouds or only on
        the one it was written against.

        The consequence to know: schema application is therefore NOT atomic.
        A failure part-way leaves earlier statements applied. Every statement is
        `IF NOT EXISTS`, so re-running converges rather than conflicting.
        """
        schema = Path(__file__).with_name("storage_postgres_schema.sql").read_text()
        statements = _split_sql_statements(schema)

        with self._pool.connection() as conn:
            previous_autocommit = conn.autocommit
            conn.autocommit = True
            try:
                for statement in statements:
                    self._execute_ddl(conn, statement)
            finally:
                conn.autocommit = previous_autocommit

    @staticmethod
    def _execute_ddl(conn: Any, statement: str) -> None:
        """Run one DDL statement, tolerating Aurora DSQL's async-index rule.

        DSQL builds indexes asynchronously and rejects a plain `CREATE INDEX`
        with "unsupported mode. please use CREATE INDEX ASYNC." Stock Postgres
        and Spanner's PG dialect both reject the `ASYNC` keyword, so neither
        spelling is portable on its own.

        Rather than branch on a configured dialect — which would mean the DSN
        has to declare what it is, and would be wrong the first time someone
        pointed it somewhere new — try the portable form and fall back only on
        the specific error DSQL raises. The backend then adapts to whatever it
        is actually connected to.

        DSQL's ASYNC build returns immediately and completes in the background.
        That is acceptable here: these indexes serve read paths that are correct
        (just slower) while the index is still building.
        """
        try:
            conn.execute(statement, prepare=False)
            return
        except psycopg.errors.FeatureNotSupported:
            head = statement.lstrip()[:12].upper()
            if not head.startswith("CREATE INDEX"):
                raise
        conn.execute(statement.replace("CREATE INDEX", "CREATE INDEX ASYNC", 1), prepare=False)

    # Generic entity IO ------------------------------------------------------

    def _run_transaction(self, operation: Callable[[Any], T]) -> T:
        last_serialization_error: BaseException | None = None
        for _attempt in range(self._transaction_attempts):
            try:
                with self._pool.connection() as conn:
                    with conn.transaction():
                        return operation(conn)
            except psycopg.Error as exc:
                # Retry on SQLSTATE, NOT on psycopg's exception classes. See
                # _RETRYABLE_ROLLBACK_SQLSTATES: psycopg gives every SQLSTATE
                # its own flat class, so the obvious `except TransactionRollback`
                # silently caught neither 40001 nor 40P01.
                #
                # Safe to retry because the transaction rolled back whole: an
                # idempotency row inserted on the failed attempt is gone, so
                # the retry re-inserts and credits exactly once.
                if exc.sqlstate in _RETRYABLE_ROLLBACK_SQLSTATES:
                    last_serialization_error = exc
                    continue
                if isinstance(exc, psycopg.IntegrityError):
                    raise StoreConflict("Postgres write conflict") from exc
                raise StoreUnavailable("Postgres could not service the storage operation") from exc
        raise StoreConflict(
            "Postgres transaction repeatedly rolled back (serialization failure or deadlock)"
        ) from last_serialization_error

    def _read_entity_tx(
        self,
        conn: Any,
        kind: str,
        entity_id: str,
        cls: type[T],
        *,
        for_update: bool = False,
    ) -> T | None:
        query = "SELECT body FROM tr_entities WHERE kind = %s AND id = %s"
        if for_update:
            query += " FOR UPDATE"
        row = conn.execute(query, (kind, entity_id)).fetchone()
        if row is None:
            return None
        raw = row[0]
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if cls is dict:
            return cast(T, data)
        if dataclasses.is_dataclass(cls):
            known = {field.name for field in dataclasses.fields(cls)}
            data = {key: value for key, value in data.items() if key in known}
        return cls(**data)

    def _read_entity(self, kind: str, entity_id: str, cls: type[T]) -> T | None:
        return self._run_transaction(lambda conn: self._read_entity_tx(conn, kind, entity_id, cls))

    def _list_entities(
        self,
        kind: str,
        cls: type[T],
        *,
        limit: int | None = None,
    ) -> list[T]:
        def operation(conn: Any) -> list[T]:
            query = "SELECT body FROM tr_entities WHERE kind = %s ORDER BY id"
            params: tuple[Any, ...] = (kind,)
            if limit is not None:
                query += " LIMIT %s"
                params = (kind, max(0, limit))
            rows = conn.execute(query, params).fetchall()
            result: list[T] = []
            for row in rows:
                raw = row[0]
                data = json.loads(raw) if isinstance(raw, str) else dict(raw)
                if cls is dict:
                    result.append(cast(T, data))
                    continue
                if dataclasses.is_dataclass(cls):
                    known = {field.name for field in dataclasses.fields(cls)}
                    data = {key: value for key, value in data.items() if key in known}
                result.append(cls(**data))
            return result

        return self._run_transaction(operation)

    @staticmethod
    def _like_prefix(prefix: str) -> str:
        """Turn an id prefix into a LIKE pattern that cannot act as a wildcard.

        Secondary-index ids are `<owner>#<rest>`, and owner ids routinely
        contain `_`.  In LIKE, `_` matches ANY single character, so the
        natural-looking `id LIKE 'ws_abc#%'` also matches `wsXabc#...` — one
        tenant's prefix scan can return another tenant's rows.  Escaping is a
        correctness requirement here, not hygiene.
        """
        escaped = prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        return f"{escaped}%"

    def _write_entity_tx(
        self,
        conn: Any,
        kind: str,
        entity_id: str,
        value: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO tr_entities (kind, id, body, updated_at) "
            "VALUES (%s, %s, %s::jsonb, CURRENT_TIMESTAMP) "
            "ON CONFLICT (kind, id) DO UPDATE "
            "SET body = EXCLUDED.body, updated_at = EXCLUDED.updated_at",
            (kind, entity_id, json_body(value)),
        )

    def _delete_entity_tx(self, conn: Any, kind: str, entity_id: str) -> int:
        cursor = conn.execute(
            "DELETE FROM tr_entities WHERE kind = %s AND id = %s",
            (kind, entity_id),
        )
        return int(cursor.rowcount)

    def _insert_entity_once_tx(
        self,
        conn: Any,
        kind: str,
        entity_id: str,
        value: Any,
    ) -> bool:
        cursor = conn.execute(
            "INSERT INTO tr_entities (kind, id, body, updated_at) "
            "VALUES (%s, %s, %s::jsonb, CURRENT_TIMESTAMP) "
            "ON CONFLICT (kind, id) DO NOTHING",
            (kind, entity_id, json_body(value)),
        )
        return cursor.rowcount == 1

    def _write_indexed_entity_tx(
        self,
        conn: Any,
        kind: str,
        entity_id: str,
        value: Any,
        *,
        indexed_at: str,
        index_date: str | None = None,
        index_target: str | None = None,
        index_probe_type: str | None = None,
        index_monitor_region: str | None = None,
        index_period: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO tr_entities "
            "(kind, id, body, indexed_at, index_date, index_target, "
            "index_probe_type, index_monitor_region, index_period, updated_at) "
            "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, "
            "CURRENT_TIMESTAMP) "
            "ON CONFLICT (kind, id) DO UPDATE SET "
            "body = EXCLUDED.body, "
            "indexed_at = EXCLUDED.indexed_at, "
            "index_date = EXCLUDED.index_date, "
            "index_target = EXCLUDED.index_target, "
            "index_probe_type = EXCLUDED.index_probe_type, "
            "index_monitor_region = EXCLUDED.index_monitor_region, "
            "index_period = EXCLUDED.index_period, "
            "updated_at = EXCLUDED.updated_at",
            (
                kind,
                entity_id,
                json_body(value),
                _parse_timestamp(indexed_at),
                index_date,
                index_target,
                index_probe_type,
                index_monitor_region,
                index_period,
            ),
        )

    def _insert_indexed_entity_once_tx(
        self,
        conn: Any,
        kind: str,
        entity_id: str,
        value: Any,
        *,
        indexed_at: str,
        index_period: str,
    ) -> bool:
        cursor = conn.execute(
            "INSERT INTO tr_entities "
            "(kind, id, body, indexed_at, index_period, updated_at) "
            "VALUES (%s, %s, %s::jsonb, %s, %s, CURRENT_TIMESTAMP) "
            "ON CONFLICT (kind, id) DO NOTHING",
            (
                kind,
                entity_id,
                json_body(value),
                _parse_timestamp(indexed_at),
                index_period,
            ),
        )
        return cursor.rowcount == 1

    def _create_workspace_tx(
        self,
        conn: Any,
        owner_user_id: str,
        name: str,
        trial_credit_microdollars: int | None,
    ) -> Workspace:
        workspace = Workspace(
            id=str(uuid.uuid4()),
            name=name,
            owner_user_id=owner_user_id,
        )
        member = Member(
            workspace_id=workspace.id,
            user_id=owner_user_id,
            role="owner",
        )
        credit = CreditAccount(workspace_id=workspace.id)
        initial_total = 0 if trial_credit_microdollars is None else int(trial_credit_microdollars)
        self._write_entity_tx(conn, "workspace", workspace.id, workspace)
        self._write_entity_tx(
            conn,
            "member",
            member_id(workspace.id, owner_user_id),
            member,
        )
        self._write_entity_tx(conn, "credit", workspace.id, credit)
        conn.execute(
            "INSERT INTO tr_credit_balance "
            "(workspace_id, shard, total_credits, source_updated_at, updated_at) "
            "VALUES (%s, 0, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (workspace.id, initial_total),
        )
        return workspace

    def _consume_secret(
        self,
        *,
        raw_secret: str,
        lookup_kind: str,
        lookup_field: str,
        entity_kind: str,
        cls: type[T],
        expiry_field: str,
        purpose: str | None = None,
    ) -> T | None:
        lookup_hash = lookup_hash_api_key(raw_secret)

        def consume(conn: Any) -> T | None:
            lookup = self._read_entity_tx(
                conn,
                lookup_kind,
                lookup_hash,
                dict,
            )
            if lookup is None:
                return None
            record = self._read_entity_tx(
                conn,
                entity_kind,
                str(lookup[lookup_field]),
                cls,
                for_update=True,
            )
            if record is None or getattr(record, "consumed_at", None) is not None:
                return None
            if purpose is not None and getattr(record, "purpose", None) != purpose:
                return None
            if _is_expired(cast(str | None, getattr(record, expiry_field))):
                return None
            secret_record = cast(Any, record)
            if not verify_api_key(
                raw_secret,
                secret_record.salt,
                secret_record.secret_hash,
            ):
                return None
            secret_record.consumed_at = iso_now()
            self._write_entity_tx(
                conn,
                entity_kind,
                secret_record.hash,
                record,
            )
            return record

        return self._run_transaction(consume)

    @staticmethod
    def _expires_at(ttl_seconds: int) -> str:
        return (
            (utcnow() + dt.timedelta(seconds=max(ttl_seconds, 60)))
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _not_implemented(method: str) -> Never:
        raise NotImplementedError(f"PostgresStore.{method} is not implemented in increment 1")

    # Lifecycle --------------------------------------------------------------

    def reset(self) -> None:
        self._not_implemented("reset")

    # Users + workspaces -----------------------------------------------------

    def ensure_user(
        self,
        user_id: str,
        email: str | None = None,
        *,
        trial_credit_microdollars: int | None = None,
    ) -> User:
        normalized_email = normalize_email(email or user_id)

        def ensure(conn: Any) -> User:
            existing = self._read_entity_tx(
                conn,
                "email_user",
                normalized_email,
                dict,
                for_update=True,
            )
            if existing is not None:
                user = self._read_entity_tx(
                    conn,
                    "user",
                    str(existing["user_id"]),
                    User,
                )
                if user is not None:
                    return user

            user = User(id=str(uuid.uuid4()), email=normalized_email)
            self._write_entity_tx(conn, "user", user.id, user)
            self._write_entity_tx(
                conn,
                "email_user",
                normalized_email,
                {"user_id": user.id},
            )
            self._create_workspace_tx(
                conn,
                user.id,
                "Personal Workspace",
                trial_credit_microdollars,
            )
            return user

        return self._run_transaction(ensure)

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

        def write(conn: Any) -> ProviderAccessGrant:
            if self._read_entity_tx(conn, "user", user_id, User) is None:
                raise ValueError("user does not exist")
            self._write_entity_tx(
                conn,
                "provider_access",
                f"{user_id}#{normalized_provider}",
                grant,
            )
            return grant

        return self._run_transaction(write)

    def list_provider_access_for_user(self, user_id: str) -> list[ProviderAccessGrant]:
        def read(conn: Any) -> list[ProviderAccessGrant]:
            rows = conn.execute(
                "SELECT body FROM tr_entities "
                "WHERE kind = 'provider_access' AND id LIKE %s "
                "ORDER BY id",
                (f"{user_id}#%",),
            ).fetchall()
            return [
                ProviderAccessGrant(
                    **(json.loads(row[0]) if isinstance(row[0], str) else dict(row[0]))
                )
                for row in rows
            ]

        return self._run_transaction(read)

    def revoke_provider_access(self, user_id: str, provider: str) -> bool:
        normalized_provider = normalize_provider_access_slug(provider)
        return bool(
            self._run_transaction(
                lambda conn: self._delete_entity_tx(
                    conn,
                    "provider_access",
                    f"{user_id}#{normalized_provider}",
                )
            )
        )

    def signup(
        self,
        *,
        email: str,
        workspace_name: str | None = None,
        trial_credit_microdollars: int = DEFAULT_SIGNUP_CREDIT_MICRODOLLARS,
    ) -> SignupResult | None:
        self._not_implemented("signup")

    def create_acquisition_attribution(self, record: AcquisitionAttribution) -> bool:
        self._not_implemented("create_acquisition_attribution")

    def get_acquisition_attribution(self, workspace_id: str) -> AcquisitionAttribution | None:
        self._not_implemented("get_acquisition_attribution")

    def claim_acquisition_milestones(
        self,
        workspace_id: str,
        milestones: list[str],
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, list[str]]:
        self._not_implemented("claim_acquisition_milestones")

    def record_acquisition_purchase(
        self,
        workspace_id: str,
        *,
        amount_microdollars: int,
        occurred_at: str,
    ) -> AcquisitionAttribution | None:
        self._not_implemented("record_acquisition_purchase")

    def list_activation_reminders(self, *, limit: int = 100) -> list[ActivationReminderTask]:
        self._not_implemented("list_activation_reminders")

    def delete_activation_reminders(self, reminder_ids: list[str]) -> None:
        self._not_implemented("delete_activation_reminders")

    def claim_activation_reminder(
        self,
        workspace_id: str,
        stage: str,
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, bool]:
        self._not_implemented("claim_activation_reminder")

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

    def create_workspace(
        self,
        owner_user_id: str,
        name: str,
        *,
        trial_credit_microdollars: int | None = None,
    ) -> Workspace:
        return self._run_transaction(
            lambda conn: self._create_workspace_tx(
                conn,
                owner_user_id,
                name,
                trial_credit_microdollars,
            )
        )

    def list_workspaces_for_user(self, user_id: str) -> list[Workspace]:
        self._not_implemented("list_workspaces_for_user")

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
        self._not_implemented("update_workspace")

    def add_members(
        self,
        workspace_id: str,
        emails: list[str],
        role: str = "member",
    ) -> list[Member]:
        self._not_implemented("add_members")

    def remove_members(self, workspace_id: str, user_ids: list[str]) -> None:
        self._not_implemented("remove_members")

    def list_members(self, workspace_id: str) -> list[Member]:
        self._not_implemented("list_members")

    def user_can_manage(self, user_id: str, workspace_id: str) -> bool:
        self._not_implemented("user_can_manage")

    def user_is_member(self, user_id: str, workspace_id: str) -> bool:
        self._not_implemented("user_is_member")

    def get_user(self, user_id: str) -> User | None:
        self._not_implemented("get_user")

    def find_user_by_email(self, email: str) -> User | None:
        self._not_implemented("find_user_by_email")

    def find_user_by_wallet(self, address: str) -> User | None:
        self._not_implemented("find_user_by_wallet")

    def create_wallet_user(self, address: str) -> User:
        self._not_implemented("create_wallet_user")

    def set_user_email(self, user_id: str, email: str) -> User | None:
        self._not_implemented("set_user_email")

    def mark_user_email_verified(self, user_id: str) -> User | None:
        self._not_implemented("mark_user_email_verified")

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
        def txn(conn: Any) -> User | None:
            user = self._read_entity_tx(conn, "user", user_id, User, for_update=True)
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
            self._write_entity_tx(conn, "user", user.id, user)
            return user

        return self._run_transaction(txn)

    def begin_phone_verification(
        self, user_id: str, phone: str, channel: str | None = None
    ) -> tuple[str, User] | None:
        self._not_implemented("begin_phone_verification")

    def confirm_phone_verification(self, user_id: str, code: str) -> tuple[str, User | None]:
        self._not_implemented("confirm_phone_verification")

    def cancel_phone_verification(self, user_id: str) -> User | None:
        self._not_implemented("cancel_phone_verification")

    def clear_user_phone(self, user_id: str) -> User | None:
        self._not_implemented("clear_user_phone")

    # Auth sessions ----------------------------------------------------------

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
        raw = new_api_key(prefix="trsess-v1")
        session = AuthSession(
            hash=new_key_id(prefix="sess"),
            salt=new_hash_salt(),
            secret_hash="",
            lookup_hash=lookup_hash_api_key(raw),
            user_id=user_id,
            provider=provider,
            label=label,
            workspace_id=workspace_id,
            expires_at=self._expires_at(ttl_seconds),
            state=state,
        )
        session.secret_hash = hash_api_key(raw, session.salt)

        def create(conn: Any) -> None:
            self._write_entity_tx(conn, "auth_session", session.hash, session)
            self._write_entity_tx(
                conn,
                "auth_session_lookup",
                session.lookup_hash,
                {"session_id": session.hash},
            )

        self._run_transaction(create)
        return raw, session

    def upgrade_auth_session(self, raw_token: str, *, state: str) -> AuthSession | None:
        self._not_implemented("upgrade_auth_session")

    def set_auth_session_workspace(self, raw_token: str, workspace_id: str) -> AuthSession | None:
        self._not_implemented("set_auth_session_workspace")

    def get_auth_session_by_raw(self, raw_token: str) -> AuthSession | None:
        lookup_hash = lookup_hash_api_key(raw_token)

        def get(conn: Any) -> AuthSession | None:
            lookup = self._read_entity_tx(
                conn,
                "auth_session_lookup",
                lookup_hash,
                dict,
            )
            if lookup is None:
                return None
            session = self._read_entity_tx(
                conn,
                "auth_session",
                str(lookup["session_id"]),
                AuthSession,
            )
            if session is None:
                return None
            if _is_expired(session.expires_at):
                self._delete_entity_tx(conn, "auth_session", session.hash)
                self._delete_entity_tx(conn, "auth_session_lookup", lookup_hash)
                return None
            if verify_api_key(raw_token, session.salt, session.secret_hash):
                return session
            return None

        return self._run_transaction(get)

    def delete_auth_session_by_raw(self, raw_token: str) -> bool:
        lookup_hash = lookup_hash_api_key(raw_token)

        def delete(conn: Any) -> bool:
            lookup = self._read_entity_tx(
                conn,
                "auth_session_lookup",
                lookup_hash,
                dict,
                for_update=True,
            )
            if lookup is None:
                return False
            self._delete_entity_tx(
                conn,
                "auth_session",
                str(lookup["session_id"]),
            )
            return (
                self._delete_entity_tx(
                    conn,
                    "auth_session_lookup",
                    lookup_hash,
                )
                == 1
            )

        return self._run_transaction(delete)

    def session_auth_context(
        self,
        raw_token: str,
        *,
        requested_workspace_id: str | None = None,
    ) -> SessionAuthContext | None:
        """Resolve session principal state in one portable SQL statement."""
        lookup_hash = lookup_hash_api_key(raw_token)

        def resolve(conn: Any) -> SessionAuthContext | None:
            rows = conn.execute(
                _SESSION_AUTH_CONTEXT_SQL,
                (lookup_hash,),
            ).fetchall()
            if not rows:
                return None
            session = _dataclass_from_json(rows[0][0], AuthSession)
            if _is_expired(session.expires_at):
                self._delete_entity_tx(conn, "auth_session", session.hash)
                self._delete_entity_tx(conn, "auth_session_lookup", lookup_hash)
                return None
            if not verify_api_key(raw_token, session.salt, session.secret_hash):
                return None

            user = (
                _dataclass_from_json(rows[0][1], User)
                if rows[0][1] is not None
                else None
            )
            memberships: list[tuple[Member, Workspace]] = []
            for _session_body, _user_body, workspace_body, member_body in rows:
                if workspace_body is None or member_body is None:
                    continue
                member = _dataclass_from_json(member_body, Member)
                workspace = _dataclass_from_json(workspace_body, Workspace)
                memberships.append((member, workspace))
            return build_session_auth_context(
                session=session,
                user=user,
                memberships=memberships,
                requested_workspace_id=requested_workspace_id,
            )

        return self._run_transaction(resolve)

    # Wallet, verification, and OAuth one-shot secrets ----------------------

    def create_wallet_challenge(
        self,
        *,
        address: str,
        message: str,
        ttl_seconds: int,
        raw_nonce: str | None = None,
    ) -> tuple[str, WalletChallenge]:
        raw = raw_nonce or secrets.token_urlsafe(32)
        challenge = WalletChallenge(
            hash=new_key_id(prefix="siwe"),
            salt=new_hash_salt(),
            secret_hash="",
            lookup_hash=lookup_hash_api_key(raw),
            address=address.strip().lower(),
            message=message,
            expires_at=self._expires_at(ttl_seconds),
        )
        challenge.secret_hash = hash_api_key(raw, challenge.salt)

        def create(conn: Any) -> None:
            self._write_entity_tx(
                conn,
                "wallet_challenge",
                challenge.hash,
                challenge,
            )
            self._write_entity_tx(
                conn,
                "wallet_challenge_lookup",
                challenge.lookup_hash,
                {"challenge_id": challenge.hash},
            )

        self._run_transaction(create)
        return raw, challenge

    def consume_wallet_challenge(self, raw_nonce: str) -> WalletChallenge | None:
        return self._consume_secret(
            raw_secret=raw_nonce,
            lookup_kind="wallet_challenge_lookup",
            lookup_field="challenge_id",
            entity_kind="wallet_challenge",
            cls=WalletChallenge,
            expiry_field="expires_at",
        )

    def create_verification_token(
        self,
        *,
        user_id: str,
        purpose: str,
        ttl_seconds: int,
    ) -> tuple[str, VerificationToken]:
        raw = secrets.token_urlsafe(32)
        token = VerificationToken(
            hash=new_key_id(prefix="verify"),
            salt=new_hash_salt(),
            secret_hash="",
            lookup_hash=lookup_hash_api_key(raw),
            user_id=user_id,
            purpose=purpose,
            expires_at=self._expires_at(ttl_seconds),
        )
        token.secret_hash = hash_api_key(raw, token.salt)

        def create(conn: Any) -> None:
            self._write_entity_tx(
                conn,
                "verification_token",
                token.hash,
                token,
            )
            self._write_entity_tx(
                conn,
                "verification_token_lookup",
                token.lookup_hash,
                {"token_id": token.hash},
            )

        self._run_transaction(create)
        return raw, token

    def consume_verification_token(
        self,
        raw_token: str,
        *,
        purpose: str,
    ) -> VerificationToken | None:
        return self._consume_secret(
            raw_secret=raw_token,
            lookup_kind="verification_token_lookup",
            lookup_field="token_id",
            entity_kind="verification_token",
            cls=VerificationToken,
            expiry_field="expires_at",
            purpose=purpose,
        )

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
        raw = new_api_key(prefix="auth_code")
        code = OAuthAuthorizationCode(
            hash=new_key_id(prefix="oauth"),
            salt=new_hash_salt(),
            secret_hash="",
            lookup_hash=lookup_hash_api_key(raw),
            workspace_id=workspace_id,
            user_id=user_id,
            app_id=app_id,
            callback_url=callback_url,
            key_label=key_label,
            limit_microdollars=limit_microdollars,
            limit_reset=limit_reset,
            expires_at=expires_at,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            code_expires_at=self._expires_at(ttl_seconds),
            spawn_agent=spawn_agent,
            spawn_cloud=spawn_cloud,
        )
        code.secret_hash = hash_api_key(raw, code.salt)

        def create(conn: Any) -> None:
            self._write_entity_tx(conn, "oauth_code", code.hash, code)
            self._write_entity_tx(
                conn,
                "oauth_code_lookup",
                code.lookup_hash,
                {"code_id": code.hash},
            )

        self._run_transaction(create)
        return raw, code

    def consume_oauth_authorization_code(self, raw_code: str) -> OAuthAuthorizationCode | None:
        return self._consume_secret(
            raw_secret=raw_code,
            lookup_kind="oauth_code_lookup",
            lookup_field="code_id",
            entity_kind="oauth_code",
            cls=OAuthAuthorizationCode,
            expiry_field="code_expires_at",
        )

    # Email send blocks ------------------------------------------------------

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
        self._not_implemented("block_email_sending")

    def is_email_blocked(self, email: str) -> bool:
        self._not_implemented("is_email_blocked")

    def get_email_block(self, email: str) -> EmailSendBlock | None:
        self._not_implemented("get_email_block")

    def record_sns_message_once(self, message_id: str) -> bool:
        return self._run_transaction(
            lambda conn: self._insert_entity_once_tx(
                conn,
                "sns_message",
                message_id,
                {"created_at": iso_now()},
            )
        )

    def record_webhook_event_once(self, source: str, event_id: str) -> bool:
        return self._run_transaction(
            lambda conn: self._insert_entity_once_tx(
                conn,
                "webhook_event",
                f"{source}#{event_id}",
                {"created_at": iso_now()},
            )
        )

    # API keys ---------------------------------------------------------------

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
        raw = raw_key or new_api_key()
        key = ApiKey(
            hash=new_key_id(),
            salt=new_hash_salt(),
            secret_hash="",
            lookup_hash=lookup_hash_api_key(raw),
            name=name,
            label=key_label(raw),
            workspace_id=workspace_id,
            creator_user_id=creator_user_id,
            management=management,
            limit_microdollars=limit_microdollars,
            limit_reset=limit_reset,
            include_byok_in_limit=include_byok_in_limit,
            expires_at=expires_at,
            limit_daily_microdollars=limit_daily_microdollars,
            limit_weekly_microdollars=limit_weekly_microdollars,
            limit_monthly_microdollars=limit_monthly_microdollars,
            budget_alert_only=budget_alert_only,
            tags=dict(tags or {}),
        )
        key.secret_hash = hash_api_key(raw, key.salt)

        def create(conn: Any) -> None:
            self._write_entity_tx(conn, "api_key", key.hash, key)
            self._write_entity_tx(
                conn,
                "api_key_lookup",
                key.lookup_hash,
                {"key_id": key.hash},
            )
            self._write_entity_tx(
                conn,
                "api_key_by_workspace",
                workspace_key_id(workspace_id, key.hash),
                {"key_id": key.hash},
            )
            conn.execute(
                "INSERT INTO tr_key_limit "
                "(workspace_id, key_hash, shard, limit_micro, include_byok, "
                "day_limit_micro, week_limit_micro, month_limit_micro, "
                "source_updated_at, updated_at) "
                "VALUES (%s, %s, 0, %s, %s, %s, %s, %s, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    workspace_id,
                    key.hash,
                    limit_microdollars,
                    include_byok_in_limit,
                    limit_daily_microdollars,
                    limit_weekly_microdollars,
                    limit_monthly_microdollars,
                ),
            )

        self._run_transaction(create)
        return raw, key

    def get_key_by_hash(self, key_hash: str) -> ApiKey | None:
        return self._read_entity("api_key", key_hash, ApiKey)

    def typed_key_usage(
        self,
        key_hash: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, Any] | None:
        self._not_implemented("typed_key_usage")

    def upsert_federated_api_key(self, record: dict[str, Any]) -> ApiKey:
        """Persist a key record resolved from the home plane.

        Identity only: no salt/secret_hash (a peer never holds home-issued
        key material) and NO credits — a federated key seeds at ZERO local
        balance, because copying a balance mints money. Spending on this
        plane requires an explicit transfer.

        The shadow workspace is written in the SAME transaction as the key.
        A key without its workspace 403s on every request (the authorize path
        reads the workspace before it reads credits), so the two must commit
        together or not at all — a half-written pair would be a permanently
        broken key that looks successfully federated.
        """
        key = federated_api_key_from_record(record)
        workspace = federated_workspace_from_record(record)

        def upsert(conn: Any) -> ApiKey:
            self._materialize_federated_workspace_tx(conn, workspace)
            self._write_entity_tx(conn, "api_key", key.hash, key)
            # The reader (get_key_by_lookup_hash) indexes this by "key_id",
            # matching create_key. Writing "key_hash" here made the FIRST
            # federated request work — _federate_api_key returns the record
            # directly — and every request after it raise KeyError, which is
            # exactly the shape a naive smoke test passes.
            self._write_entity_tx(conn, "api_key_lookup", key.lookup_hash, {"key_id": key.hash})
            # A typed key-limit row is MANDATORY: the typed authorize path
            # fail-closes with KEY_MISSING when a key has a JSON entity but
            # no typed row, which would 402 every federated user. Limits come
            # from the home record; usage and reserved start at zero.
            conn.execute(
                "INSERT INTO tr_key_limit"
                " (workspace_id, key_hash, shard, limit_micro, usage, byok_usage,"
                "  reserved, include_byok, day_limit_micro, week_limit_micro,"
                "  month_limit_micro)"
                " VALUES (%s, %s, 0, %s, 0, 0, 0, %s, %s, %s, %s)"
                " ON CONFLICT (workspace_id, key_hash, shard) DO UPDATE SET"
                "   limit_micro = EXCLUDED.limit_micro"
                " , include_byok = EXCLUDED.include_byok"
                " , day_limit_micro = EXCLUDED.day_limit_micro"
                " , week_limit_micro = EXCLUDED.week_limit_micro"
                " , month_limit_micro = EXCLUDED.month_limit_micro"
                " , updated_at = CURRENT_TIMESTAMP",
                (
                    key.workspace_id,
                    key.hash,
                    key.limit_microdollars,
                    key.include_byok_in_limit,
                    key.limit_daily_microdollars,
                    key.limit_weekly_microdollars,
                    key.limit_monthly_microdollars,
                ),
                prepare=False,
            )
            return key

        return self._run_transaction(upsert)

    def _materialize_federated_workspace_tx(self, conn: Any, workspace: Workspace) -> None:
        """Write (or refresh) the shadow workspace a federated key needs.

        Two guards, both of which protect money or a real tenant:

        * A pre-existing NON-federated workspace with this id is a directory
          COLLISION. Overwriting it would replace a real tenant's workspace
          with an ownerless shadow, so this raises loudly instead. Refusing to
          federate one key is recoverable; silently destroying a workspace is
          not.
        * The credit-balance row is inserted ON CONFLICT DO NOTHING, at ZERO.
          Re-federating a key must never reset a balance that a completed
          credit transfer already funded. An upsert here would silently delete
          transferred money on the next cache miss.
        """
        if not workspace.id:
            raise ValueError("federated record carries no workspace_id")
        existing = self._read_entity_tx(conn, "workspace", workspace.id, Workspace)
        if existing is not None and not existing.federated_home:
            raise StoreConflict(
                f"workspace {workspace.id} exists locally and is not federated; "
                "refusing to overwrite it with a federated shadow"
            )
        self._write_entity_tx(conn, "workspace", workspace.id, workspace)
        conn.execute(
            "INSERT INTO tr_credit_balance "
            "(workspace_id, shard, total_credits, total_usage, reserved, "
            " source_updated_at, updated_at) "
            "VALUES (%s, 0, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT (workspace_id, shard) DO NOTHING",
            (workspace.id,),
            prepare=False,
        )

    def get_key_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None:
        lookup = self._read_entity("api_key_lookup", lookup_hash, dict)
        if lookup is None:
            return None
        return self.get_key_by_hash(str(lookup["key_id"]))

    def get_key_by_raw(self, raw_key: str) -> ApiKey | None:
        key = self.get_key_by_lookup_hash(lookup_hash_api_key(raw_key))
        if key is not None and verify_api_key(
            raw_key,
            key.salt,
            key.secret_hash,
        ):
            return key
        return None

    def api_key_auth_context(self, raw_key: str) -> ApiKeyAuthContext | None:
        """Resolve and verify an API key with its workspace in one query."""
        lookup_hash = lookup_hash_api_key(raw_key)

        def resolve(conn: Any) -> ApiKeyAuthContext | None:
            row = conn.execute(
                _API_KEY_AUTH_CONTEXT_SQL,
                (lookup_hash,),
            ).fetchone()
            if row is None:
                return None
            api_key = _dataclass_from_json(row[0], ApiKey)
            if not verify_api_key(raw_key, api_key.salt, api_key.secret_hash):
                return None
            workspace = (
                _dataclass_from_json(row[1], Workspace)
                if row[1] is not None
                else None
            )
            if workspace is not None and workspace.deleted:
                workspace = None
            return ApiKeyAuthContext(api_key=api_key, workspace=workspace)

        return self._run_transaction(resolve)

    def list_keys(self, workspace_id: str) -> list[ApiKey]:
        self._not_implemented("list_keys")

    def delete_key(self, key_hash: str) -> bool:
        def delete(conn: Any) -> bool:
            key = self._read_entity_tx(
                conn,
                "api_key",
                key_hash,
                ApiKey,
                for_update=True,
            )
            if key is None:
                return False
            self._delete_entity_tx(conn, "api_key", key.hash)
            self._delete_entity_tx(
                conn,
                "api_key_lookup",
                key.lookup_hash,
            )
            self._delete_entity_tx(
                conn,
                "api_key_by_workspace",
                workspace_key_id(key.workspace_id, key.hash),
            )
            conn.execute(
                "DELETE FROM tr_key_limit WHERE workspace_id = %s AND key_hash = %s",
                (key.workspace_id, key.hash),
            )
            return True

        return self._run_transaction(delete)

    def reserve_key_limit(
        self,
        key_hash: str,
        amount_microdollars: int,
        *,
        usage_type: UsageType | str,
    ) -> None:
        """Hold `amount` against this key's caps, or raise.

        Mirrors InMemoryApiKeys.reserve_limit (storage_keys.py), which is the
        semantic reference the conformance suite pins:

          * BYOK spend on a key that excludes BYOK is a NO-OP — lifetime cap
            and windows both.
          * Window limits are checked FIRST and independently of the lifetime
            cap, raising KeyWindowLimitExceeded(window) so the gateway can
            answer 429 with a Retry-After derived from that window's reset.
            In-flight `reserved` is deliberately not counted toward windows,
            matching the typed authorize check.
          * A key with no lifetime limit is uncapped: no-op after windows.
          * Otherwise reserve conditionally; 0 rows updated means the
            predicate failed, i.e. insufficient headroom.

        The conditional UPDATE is a single statement so two concurrent
        reserves cannot both pass the predicate. On DSQL a contended key is a
        hot row and loses the race with a 40001 abort, which _run_transaction
        retries.
        """
        window_floor_map = window_floors(utcnow())

        def reserve(conn: Any) -> None:
            row = conn.execute(
                "SELECT limit_micro, usage, byok_usage, reserved, include_byok,"
                " day_limit_micro, week_limit_micro, month_limit_micro,"
                " day_usage, day_start, week_usage, week_start, month_usage, month_start"
                " FROM tr_key_limit WHERE key_hash = %s AND shard = 0",
                (key_hash,),
                prepare=False,
            ).fetchone()
            if row is None:
                # No typed row: nothing to enforce here. The gateway's own
                # KEY_MISSING handling covers the typed-authorize path.
                return
            (
                limit_micro,
                _usage,
                _byok_usage,
                _reserved,
                include_byok,
                day_limit,
                week_limit,
                month_limit,
                day_usage,
                day_start,
                week_usage,
                week_start,
                month_usage,
                month_start,
            ) = row

            if _is_byok(usage_type) and not include_byok:
                return

            # Lazy windows: a NULL or stale *_start means the window has not
            # started in this period, so its usage reads as ZERO. No reset job
            # exists (or could silently stop and leave keys stuck over limit).
            windows = (
                ("daily", day_limit, day_usage, day_start),
                ("weekly", week_limit, week_usage, week_start),
                ("monthly", month_limit, month_usage, month_start),
            )
            for name, limit, used, started in windows:
                if limit is None:
                    continue
                floor = window_floor_map[name]
                current = 0 if started is None or _as_utc(started) < floor else int(used or 0)
                if current + amount_microdollars > int(limit):
                    raise KeyWindowLimitExceeded(name)

            if limit_micro is None:
                return

            updated = conn.execute(
                "UPDATE tr_key_limit"
                " SET reserved = reserved + %s, updated_at = CURRENT_TIMESTAMP"
                " WHERE key_hash = %s AND shard = 0"
                " AND limit_micro IS NOT NULL"
                " AND limit_micro - usage"
                "     - CASE WHEN include_byok THEN byok_usage ELSE 0 END"
                "     - reserved >= %s",
                (amount_microdollars, key_hash, amount_microdollars),
                prepare=False,
            ).rowcount
            if updated == 0:
                raise ValueError("key limit exceeded")

        self._run_transaction(reserve)

    def settle_key_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        actual_microdollars: int,
        *,
        usage_type: UsageType | str,
    ) -> None:
        """Release the hold and roll the window counters by the ACTUAL cost.

        The lifetime `usage` column is booked by add_generation, not here —
        same split as InMemory, where settle_limit only releases the hold and
        add_usage books the spend. The window counters ARE rolled here because
        nothing else does it, using the same lazy floor as reserve: a window
        whose start is missing or stale restarts at this settlement rather
        than accumulating across periods.
        """
        self._release_key_hold(
            key_hash,
            reserved_microdollars,
            usage_type=usage_type,
            window_amount=max(0, int(actual_microdollars)),
        )

    def refund_key_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: UsageType | str,
    ) -> None:
        """Release the hold without booking any spend (the request failed)."""
        self._release_key_hold(
            key_hash,
            reserved_microdollars,
            usage_type=usage_type,
            window_amount=0,
        )

    # Deferred settlement ---------------------------------------------------

    def _reserve_deferred_outstanding_tx(
        self,
        conn: Any,
        workspace_id: str,
        amount_microdollars: int,
        *,
        cap_microdollars: int,
    ) -> None:
        """Admit `amount` of deferred spend, or raise at the cap.

        ONE conditional UPDATE, rowcount-gated — the same shape as `reserve`
        and `reserve_key_limit`, and for the same reason. A read-then-check
        would bound nothing: authorize-to-settle spans an entire provider
        stream, so N concurrent requests would each read the same stale total
        and each admit, and the cap this feature advertises as its worst-case
        exposure would be advisory. The conditional UPDATE is what makes the
        number true.

        Takes `conn` rather than opening its own transaction: the caller runs
        it inside the authorization insert, so the admission and the record it
        admits commit together or not at all.
        """
        amount = max(0, int(amount_microdollars))
        for _attempt in (1, 2):
            cursor = conn.execute(
                "UPDATE tr_deferred_outstanding SET"
                "   outstanding = outstanding + %s"
                " , updated_at = CURRENT_TIMESTAMP"
                " WHERE workspace_id = %s AND outstanding + %s <= %s",
                (amount, workspace_id, amount, int(cap_microdollars)),
                prepare=False,
            )
            if cursor.rowcount == 1:
                return
            # Either the row does not exist yet (first deferred spend for this
            # workspace) or the cap refused. Seeding is insert-once, so a
            # concurrent seeder does not clobber a live counter; the retry
            # then re-runs the SAME conditional statement, which is what keeps
            # the cap authoritative on the second pass too.
            seeded = conn.execute(
                "INSERT INTO tr_deferred_outstanding"
                "   (workspace_id, outstanding, dead_lettered, updated_at)"
                " VALUES (%s, 0, 0, CURRENT_TIMESTAMP)"
                " ON CONFLICT (workspace_id) DO NOTHING",
                (workspace_id,),
                prepare=False,
            )
            if seeded.rowcount != 1:
                break
        raise DeferredSettlementCapReached(
            f"deferred settlement cap reached for workspace {workspace_id}"
        )

    def _adjust_deferred_outstanding_tx(
        self, conn: Any, workspace_id: str, delta_microdollars: int
    ) -> None:
        """Move the counter by a signed delta, clamped at zero.

        GREATEST is not defensive dressing: settle adjusts by
        (actual - estimate), and an actual smaller than its estimate is the
        common case, so the clamp is what keeps an arithmetic underflow from
        turning into negative debt that silently hands a workspace headroom
        it never earned.
        """
        conn.execute(
            "UPDATE tr_deferred_outstanding SET"
            "   outstanding = GREATEST(outstanding + %s, 0)"
            " , updated_at = CURRENT_TIMESTAMP"
            " WHERE workspace_id = %s",
            (int(delta_microdollars), workspace_id),
            prepare=False,
        )

    def release_deferred_outstanding(self, workspace_id: str, amount_microdollars: int) -> None:
        """Hand back an admitted amount (a cap-refused request unwinding)."""
        self._run_transaction(
            lambda conn: self._adjust_deferred_outstanding_tx(
                conn, workspace_id, -max(0, int(amount_microdollars))
            )
        )

    def pending_home_settlements(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """ELIGIBLE pending debt rows, most-overdue first.

        Eligibility is next_attempt_at <= now: a clamped or outage-bumped row
        waits out its backoff instead of being re-presented every pass while
        eligible rows behind it starve. Ordering by next_attempt_at (not
        enqueued_at) is what lets fresh rows overtake a parked backlog.
        """
        now = iso_now()

        def read(conn: Any) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT authorization_id, workspace_id, cost_microdollars, attempts"
                " FROM tr_home_settlement_outbox"
                " WHERE state = 'pending' AND next_attempt_at <= %s"
                " ORDER BY next_attempt_at"
                " LIMIT %s",
                (now, int(limit)),
                prepare=False,
            ).fetchall()
            return [
                {
                    "authorization_id": str(r[0]),
                    "workspace_id": str(r[1]),
                    "cost_microdollars": int(r[2]),
                    "attempts": int(r[3]),
                }
                for r in rows
            ]

        return self._run_transaction(read)

    def mark_home_settlement_forwarded(self, authorization_id: str) -> bool:
        """pending -> forwarded, decrementing the outstanding counter ONCE.

        The state flip is a conditional UPDATE and the counter decrement rides
        the same transaction, gated on that flip having hit exactly one row —
        so two drainers racing on one row produce one decrement, and the loser
        changes nothing. Returns whether THIS call did the work.
        """

        def mark(conn: Any) -> bool:
            row = conn.execute(
                "SELECT workspace_id, cost_microdollars FROM tr_home_settlement_outbox"
                " WHERE authorization_id = %s AND state = 'pending'",
                (authorization_id,),
                prepare=False,
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                "UPDATE tr_home_settlement_outbox SET"
                "   state = 'forwarded', updated_at = CURRENT_TIMESTAMP"
                " WHERE authorization_id = %s AND state = 'pending'",
                (authorization_id,),
                prepare=False,
            )
            if cursor.rowcount != 1:
                return False
            self._adjust_deferred_outstanding_tx(conn, str(row[0]), -int(row[1]))
            return True

        return self._run_transaction(mark)

    def mark_home_settlement_dead_letter(self, authorization_id: str, *, reason: str) -> bool:
        """pending -> dead_letter, moving the amount out of the serving cap.

        The workspace's headroom is restored — one corruption-class row must
        not brick a workspace forever — while the debt stays VISIBLE in the
        dead_lettered column and in this row's preserved terms. Writing it
        off or replaying it is an explicit operator action, never automatic.
        """

        def mark(conn: Any) -> bool:
            row = conn.execute(
                "SELECT workspace_id, cost_microdollars FROM tr_home_settlement_outbox"
                " WHERE authorization_id = %s AND state = 'pending'",
                (authorization_id,),
                prepare=False,
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                "UPDATE tr_home_settlement_outbox SET"
                "   state = 'dead_letter', last_error = %s,"
                "   updated_at = CURRENT_TIMESTAMP"
                " WHERE authorization_id = %s AND state = 'pending'",
                (reason[:500], authorization_id),
                prepare=False,
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                "UPDATE tr_deferred_outstanding SET"
                "   outstanding = GREATEST(outstanding - %s, 0)"
                " , dead_lettered = dead_lettered + %s"
                " , updated_at = CURRENT_TIMESTAMP"
                " WHERE workspace_id = %s",
                (int(row[1]), int(row[1]), str(row[0])),
                prepare=False,
            )
            return True

        return self._run_transaction(mark)

    def bump_home_settlement_attempt(
        self, authorization_id: str, *, error: str, retry_in_seconds: int = 60
    ) -> None:
        next_attempt = (
            (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=max(1, int(retry_in_seconds))))
            .isoformat()
            .replace("+00:00", "Z")
        )
        self._run_transaction(
            lambda conn: conn.execute(
                "UPDATE tr_home_settlement_outbox SET"
                "   attempts = attempts + 1, last_error = %s,"
                "   next_attempt_at = %s,"
                "   updated_at = CURRENT_TIMESTAMP"
                " WHERE authorization_id = %s AND state = 'pending'",
                (error[:500], next_attempt, authorization_id),
                prepare=False,
            )
        )

    def deferred_outstanding(self, workspace_id: str) -> dict[str, int]:
        """Read the counter. For operators and tests, never for admission —
        admission is the conditional UPDATE above."""

        def read(conn: Any) -> dict[str, int]:
            row = conn.execute(
                "SELECT outstanding, dead_lettered FROM tr_deferred_outstanding"
                " WHERE workspace_id = %s",
                (workspace_id,),
                prepare=False,
            ).fetchone()
            if row is None:
                return {"outstanding": 0, "dead_lettered": 0}
            return {"outstanding": int(row[0]), "dead_lettered": int(row[1])}

        return self._run_transaction(read)

    def _release_key_hold(
        self,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: UsageType | str,
        window_amount: int,
    ) -> None:
        self._run_transaction(
            lambda conn: self._release_key_hold_tx(
                conn,
                key_hash,
                reserved_microdollars,
                usage_type=usage_type,
                window_amount=window_amount,
            )
        )

    def _release_key_hold_tx(
        self,
        conn: Any,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: UsageType | str,
        window_amount: int,
        window_is_byok: bool | None = None,
    ) -> None:
        """Release + window-roll inside a CALLER's transaction.

        finalize_gateway_authorization must do this in the same transaction
        as the credit release, or a crash between them leaves the key hold
        stranded while the money moved.

        `usage_type` is the type the hold was RESERVED under; `window_is_byok`
        is whether the spend being rolled into the windows was BYOK. They
        differ on a mixed-candidate authorization: the hold is Credits-typed
        whenever any credit candidate existed, but the enclave may select a
        BYOK endpoint. Deriving both from one value made the early-return
        below skip the RELEASE for include_byok=false keys — stranding
        `reserved` forever — when what should be skipped is only the window
        contribution.
        """
        floors = window_floors(utcnow())
        row = conn.execute(
            "SELECT limit_micro, include_byok FROM tr_key_limit WHERE key_hash = %s AND shard = 0",
            (key_hash,),
            prepare=False,
        ).fetchone()
        if row is None:
            return
        limit_micro, include_byok = row
        if _is_byok(usage_type) and not include_byok:
            # Symmetric with reserve_key_limit: no hold was ever taken for a
            # BYOK-typed reservation on a key that excludes BYOK, so there is
            # nothing to release and nothing to roll.
            return
        if window_is_byok is None:
            window_is_byok = _is_byok(usage_type)
        if window_is_byok and not include_byok:
            # The hold (Credits-typed) still releases; only the spend's window
            # contribution is excluded, because the key excludes BYOK.
            window_amount = 0
        # GREATEST(...) floors the release at zero so a duplicate settle
        # cannot drive `reserved` negative and hand the key free headroom.
        # Window counters roll in the same statement so a settle is one
        # round trip; each window restarts when its start is NULL or older
        # than the current floor.
        conn.execute(
            "UPDATE tr_key_limit SET"
            "   reserved = GREATEST(reserved - %s, 0)"
            " , day_usage = CASE WHEN day_start IS NULL OR day_start < %s"
            "       THEN %s ELSE COALESCE(day_usage, 0) + %s END"
            " , day_start = CASE WHEN day_start IS NULL OR day_start < %s"
            "       THEN %s ELSE day_start END"
            " , week_usage = CASE WHEN week_start IS NULL OR week_start < %s"
            "       THEN %s ELSE COALESCE(week_usage, 0) + %s END"
            " , week_start = CASE WHEN week_start IS NULL OR week_start < %s"
            "       THEN %s ELSE week_start END"
            " , month_usage = CASE WHEN month_start IS NULL OR month_start < %s"
            "       THEN %s ELSE COALESCE(month_usage, 0) + %s END"
            " , month_start = CASE WHEN month_start IS NULL OR month_start < %s"
            "       THEN %s ELSE month_start END"
            " , updated_at = CURRENT_TIMESTAMP"
            " WHERE key_hash = %s AND shard = 0",
            (
                reserved_microdollars,
                floors["daily"],
                window_amount,
                window_amount,
                floors["daily"],
                floors["daily"],
                floors["weekly"],
                window_amount,
                window_amount,
                floors["weekly"],
                floors["weekly"],
                floors["monthly"],
                window_amount,
                window_amount,
                floors["monthly"],
                floors["monthly"],
                key_hash,
            ),
            prepare=False,
        )
        _ = limit_micro  # read for the BYOK/uncapped guard above only.

    def update_key(
        self,
        key_hash: str,
        patch: dict[str, Any],
    ) -> ApiKey | None:
        self._not_implemented("update_key")

    # BYOK -------------------------------------------------------------------

    def upsert_byok_provider(
        self,
        *,
        workspace_id: str,
        provider: str,
        secret_ref: str,
        key_hint: str | None,
        encrypted_secret: EncryptedSecretEnvelope | None = None,
    ) -> ByokProviderConfig:
        self._not_implemented("upsert_byok_provider")

    def list_byok_providers(self, workspace_id: str) -> list[ByokProviderConfig]:
        self._not_implemented("list_byok_providers")

    def get_byok_provider(
        self,
        workspace_id: str,
        provider: str,
    ) -> ByokProviderConfig | None:
        # Gateway-reachable (routes/internal/gateway.py `_get_byok_provider`),
        # so this MUST return rather than raise: a NotImplementedError here
        # 500s the whole authorize.
        #
        # A peer plane has no writer for this kind yet — upsert_byok_provider
        # is still unimplemented — so in practice this misses and the caller
        # drops BYOK endpoints from the candidate set.  That is the safe
        # direction (nothing is billed against a key we could not find), but
        # BYOK models stay unavailable on a peer until these configs are
        # replicated.  Kind and id match the GCP writer (storage_gcp_byok.py)
        # so replication drops straight in.
        return self._read_entity("byok", byok_id(workspace_id, provider), ByokProviderConfig)

    def delete_byok_provider(
        self,
        workspace_id: str,
        provider: str,
    ) -> bool:
        self._not_implemented("delete_byok_provider")

    # Custom models ----------------------------------------------------------

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
        self._not_implemented("create_custom_model")

    def list_custom_models_for_user(self, owner_user_id: str) -> list[CustomModel]:
        self._not_implemented("list_custom_models_for_user")

    def get_custom_model(self, model_id: str) -> CustomModel | None:
        # Gateway-reachable (gateway.py:223 authorize, :707
        # resolve-custom-model).  A miss yields a clean 404 "Custom model not
        # found" instead of a 500.  Both callers already apply
        # normalize_custom_model_id and the GCP writer stores under the
        # normalized id, so this deliberately does not normalize again — a
        # second pass would mask a caller that forgot.
        return self._read_entity("custom_model", model_id, CustomModel)

    def update_custom_model(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> CustomModel | None:
        self._not_implemented("update_custom_model")

    def delete_custom_model(
        self,
        model_id: str,
        *,
        owner_user_id: str,
    ) -> bool:
        self._not_implemented("delete_custom_model")

    # User-provided models ---------------------------------------------------

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
        self._not_implemented("create_user_model")

    def list_user_models_for_user(self, owner_user_id: str) -> list[UserProvidedModel]:
        self._not_implemented("list_user_models_for_user")

    def get_user_model(self, model_id: str) -> UserProvidedModel | None:
        return self._read_entity("user_provided_model", model_id, UserProvidedModel)

    def update_user_model(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> UserProvidedModel:
        self._not_implemented("update_user_model")

    def delete_user_model(self, model_id: str, *, owner_user_id: str) -> bool:
        self._not_implemented("delete_user_model")

    def set_user_model_online(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        online: bool,
    ) -> UserProvidedModel:
        self._not_implemented("set_user_model_online")

    def record_user_model_heartbeat(
        self,
        model_id: str,
        *,
        expires_at: str,
    ) -> UserProvidedModel:
        self._not_implemented("record_user_model_heartbeat")

    def record_user_model_probe(
        self,
        model_id: str,
        *,
        status: str,
        checked_at: str,
    ) -> UserProvidedModel:
        self._not_implemented("record_user_model_probe")

    def record_user_model_dispatch_result(
        self,
        model_id: str,
        *,
        success: bool,
    ) -> UserProvidedModel:
        self._not_implemented("record_user_model_dispatch_result")

    def acquire_user_model_slot(
        self,
        model_id: str,
        authorization_id: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> bool:
        self._not_implemented("acquire_user_model_slot")

    def release_user_model_slot(self, model_id: str, authorization_id: str) -> None:
        self._not_implemented("release_user_model_slot")

    def list_public_user_models(
        self,
        *,
        kind: str | None = None,
    ) -> list[UserProvidedModel]:
        self._not_implemented("list_public_user_models")

    # Broadcast destinations ------------------------------------------------

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
        self._not_implemented("create_broadcast_destination")

    def list_broadcast_destinations(self, workspace_id: str) -> list[BroadcastDestination]:
        # Gateway-reachable at TWO points inside authorize (gateway.py:368 on
        # the idempotent-replay branch, :616 on the main path).  :616 runs
        # AFTER reserve_key_limit / reserve / create_gateway_authorization have
        # committed, so raising here would strand a credit reservation on every
        # single request.  This must return a value, never raise.
        #
        # Written as a real query against the same two kinds the GCP writer
        # uses (storage_gcp_broadcast.py) rather than a bare `return []`, so
        # replicating these rows to a peer needs no change here.  Until then a
        # peer has no rows and gets [].
        def read(conn: Any) -> list[BroadcastDestination]:
            rows = conn.execute(
                "SELECT body FROM tr_entities "
                "WHERE kind = 'broadcast_destination_by_workspace' "
                "AND id LIKE %s ESCAPE '\\' "
                "ORDER BY id",
                (self._like_prefix(f"{workspace_id}#"),),
            ).fetchall()
            destinations: list[BroadcastDestination] = []
            for row in rows:
                raw = row[0]
                pointer = json.loads(raw) if isinstance(raw, str) else dict(raw)
                destination_id = str(pointer.get("destination_id") or "")
                if not destination_id:
                    continue
                destination = self._read_entity_tx(
                    conn, "broadcast_destination", destination_id, BroadcastDestination
                )
                if destination is None:
                    # Index row and entity are two separate writes, so a torn
                    # write leaves a dangling pointer.  Skip it: a half-written
                    # destination must not 500 an authorize that has already
                    # escrowed credits.
                    continue
                if getattr(destination, "workspace_id", workspace_id) != workspace_id:
                    # Defence in depth against a mis-keyed index row — never
                    # hand one workspace another workspace's destination.
                    continue
                destinations.append(destination)
            return destinations

        return self._run_transaction(read)

    def get_broadcast_destination(
        self,
        workspace_id: str,
        destination_id: str,
    ) -> BroadcastDestination | None:
        self._not_implemented("get_broadcast_destination")

    def update_broadcast_destination(
        self,
        workspace_id: str,
        destination_id: str,
        **patch: Any,
    ) -> BroadcastDestination | None:
        self._not_implemented("update_broadcast_destination")

    def delete_broadcast_destination(
        self,
        workspace_id: str,
        destination_id: str,
    ) -> bool:
        self._not_implemented("delete_broadcast_destination")

    def enqueue_broadcast_delivery(
        self,
        *,
        workspace_id: str,
        destination_id: str,
        generation_id: str,
        settle_body: dict[str, Any],
    ) -> BroadcastDeliveryJob:
        self._not_implemented("enqueue_broadcast_delivery")

    def due_broadcast_deliveries(self, *, limit: int = 100) -> list[BroadcastDeliveryJob]:
        self._not_implemented("due_broadcast_deliveries")

    def claim_broadcast_deliveries(
        self,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[BroadcastDeliveryJob]:
        self._not_implemented("claim_broadcast_deliveries")

    def mark_broadcast_delivery(
        self,
        job_id: str,
        *,
        success: bool,
        error: str | None = None,
        lease_owner: str | None = None,
    ) -> BroadcastDeliveryJob | None:
        self._not_implemented("mark_broadcast_delivery")

    # Asynchronous video jobs -----------------------------------------------

    def prepare_video_job(self, job: VideoJob) -> tuple[VideoJob, bool]:
        # Mirrors SpannerVideoJobs.prepare. Idempotent on BOTH keys: an
        # authorization that already has a job returns that job, and so does a
        # replayed job id. Returning (job, False) rather than creating a second
        # row is what stops one authorization submitting twice to a provider.
        def txn(conn: Any) -> tuple[VideoJob, bool]:
            pointer = self._read_entity_tx(
                conn, "video_job_by_authorization", job.authorization_id, dict, for_update=True
            )
            if pointer is not None:
                existing = self._read_entity_tx(
                    conn, "video_job", str(pointer.get("job_id", "")), VideoJob
                )
                if existing is not None:
                    return existing, False
            existing = self._read_entity_tx(conn, "video_job", job.id, VideoJob, for_update=True)
            if existing is not None:
                return existing, False
            job.next_poll_at = _iso_after_seconds(VIDEO_SUBMISSION_TIMEOUT_SECONDS)
            self._write_entity_tx(conn, "video_job", job.id, job)
            self._write_entity_tx(
                conn, "video_job_by_authorization", job.authorization_id, {"job_id": job.id}
            )
            self._write_entity_tx(
                conn,
                "video_job_due",
                _video_due_id(job),
                {"job_id": job.id, "next_poll_at": job.next_poll_at},
            )
            return job, True

        return self._run_transaction(txn)

    def get_video_job(self, job_id: str) -> VideoJob | None:
        return self._read_entity("video_job", job_id, VideoJob)

    def get_video_job_for_key(self, job_id: str, key_hash: str) -> VideoJob | None:
        # The key_hash check is the tenant boundary for the public video
        # endpoints: without it any caller who guesses a job id could read
        # another workspace's job.
        job = self.get_video_job(job_id)
        return job if job is not None and job.key_hash == key_hash else None

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
        def txn(conn: Any) -> VideoJob | None:
            job = self._read_entity_tx(conn, "video_job", job_id, VideoJob, for_update=True)
            if job is None:
                return None
            if job.provider_job_id and job.provider_job_id != provider_job_id:
                raise ValueError("video job was already queued with a different provider id")
            if job.provider_job_id and (
                job.provider != provider
                or job.endpoint_id != endpoint_id
                or job.provider_model != provider_model
                or job.quoted_microdollars != quoted_microdollars
            ):
                raise ValueError("video job was already queued with different route metadata")
            old_due = _video_due_id(job)
            job.provider_job_id = provider_job_id
            job.provider = provider
            job.endpoint_id = endpoint_id
            job.provider_model = provider_model
            job.quoted_microdollars = quoted_microdollars
            if job.status == "submitting":
                job.status = "pending"
            job.next_poll_at = _iso_after_seconds(poll_after_seconds)
            job.updated_at = iso_now()
            self._write_entity_tx(conn, "video_job", job.id, job)
            # Delete BEFORE writing the new pointer: when next_poll_at is
            # unchanged both ids are equal, and deleting second would remove
            # the row we just wrote and drop the job out of the queue forever.
            self._delete_entity_tx(conn, "video_job_due", old_due)
            self._write_entity_tx(
                conn,
                "video_job_due",
                _video_due_id(job),
                {"job_id": job.id, "next_poll_at": job.next_poll_at},
            )
            return job

        return self._run_transaction(txn)

    def claim_video_jobs(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[VideoJob]:
        # Lease claim. Correctness requirement: two pollers must never claim
        # the same job, or the provider is polled twice and a completion can be
        # billed twice. Each candidate is locked FOR UPDATE and re-checked with
        # the shared _is_due predicate INSIDE the transaction, so the loser of a
        # race observes the winner's lease and skips.
        now = iso_now()
        lease_until = _iso_after_seconds(lease_seconds)

        claimed: list[VideoJob] = []
        for pointer in self._list_entities("video_job_due", dict, limit=max(limit * 10, limit)):
            if len(claimed) >= limit:
                break
            # Ids sort as "<next_poll_at>#<job_id>", so the scan is in due
            # order and the first not-yet-due entry ends it.
            if str(pointer.get("next_poll_at", "")) > now:
                break
            candidate_id = str(pointer.get("job_id", ""))
            if not candidate_id:
                continue

            def claim(conn: Any, *, job_id: str = candidate_id) -> VideoJob | None:
                job = self._read_entity_tx(conn, "video_job", job_id, VideoJob, for_update=True)
                if job is None or not _is_due(job, now):
                    return None
                job.lease_owner = lease_owner
                job.leased_until = lease_until
                job.updated_at = now
                self._write_entity_tx(conn, "video_job", job.id, job)
                return job

            job = self._run_transaction(claim)
            if job is not None:
                claimed.append(job)
        return claimed

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
        def txn(conn: Any) -> VideoJob | None:
            job = self._read_entity_tx(conn, "video_job", job_id, VideoJob, for_update=True)
            if job is None:
                return None
            if lease_owner is not None and job.lease_owner not in {None, lease_owner}:
                # Someone else holds the lease; report state, change nothing.
                return job
            if job.status in {"completed", "failed"}:
                # Terminal state is immutable except for repairing a missing
                # generation link after concurrent regional settlement. This is
                # monotonic and leaves polling/cleanup indexes unchanged.
                if (
                    job.status == "completed"
                    and status == "completed"
                    and generation_id
                    and not job.generation_id
                ):
                    job.generation_id = generation_id
                    job.updated_at = iso_now()
                    self._write_entity_tx(conn, "video_job", job.id, job)
                return job
            old_due = _video_due_id(job)
            job.status = status
            job.provider_status = provider_status
            if generation_id:
                job.generation_id = generation_id
            job.last_error = error[:500] if error else None
            job.attempts += 1
            job.lease_owner = None
            job.leased_until = None
            job.updated_at = iso_now()
            if status in {"pending", "in_progress"}:
                job.next_poll_at = _iso_after_seconds(poll_after_seconds)
            elif status == "completed" and job.cleaned_at is None:
                if job.content_expires_at is None:
                    job.content_expires_at = _iso_after_seconds(VIDEO_CONTENT_RETENTION_SECONDS)
                job.next_poll_at = job.content_expires_at
            self._write_entity_tx(conn, "video_job", job.id, job)
            self._delete_entity_tx(conn, "video_job_due", old_due)
            if status in {"pending", "in_progress", "completed"} and job.cleaned_at is None:
                self._write_entity_tx(
                    conn,
                    "video_job_due",
                    _video_due_id(job),
                    {"job_id": job.id, "next_poll_at": job.next_poll_at},
                )
            return job

        return self._run_transaction(txn)

    def mark_video_job_cleaned(self, job_id: str) -> VideoJob | None:
        def txn(conn: Any) -> VideoJob | None:
            job = self._read_entity_tx(conn, "video_job", job_id, VideoJob, for_update=True)
            if job is None:
                return None
            if job.cleaned_at is None:
                old_due = _video_due_id(job)
                job.cleaned_at = iso_now()
                job.updated_at = job.cleaned_at
                job.lease_owner = None
                job.leased_until = None
                self._write_entity_tx(conn, "video_job", job.id, job)
                self._delete_entity_tx(conn, "video_job_due", old_due)
            return job

        return self._run_transaction(txn)

    # Credit ledger ----------------------------------------------------------

    def get_credit_account(self, workspace_id: str) -> CreditAccount | None:
        self._not_implemented("get_credit_account")

    def credit_workspace_typed_direct(
        self,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        lifetime_topup_user_id: str | None = None,
    ) -> bool:
        def credit(conn: Any) -> bool:
            won = self._insert_entity_once_tx(
                conn,
                "stripe_event",
                event_id,
                {
                    "created_at": iso_now(),
                    "workspace_id": workspace_id,
                },
            )
            if not won:
                return False
            self._credit_workspace_balance_tx(
                conn,
                workspace_id,
                int(amount_microdollars),
            )
            if lifetime_topup_user_id is not None:
                conn.execute(
                    "INSERT INTO tr_user_lifetime_topup "
                    "(user_id, total_microdollars, updated_at) "
                    "VALUES (%s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "total_microdollars = "
                    "tr_user_lifetime_topup.total_microdollars + EXCLUDED.total_microdollars, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (lifetime_topup_user_id, int(amount_microdollars)),
                )
            return True

        return self._run_transaction(credit)

    def credit_workspace_once(
        self,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> bool:
        return self.credit_workspace_typed_direct(
            workspace_id,
            amount_microdollars,
            event_id,
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
        conn: Any,
        workspace_id: str,
        amount_microdollars: int,
    ) -> None:
        account = self._read_entity_tx(conn, "credit", workspace_id, CreditAccount)
        if account is None:
            raise ValueError("credit_account_not_found")
        deltas = distribute_credit_amount(
            int(amount_microdollars),
            credit_shard_count(account),
        )
        for shard, delta in enumerate(deltas):
            cursor = conn.execute(
                "UPDATE tr_credit_balance "
                "SET total_credits = total_credits + %s, "
                "source_updated_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE workspace_id = %s AND shard = %s",
                (delta, workspace_id, shard),
            )
            if cursor.rowcount != 1:
                raise ValueError("credit_balance_shard_missing")

    @staticmethod
    def _insert_credit_movement_tx(conn: Any, movement: CreditMovement) -> None:
        conn.execute(
            "INSERT INTO tr_credit_movement "
            "(account_id, movement_id, kind, amount_microdollars, "
            "counterparty_account_id, custom_model_id, authorization_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                movement.account_id,
                movement.movement_id,
                movement.kind,
                movement.amount_microdollars,
                movement.counterparty_account_id,
                movement.custom_model_id,
                movement.authorization_id,
                _parse_timestamp(movement.created_at),
            ),
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

        def debit(conn: Any) -> str:
            won = self._insert_entity_once_tx(
                conn,
                "stripe_event",
                event_id,
                {"created_at": iso_now(), "workspace_id": workspace_id},
            )
            if not won:
                return "duplicate"
            cursor = conn.execute(
                "UPDATE tr_credit_balance "
                "SET total_credits = total_credits - %s, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE workspace_id = %s AND shard = 0 "
                "AND (total_credits - total_usage - reserved) >= %s",
                (amount, workspace_id, amount),
            )
            if cursor.rowcount != 1:
                self._delete_entity_tx(conn, "stripe_event", event_id)
                return "insufficient"
            self._insert_credit_movement_tx(
                conn,
                CreditMovement(
                    account_id=workspace_id,
                    movement_id=event_id,
                    kind=kind,
                    amount_microdollars=-amount,
                    custom_model_id=custom_model_id,
                    authorization_id=authorization_id,
                ),
            )
            return "accepted"

        return self._run_transaction(debit)

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

        def credit(conn: Any) -> bool:
            won = self._insert_entity_once_tx(
                conn,
                "stripe_event",
                event_id,
                {"created_at": iso_now(), "user_id": user_id},
            )
            if not won:
                return False
            conn.execute(
                "INSERT INTO tr_earnings_balance "
                "(user_id, shard, total_earned, total_transferred, updated_at) "
                "VALUES (%s, 0, %s, 0, CURRENT_TIMESTAMP) "
                "ON CONFLICT (user_id, shard) DO UPDATE SET "
                "total_earned = tr_earnings_balance.total_earned + EXCLUDED.total_earned, "
                "updated_at = CURRENT_TIMESTAMP",
                (user_id, amount),
            )
            self._insert_credit_movement_tx(
                conn,
                CreditMovement(
                    account_id=f"user:{user_id}",
                    movement_id=event_id,
                    kind="custom_model_payout",
                    amount_microdollars=amount,
                    counterparty_account_id=payer_workspace_id,
                    custom_model_id=custom_model_id,
                    authorization_id=(
                        user_model_authorization_id_from_payout_event_id(event_id)
                    ),
                ),
            )
            return True

        return self._run_transaction(credit)

    def transfer_earnings_to_workspace(
        self,
        user_id: str,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> str:
        amount = self._positive_money_amount(amount_microdollars)
        user_account_id = f"user:{user_id}"

        def transfer(conn: Any) -> str:
            won = self._insert_entity_once_tx(
                conn,
                "stripe_event",
                event_id,
                {"created_at": iso_now(), "user_id": user_id},
            )
            if not won:
                return "duplicate"
            cursor = conn.execute(
                "UPDATE tr_earnings_balance "
                "SET total_transferred = total_transferred + %s, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = %s AND shard = 0 "
                "AND (total_earned - total_transferred) >= %s",
                (amount, user_id, amount),
            )
            if cursor.rowcount != 1:
                self._delete_entity_tx(conn, "stripe_event", event_id)
                return "insufficient"
            self._credit_workspace_balance_tx(conn, workspace_id, amount)
            created_at = iso_now()
            self._insert_credit_movement_tx(
                conn,
                CreditMovement(
                    account_id=user_account_id,
                    movement_id=event_id,
                    kind="earnings_transfer_out",
                    amount_microdollars=-amount,
                    counterparty_account_id=workspace_id,
                    created_at=created_at,
                ),
            )
            self._insert_credit_movement_tx(
                conn,
                CreditMovement(
                    account_id=workspace_id,
                    movement_id=event_id,
                    kind="earnings_transfer_in",
                    amount_microdollars=amount,
                    counterparty_account_id=user_account_id,
                    created_at=created_at,
                ),
            )
            return "accepted"

        return self._run_transaction(transfer)

    def ensure_earnings_account(self, user_id: str) -> None:
        def ensure(conn: Any) -> None:
            conn.execute(
                "INSERT INTO tr_earnings_balance "
                "(user_id, shard, total_earned, total_transferred, updated_at) "
                "VALUES (%s, 0, 0, 0, CURRENT_TIMESTAMP) "
                "ON CONFLICT (user_id, shard) DO NOTHING",
                (user_id,),
            )

        self._run_transaction(ensure)

    def earnings_summary(
        self,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, int]:
        def read(conn: Any) -> dict[str, int]:
            row = conn.execute(
                "SELECT total_earned, total_transferred "
                "FROM tr_earnings_balance WHERE user_id = %s AND shard = 0",
                (user_id,),
            ).fetchone()
            earned, transferred = (0, 0) if row is None else (int(row[0]), int(row[1]))
            return {
                "total_earned": earned,
                "total_transferred": transferred,
                "available": earned - transferred,
            }

        return self._run_transaction(read)

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
        query = (
            "SELECT account_id, movement_id, kind, amount_microdollars, "
            "counterparty_account_id, custom_model_id, authorization_id, created_at "
            "FROM tr_credit_movement WHERE account_id = %s"
        )
        params: list[Any] = [account_id]
        if kinds is not None:
            query += " AND kind IN (" + ", ".join(["%s"] * len(kinds)) + ")"
            params.extend(kinds)
        if before is not None:
            query += " AND created_at < %s"
            params.append(_parse_timestamp(before))
        query += " ORDER BY created_at DESC, movement_id DESC LIMIT %s"
        params.append(max(0, int(limit)))

        def read(conn: Any) -> list[CreditMovement]:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [
                CreditMovement(
                    account_id=str(row[0]),
                    movement_id=str(row[1]),
                    kind=str(row[2]),
                    amount_microdollars=int(row[3]),
                    counterparty_account_id=(None if row[4] is None else str(row[4])),
                    custom_model_id=None if row[5] is None else str(row[5]),
                    authorization_id=None if row[6] is None else str(row[6]),
                    created_at=_timestamp_string(row[7]),
                )
                for row in rows
            ]

        return self._run_transaction(read)

    def custom_model_earnings_by_model(
        self,
        user_id: str,
        *,
        since: str,
    ) -> dict[str, int]:
        def read(conn: Any) -> dict[str, int]:
            rows = conn.execute(
                "SELECT custom_model_id, SUM(amount_microdollars) "
                "FROM tr_credit_movement "
                "WHERE account_id = %s AND kind = 'custom_model_payout' "
                "AND custom_model_id IS NOT NULL AND created_at >= %s "
                "GROUP BY custom_model_id",
                (f"user:{user_id}", _parse_timestamp(since)),
            ).fetchall()
            return {str(row[0]): int(row[1]) for row in rows}

        return self._run_transaction(read)

    def get_lifetime_topup_microdollars(
        self,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> int:
        def read(conn: Any) -> int:
            row = conn.execute(
                "SELECT total_microdollars FROM tr_user_lifetime_topup WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            return 0 if row is None else int(row[0])

        return self._run_transaction(read)

    def add_lifetime_topup(
        self,
        user_id: str,
        amount_microdollars: int,
        event_id: str,
    ) -> bool:
        amount = self._positive_money_amount(amount_microdollars)

        def add(conn: Any) -> bool:
            won = self._insert_entity_once_tx(
                conn,
                "stripe_event",
                event_id,
                {"created_at": iso_now(), "lifetime_topup_user_id": user_id},
            )
            if not won:
                return False
            conn.execute(
                "INSERT INTO tr_user_lifetime_topup "
                "(user_id, total_microdollars, updated_at) "
                "VALUES (%s, %s, CURRENT_TIMESTAMP) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "total_microdollars = "
                "tr_user_lifetime_topup.total_microdollars + EXCLUDED.total_microdollars, "
                "updated_at = CURRENT_TIMESTAMP",
                (user_id, amount),
            )
            return True

        return self._run_transaction(add)

    def typed_credit_snapshot(self, workspace_id: str) -> tuple[int, int, int] | None:
        def read(conn: Any) -> tuple[int, int, int] | None:
            account = self._read_entity_tx(conn, "credit", workspace_id, CreditAccount)
            if account is None:
                return None
            shard_count = credit_shard_count(account)
            rows = conn.execute(
                "SELECT shard, total_credits, total_usage, reserved "
                "FROM tr_credit_balance WHERE workspace_id = %s "
                "AND shard >= 0 AND shard < %s ORDER BY shard",
                (workspace_id, shard_count),
            ).fetchall()
            if [int(row[0]) for row in rows] != list(range(shard_count)):
                raise ValueError("credit_balance_shard_missing")
            return (
                sum(int(row[1]) for row in rows),
                sum(int(row[2]) for row in rows),
                sum(int(row[3]) for row in rows),
            )

        return self._run_transaction(read)

    def reserve(
        self,
        workspace_id: str,
        key_hash: str,
        amount_microdollars: int,
        *,
        idempotency_key: str | None = None,
    ) -> Reservation:
        reservation = Reservation(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            key_hash=key_hash,
            amount_microdollars=int(amount_microdollars),
            idempotency_key=idempotency_key,
        )

        def reserve_credit(conn: Any) -> Reservation:
            if idempotency_key is not None:
                won = self._insert_entity_once_tx(
                    conn,
                    _RESERVATION_IDEMPOTENCY_KIND,
                    idempotency_key,
                    reservation,
                )
                if not won:
                    existing = self._read_entity_tx(
                        conn,
                        _RESERVATION_IDEMPOTENCY_KIND,
                        idempotency_key,
                        Reservation,
                    )
                    if existing is None:
                        raise RuntimeError("reservation idempotency row disappeared after conflict")
                    return existing

            inserted = self._insert_entity_once_tx(
                conn,
                _RESERVATION_KIND,
                reservation.id,
                reservation,
            )
            if not inserted:
                raise RuntimeError("reservation id collision")

            cursor = conn.execute(
                "UPDATE tr_credit_balance "
                "SET reserved = reserved + %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE workspace_id = %s AND shard = 0 "
                "AND total_credits - total_usage - reserved >= %s",
                (
                    reservation.amount_microdollars,
                    workspace_id,
                    reservation.amount_microdollars,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("insufficient credits")
            return reservation

        return self._run_transaction(reserve_credit)

    def settle(
        self,
        reservation_id: str,
        actual_microdollars: int,
    ) -> None:
        self._finalize_reservation(
            reservation_id,
            actual_microdollars=int(actual_microdollars),
            operation="settle",
        )

    def refund(self, reservation_id: str) -> None:
        self._finalize_reservation(
            reservation_id,
            actual_microdollars=0,
            operation="refund",
        )

    def _finalize_reservation(
        self,
        reservation_id: str,
        *,
        actual_microdollars: int,
        operation: str,
    ) -> None:
        def finalize(conn: Any) -> None:
            reservation = self._read_entity_tx(
                conn,
                _RESERVATION_KIND,
                reservation_id,
                Reservation,
            )
            if reservation is None:
                raise KeyError(reservation_id)

            won = self._insert_entity_once_tx(
                conn,
                _RESERVATION_FINALIZATION_KIND,
                reservation_id,
                {
                    "actual_microdollars": actual_microdollars,
                    "operation": operation,
                },
            )
            if not won:
                return

            cursor = conn.execute(
                "UPDATE tr_credit_balance "
                "SET reserved = reserved - %s, "
                "total_usage = total_usage + %s, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE workspace_id = %s AND shard = 0 AND reserved >= %s",
                (
                    reservation.amount_microdollars,
                    actual_microdollars,
                    reservation.workspace_id,
                    reservation.amount_microdollars,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("reservation release row-count != 1")

        self._run_transaction(finalize)

    # --- Cross-plane credit transfer ---------------------------------------
    #
    # See trusted_router.credit_transfer for the state machine, which plane
    # holds the value in each state, and the conservation invariant. Every
    # transition below pairs an INSERT-ONCE row with the balance change it
    # authorizes, in ONE transaction, so a retry re-runs an insert that loses
    # and therefore moves nothing.

    def open_credit_transfer(
        self,
        *,
        transfer_id: str,
        workspace_id: str,
        amount_microdollars: int,
        destination: str,
    ) -> CreditTransfer:
        """SOURCE side: debit into escrow. Value becomes held by THIS plane.

        Idempotent on `transfer_id`: a redelivered open returns the existing
        record and debits nothing. Raises ValueError("insufficient credits")
        when the workspace cannot cover the amount — a CONDITIONAL debit, never
        a blind decrement, so two concurrent transfers cannot overdraw between
        a read and a write.
        """
        transfer_id = validate_transfer_id(transfer_id)
        amount = validate_amount(amount_microdollars)
        transfer = CreditTransfer(
            id=transfer_id,
            workspace_id=workspace_id,
            amount_microdollars=amount,
            destination=str(destination or ""),
            state=credit_transfer.ESCROWED,
        )

        def open_transfer(conn: Any) -> CreditTransfer:
            # Insert-once FIRST: if this loses, the debit below must not run.
            won = self._insert_entity_once_tx(conn, _CREDIT_TRANSFER_KIND, transfer_id, transfer)
            if not won:
                existing = self._read_entity_tx(
                    conn, _CREDIT_TRANSFER_KIND, transfer_id, CreditTransfer
                )
                if existing is None:
                    raise RuntimeError("credit transfer row disappeared after conflict")
                # Idempotency is keyed on the id, but the id alone does not
                # identify the AGREEMENT. Returning a transfer escrowed for
                # destination A to a caller holding a client for destination B
                # lets push/cancel ask the WRONG plane for a verdict on value
                # this plane is holding for someone else — and a REJECTED
                # tombstone written by B releases the escrow while A may have
                # already accepted. That is a double-spend, not a retry.
                #
                # recover_credit_transfers already skips on this exact
                # mismatch; the check belongs here too, where every caller
                # passes through.
                #
                # The workspace and amount are checked for the same reason. An
                # operator funding workspace B with an id already spent on
                # workspace A would otherwise be handed A's record and told
                # "delivered" — a 200 for a transfer that moved nothing for B,
                # and no field in the reply to notice it by.
                credit_transfer.require_matching_transfer(
                    transfer_id,
                    existing,
                    workspace_id=workspace_id,
                    amount_microdollars=amount,
                    destination=destination,
                )
                return existing
            self._write_entity_tx(
                conn,
                _CREDIT_TRANSFER_OPEN_KIND,
                transfer_id,
                {"transfer_id": transfer_id},
            )
            cursor = conn.execute(
                "UPDATE tr_credit_balance "
                "SET total_credits = total_credits - %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE workspace_id = %s AND shard = 0 "
                "AND total_credits - total_usage - reserved >= %s",
                (amount, workspace_id, amount),
            )
            if cursor.rowcount != 1:
                # Rolls the whole transaction back, including the insert-once
                # row, so the same transfer id stays usable after the customer
                # tops up. A refused transfer must leave no trace.
                raise ValueError("insufficient credits")
            return transfer

        return self._run_transaction(open_transfer)

    def get_credit_transfer(self, transfer_id: str) -> CreditTransfer | None:
        return self._read_entity(_CREDIT_TRANSFER_KIND, transfer_id, CreditTransfer)

    def list_open_credit_transfers(
        self, limit: int = 100, *, after_id: str = ""
    ) -> list[CreditTransfer]:
        """Transfers still in ESCROWED — the recovery queue.

        Bounded PK-prefix scan of the open index, which the resolve path
        deletes from. A transfer appears here exactly while its fate is
        unknown, which is precisely what a recovery pass must ask the
        destination about.

        PAGED, because not every row leaves the queue by being resolved. A
        transfer escrowed for a DIFFERENT destination is skipped on every pass
        and stays in the index; once `limit` of those sort ahead of the live
        escrows, an unpaged "first N" would return nothing but skips forever
        and silently stop recovering anything else. `after_id` lets the driver
        walk past them.
        """
        bounded = max(1, min(int(limit), 500))
        cursor_id = str(after_id or "")

        def read(conn: Any) -> list[CreditTransfer]:
            rows = conn.execute(
                "SELECT id FROM tr_entities WHERE kind = %s AND id > %s ORDER BY id LIMIT %s",
                (_CREDIT_TRANSFER_OPEN_KIND, cursor_id, bounded),
            ).fetchall()
            transfers = []
            for row in rows:
                entity_id = str(row[0])
                transfer = self._read_entity_tx(
                    conn, _CREDIT_TRANSFER_KIND, entity_id, CreditTransfer
                )
                if transfer is not None and transfer.state == credit_transfer.ESCROWED:
                    transfers.append(transfer)
                    continue
                # An index row whose transfer is resolved (or absent) is
                # garbage: resolve deletes both in one transaction, so this
                # only exists after a partial repair. Dropping it here keeps
                # "a row in the index means an escrowed transfer is returned"
                # true, which is what lets the driver page on the last
                # RETURNED id without a filtered-out row stalling the walk.
                self._delete_entity_tx(conn, _CREDIT_TRANSFER_OPEN_KIND, entity_id)
            return transfers

        return self._run_transaction(read)

    def resolve_credit_transfer(self, *, transfer_id: str, outcome: str) -> CreditTransfer:
        """SOURCE side: record the DESTINATION's verdict, and only that.

        ACCEPTED -> DELIVERED: value is now held by the destination; the source
        balance is untouched (it was debited at escrow).
        REJECTED -> RETURNED: the escrowed amount is credited back here, in the
        same transaction that records the state, so it cannot be returned
        twice.

        This plane never invents a verdict. A repeat of the SAME verdict is a
        no-op; a DISAGREEING one raises CreditTransferConflict rather than
        applying a second balance change.

        THE GUARD IS THE INSERT-ONCE ROW, not the state read. Reading the
        transfer and branching on `state` in Python decides on a snapshot: two
        transactions that both read ESCROWED — an operator cancel racing the
        recovery pass, which the recovery docstring explicitly says is safe —
        would both fall through and both run the refund. The read is still
        taken FOR UPDATE so the ordinary case serializes on the row, but
        correctness rests on the insert, which is the same mechanism
        `open_credit_transfer` and `claim_credit_transfer` already use and the
        one this transition was missing.
        """
        transfer_id = validate_transfer_id(transfer_id)
        outcome = validate_outcome(outcome)
        target_state = credit_transfer.STATE_FOR_OUTCOME[outcome]

        def resolve(conn: Any) -> CreditTransfer:
            existing = self._read_entity_tx(
                conn, _CREDIT_TRANSFER_KIND, transfer_id, CreditTransfer, for_update=True
            )
            if existing is None:
                raise KeyError(transfer_id)
            if existing.state != credit_transfer.ESCROWED:
                if existing.state != target_state:
                    raise CreditTransferConflict(
                        f"transfer {transfer_id} is {existing.state}; "
                        f"cannot re-resolve it as {target_state}"
                    )
                return existing
            resolved = dataclasses.replace(existing, state=target_state, resolved_at=iso_now())
            # Insert-once FIRST, exactly as at escrow: if this loses, the
            # balance change below must not run. The loser learns the winner's
            # verdict instead of applying a second one.
            won = self._insert_entity_once_tx(
                conn,
                _CREDIT_TRANSFER_RESOLUTION_KIND,
                transfer_id,
                {
                    "outcome": outcome,
                    "workspace_id": existing.workspace_id,
                    "amount_microdollars": existing.amount_microdollars,
                    "resolved_at": resolved.resolved_at,
                },
            )
            if not won:
                recorded = self._read_entity_tx(
                    conn, _CREDIT_TRANSFER_RESOLUTION_KIND, transfer_id, dict
                )
                if recorded is None:
                    raise RuntimeError("credit transfer resolution disappeared")
                decided = str(recorded["outcome"])
                if decided != outcome:
                    raise CreditTransferConflict(
                        f"transfer {transfer_id} was already resolved as "
                        f"{decided}; cannot re-resolve it as {outcome}"
                    )
                # Same verdict, already applied by the winner. The transfer row
                # this transaction read was stale; report the settled shape
                # without touching a balance.
                return dataclasses.replace(
                    existing,
                    state=credit_transfer.STATE_FOR_OUTCOME[decided],
                    resolved_at=str(recorded.get("resolved_at") or "") or None,
                )
            self._write_entity_tx(conn, _CREDIT_TRANSFER_KIND, transfer_id, resolved)
            # Leaves the recovery queue only now: while this row exists the
            # fate is unknown and a recovery pass must keep asking.
            self._delete_entity_tx(conn, _CREDIT_TRANSFER_OPEN_KIND, transfer_id)
            if outcome == credit_transfer.REJECTED:
                cursor = conn.execute(
                    "UPDATE tr_credit_balance "
                    "SET total_credits = total_credits + %s, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE workspace_id = %s AND shard = 0",
                    (existing.amount_microdollars, existing.workspace_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "missing authoritative tr_credit_balance for "
                        f"workspace {existing.workspace_id}"
                    )
            return resolved

        return self._run_transaction(resolve)

    def claim_credit_transfer(
        self,
        *,
        transfer_id: str,
        workspace_id: str,
        amount_microdollars: int,
        source: str,
        accept: bool,
    ) -> str:
        """DESTINATION side: decide a transfer's fate, exactly once.

        Returns the DECIDED outcome, which may differ from `accept` — the
        first writer wins and every later caller learns that verdict instead
        of overriding it. That single insert-once row is what makes a
        duplicate delivery credit once, and what makes an accept that races a
        cancel resolve one way for both planes.

        On ACCEPTED the local balance is credited in the same transaction as
        the row, so the row existing and the money existing are the same fact.
        """
        transfer_id = validate_transfer_id(transfer_id)
        amount = validate_amount(amount_microdollars)
        requested = credit_transfer.ACCEPTED if accept else credit_transfer.REJECTED

        def claim(conn: Any) -> str:
            won = self._insert_entity_once_tx(
                conn,
                _CREDIT_TRANSFER_CLAIM_KIND,
                transfer_id,
                {
                    "outcome": requested,
                    "workspace_id": workspace_id,
                    "amount_microdollars": amount,
                    "source": str(source or ""),
                    "created_at": iso_now(),
                },
            )
            if not won:
                recorded = self._read_entity_tx(
                    conn, _CREDIT_TRANSFER_CLAIM_KIND, transfer_id, dict
                )
                if recorded is None:
                    raise RuntimeError("credit transfer claim disappeared after conflict")
                # The recorded verdict answers for the (workspace, amount) it
                # was written with, and for NO other. Replaying it blindly is
                # how a second source plane gets "accepted" for free: it
                # debited, nothing here was credited, and both planes report
                # success. Two AWS regions pushing an operator-chosen id like
                # "topup-2026-08" is enough to reach it. Refuse instead — the
                # source treats a non-200 as unknown and keeps the value
                # escrowed, which is recoverable; a false "accepted" is not.
                credit_transfer.require_matching_transfer(
                    transfer_id,
                    recorded,
                    workspace_id=workspace_id,
                    amount_microdollars=amount,
                    source=str(source or ""),
                )
                return str(recorded["outcome"])
            if requested == credit_transfer.REJECTED:
                return credit_transfer.REJECTED
            cursor = conn.execute(
                "UPDATE tr_credit_balance "
                "SET total_credits = total_credits + %s, "
                "source_updated_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE workspace_id = %s AND shard = 0",
                (amount, workspace_id),
            )
            if cursor.rowcount != 1:
                # No balance row means no such workspace here. Rolling back
                # discards the claim row too, so the source can retry once the
                # workspace has been federated rather than being told the
                # transfer was accepted by a plane that never credited it.
                raise ValueError(f"no credit balance for workspace {workspace_id} on this plane")
            return credit_transfer.ACCEPTED

        return self._run_transaction(claim)

    def update_auto_refill_settings(
        self,
        workspace_id: str,
        *,
        enabled: bool,
        threshold_microdollars: int,
        amount_microdollars: int,
    ) -> CreditAccount | None:
        self._not_implemented("update_auto_refill_settings")

    def set_stripe_customer(
        self,
        workspace_id: str,
        *,
        customer_id: str,
        payment_method_id: str | None = None,
    ) -> CreditAccount | None:
        self._not_implemented("set_stripe_customer")

    def clear_stripe_payment_method(self, workspace_id: str) -> CreditAccount | None:
        self._not_implemented("clear_stripe_payment_method")

    def record_auto_refill_outcome(
        self,
        workspace_id: str,
        *,
        status: str,
    ) -> CreditAccount | None:
        self._not_implemented("record_auto_refill_outcome")

    # Gateway authorizations -------------------------------------------------

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
        """Record an authorization, deduplicating on the idempotency key.

        The idempotency index is a SEPARATE entity inserted with
        insert-once semantics, so two concurrent identical requests cannot
        both create an authorization: the loser reads the winner's row and
        returns it. That matters because an authorization holds a credit
        reservation — duplicating one double-holds the customer's money.

        `deferred_cap_microdollars` turns this into the admission point for
        deferred spend: the outstanding counter is incremented by the estimate
        under a conditional UPDATE **in this same transaction**, and
        DeferredSettlementCapReached is raised if the cap refuses. Both
        properties matter. In-transaction means an idempotent replay — which
        returns the existing authorization and writes nothing — cannot
        double-count the estimate, and a cap refusal cannot leave an orphaned
        authorization behind. Conditional means the cap is a real bound rather
        than advice: authorize-to-settle spans a whole provider stream, so N
        concurrent requests reading one stale total would every one of them
        admit.
        """
        authorization = GatewayAuthorization(
            id=authorization_id or f"gwauth_{uuid.uuid4().hex}",
            workspace_id=workspace_id,
            key_hash=key_hash,
            model_id=model_id,
            provider=provider,
            usage_type=cast(Any, usage_type),
            estimated_microdollars=estimated_microdollars,
            credit_reservation_id=credit_reservation_id,
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
            settlement=settlement,
            expires_at=expires_at,
        )

        def create(conn: Any) -> GatewayAuthorization:
            if idempotency_key:
                index_id = _gateway_idempotency_id(workspace_id, key_hash, idempotency_key)
                won = self._insert_entity_once_tx(
                    conn,
                    _GATEWAY_IDEMPOTENCY_KIND,
                    index_id,
                    {"authorization_id": authorization.id},
                )
                if not won:
                    pointer = self._read_entity_tx(conn, _GATEWAY_IDEMPOTENCY_KIND, index_id, dict)
                    existing_id = str((pointer or {}).get("authorization_id") or "")
                    existing = (
                        self._read_entity_tx(
                            conn, _GATEWAY_AUTHORIZATION_KIND, existing_id, GatewayAuthorization
                        )
                        if existing_id
                        else None
                    )
                    if existing is not None:
                        return existing
                    # Index row without its authorization: a create that died
                    # between the two writes. Fall through and take ownership
                    # rather than failing the request forever.
                    self._write_entity_tx(
                        conn,
                        _GATEWAY_IDEMPOTENCY_KIND,
                        index_id,
                        {"authorization_id": authorization.id},
                    )
            if deferred_cap_microdollars is not None:
                self._reserve_deferred_outstanding_tx(
                    conn,
                    workspace_id,
                    estimated_microdollars,
                    cap_microdollars=deferred_cap_microdollars,
                )
            self._write_entity_tx(
                conn, _GATEWAY_AUTHORIZATION_KIND, authorization.id, authorization
            )
            return authorization

        return self._run_transaction(create)

    def get_gateway_authorization(self, authorization_id: str) -> GatewayAuthorization | None:
        return self._read_entity(
            _GATEWAY_AUTHORIZATION_KIND, authorization_id, GatewayAuthorization
        )

    def get_gateway_authorization_by_idempotency_key(
        self,
        workspace_id: str,
        key_hash: str,
        idempotency_key: str,
    ) -> GatewayAuthorization | None:
        pointer = self._read_entity(
            _GATEWAY_IDEMPOTENCY_KIND,
            _gateway_idempotency_id(workspace_id, key_hash, idempotency_key),
            dict,
        )
        authorization_id = str((pointer or {}).get("authorization_id") or "")
        if not authorization_id:
            return None
        return self.get_gateway_authorization(authorization_id)

    def mark_gateway_authorization_settled(self, authorization_id: str) -> None:
        self._not_implemented("mark_gateway_authorization_settled")

    def finalize_gateway_authorization(
        self,
        authorization_id: str,
        *,
        success: bool,
        actual_microdollars: int,
        selected_usage_type: UsageType | str,
        generation: Generation | None = None,
    ) -> bool:
        """Settle a gateway authorization exactly once, in ONE transaction.

        Mirrors the Spanner/InMemory contract: release the credit hold,
        release the key-limit hold, book usage, and mark the authorization
        settled together. Returns True when this call did the work, False
        when it was already finalized.

        Exactly-once comes from an insert-once finalization marker keyed by
        authorization id, the same primitive the reservation path uses. A
        settle that is retried after a timeout MUST NOT release the hold
        twice — doing so hands the customer free headroom and corrupts the
        balance, which is the one failure mode money code cannot have.
        """

        def finalize(conn: Any) -> bool:
            authorization = self._read_entity_tx(
                conn,
                _GATEWAY_AUTHORIZATION_KIND,
                authorization_id,
                GatewayAuthorization,
            )
            if authorization is None:
                return False

            won = self._insert_entity_once_tx(
                conn,
                _GATEWAY_FINALIZATION_KIND,
                authorization_id,
                {
                    "success": success,
                    "actual_microdollars": actual_microdollars,
                },
            )
            if not won:
                # Already finalized by a concurrent or earlier call.
                return False

            booked = max(0, int(actual_microdollars)) if success else 0

            # 0. DEFERRED settlement: the spend is debt to the home plane's
            #    ledger, not a debit against this plane's balance. The
            #    decision was made at authorize and STORED — re-deriving it
            #    here from key state would read a record that can change
            #    between authorize and settle.
            #
            #    The outbox insert is gated on having WON the finalization
            #    marker above, so a redelivered settle cannot enqueue a second
            #    row for the same authorization; the marker and the row commit
            #    in this one transaction, so the debt can never exist without
            #    its usage record or vice versa.
            if authorization.settlement == _DEFERRED_HOME_SETTLEMENT:
                # A BYOK-selected settle owes home NOTHING: the customer's own
                # provider key paid for the tokens. Mixed candidate lists make
                # this reachable — authorize went deferred because the CREDITS
                # candidates needed money the local plane doesn't hold, but
                # the enclave was still free to pick a BYOK endpoint. Debiting
                # home's ledger for spend the customer already paid a provider
                # for would charge them twice.
                owes_home = 0 if _is_byok(selected_usage_type) else booked
                if owes_home:
                    conn.execute(
                        "INSERT INTO tr_home_settlement_outbox"
                        "   (authorization_id, workspace_id, cost_microdollars,"
                        "    state, attempts, enqueued_at, updated_at)"
                        " VALUES (%s, %s, %s, 'pending', 0,"
                        "         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        " ON CONFLICT (authorization_id) DO NOTHING",
                        (authorization_id, authorization.workspace_id, owes_home),
                        prepare=False,
                    )
                # True up the counter to the FROZEN debt. Authorize admitted
                # the estimate; a failed request or a BYOK-selected one owes
                # nothing and hands the whole estimate back, which is why this
                # is a signed delta rather than an increment.
                self._adjust_deferred_outstanding_tx(
                    conn,
                    authorization.workspace_id,
                    owes_home - int(authorization.estimated_microdollars),
                )
                self._release_key_hold_tx(
                    conn,
                    authorization.key_hash,
                    authorization.estimated_microdollars,
                    # The hold was reserved under the AUTHORIZE-time type
                    # (Credits whenever a credit candidate existed) — release
                    # under the same type, or an include_byok=false key whose
                    # request settled on a BYOK endpoint strands its hold
                    # forever. The window contribution keys off what was
                    # actually SELECTED.
                    usage_type=authorization.usage_type,
                    window_amount=booked,
                    window_is_byok=_is_byok(selected_usage_type),
                )
                if booked:
                    column = "byok_usage" if _is_byok(selected_usage_type) else "usage"
                    conn.execute(
                        f"UPDATE tr_key_limit SET {column} = {column} + %s"  # noqa: S608
                        " , updated_at = CURRENT_TIMESTAMP"
                        " WHERE key_hash = %s AND shard = 0",
                        (booked, authorization.key_hash),
                        prepare=False,
                    )
                if generation is not None:
                    self._write_entity_tx(conn, "generation", generation.id, generation)
                    if self._operational_analytics_outbox is not None:
                        self._operational_analytics_outbox.enqueue_activity_tx(conn, generation)
                authorization.record_finalization(
                    success=success,
                    actual_microdollars=actual_microdollars,
                    selected_usage_type=selected_usage_type,
                    generation=generation,
                )
                # Clearing the expiry is what takes this row out of the
                # reaper's scan: settled work is no longer reclaimable, and
                # leaving a past expires_at behind would make every future
                # reap pass re-examine rows it can never act on.
                authorization.expires_at = None
                self._write_entity_tx(
                    conn, _GATEWAY_AUTHORIZATION_KIND, authorization_id, authorization
                )
                return True

            # 1. Credit hold: release the reservation, book actual spend.
            #
            # UPSERT, not UPDATE. reserve() proved the balance row existed at
            # authorize time, but retention jobs and cleanup can remove rows
            # between reserve and settle, and a plain UPDATE matches zero rows
            # SILENTLY: the spend vanishes while the authorization is marked
            # settled. Recreating the row (zero credits, so the balance goes
            # negative) keeps the ledger honest — a negative balance is a
            # visible, billable fact; a missing debit is invisible forever.
            #
            # The same reasoning covers a vanished reservation ENTITY: only
            # the release amount depends on it, so its absence must not skip
            # the booking. Nothing is released (the reserved counter on a
            # recreated row is already zero) and the anomaly is logged.
            if authorization.credit_reservation_id:
                reservation = self._read_entity_tx(
                    conn, _RESERVATION_KIND, authorization.credit_reservation_id, Reservation
                )
                release = reservation.amount_microdollars if reservation is not None else 0
                if reservation is None:
                    log.error(
                        "finalize %s: reservation %s entity is gone; booking "
                        "%s microdollars of usage without a release",
                        authorization_id,
                        authorization.credit_reservation_id,
                        booked,
                    )
                cursor = conn.execute(
                    "INSERT INTO tr_credit_balance"
                    "   (workspace_id, shard, total_usage, updated_at)"
                    " VALUES (%s, 0, %s, CURRENT_TIMESTAMP)"
                    " ON CONFLICT (workspace_id, shard) DO UPDATE SET"
                    "   reserved = GREATEST(tr_credit_balance.reserved - %s, 0)"
                    " , total_usage = tr_credit_balance.total_usage"
                    "     + EXCLUDED.total_usage"
                    " , updated_at = CURRENT_TIMESTAMP",
                    (
                        authorization.workspace_id,
                        booked,
                        release,
                    ),
                    prepare=False,
                )
                if cursor.rowcount != 1:
                    # An upsert cannot miss; if it ever does, that is a driver
                    # or dialect regression and it must fail the settle loudly
                    # rather than repeat the silent-vanish this block fixes.
                    raise RuntimeError(
                        f"finalize {authorization_id}: balance upsert affected "
                        f"{cursor.rowcount} rows for workspace "
                        f"{authorization.workspace_id}"
                    )
                self._insert_entity_once_tx(
                    conn,
                    _RESERVATION_FINALIZATION_KIND,
                    authorization.credit_reservation_id,
                    {
                        "actual_microdollars": booked,
                        "operation": "gateway_finalize",
                    },
                )

            # 2. Key-limit hold: release, and roll the windows by actual spend.
            #    Reuses the same lazy-window rules as settle_key_limit.
            #
            #    Released under the AUTHORIZE-time type, not the selected one.
            #    They differ on a mixed Credits/BYOK authorization (the hold
            #    is Credits-typed whenever any credit candidate existed, but
            #    the enclave may select a BYOK endpoint), and passing the
            #    selected type made _release_key_hold_tx's early-return skip
            #    the release entirely on include_byok=false keys — reserved
            #    stranded forever, the key's cap shrinking with every mixed
            #    request that landed on BYOK. Only the WINDOW contribution
            #    keys off what was actually selected.
            self._release_key_hold_tx(
                conn,
                authorization.key_hash,
                authorization.estimated_microdollars,
                usage_type=authorization.usage_type,
                window_amount=booked,
                window_is_byok=_is_byok(selected_usage_type),
            )

            # 3. Lifetime key usage. settle_key_limit deliberately does NOT
            #    book this (add_generation owns it on other backends); here
            #    the gateway finalize is that owner.
            if booked:
                column = "byok_usage" if _is_byok(selected_usage_type) else "usage"
                conn.execute(
                    f"UPDATE tr_key_limit SET {column} = {column} + %s"  # noqa: S608
                    " , updated_at = CURRENT_TIMESTAMP"
                    " WHERE key_hash = %s AND shard = 0",
                    (booked, authorization.key_hash),
                    prepare=False,
                )

            # 4. Generation metadata, when the caller supplied one, plus the
            #    ClickHouse delivery intent for it. The outbox row rides the
            #    SAME transaction as the money and the generation record, so
            #    the activity stream cannot disagree with the ledger: either
            #    both commit or neither does. ClickHouse is never in this
            #    transaction — only the durable intent to deliver to it.
            if generation is not None:
                self._write_entity_tx(conn, "generation", generation.id, generation)
                if self._operational_analytics_outbox is not None:
                    self._operational_analytics_outbox.enqueue_activity_tx(conn, generation)

            # 5. Mark settled.
            authorization.record_finalization(
                success=success,
                actual_microdollars=actual_microdollars,
                selected_usage_type=selected_usage_type,
                generation=generation,
            )
            self._write_entity_tx(
                conn, _GATEWAY_AUTHORIZATION_KIND, authorization_id, authorization
            )
            return True

        return self._run_transaction(finalize)

    def reap_expired_deferred_authorizations(self, *, limit: int = 100) -> dict[str, int]:
        """Reclaim deferred authorizations whose settle never arrived.

        WHY THIS IS PART OF THE FEATURE, not a follow-up: the enclave dying
        between authorize and settle is the most ROUTINE failure there is —
        every deploy does it — and this plane has no other sweeper (Spanner's
        reap_expired_reservations is Spanner-only). Without this, each
        abandoned authorization leaks its estimate into the outstanding
        counter permanently; once the leaks reach the cap, every subsequent
        CREDITS request for that workspace 402s forever, on a healthy plane,
        with home back up and no way to recover. A bound meant to protect the
        home ledger would become a self-inflicted, unrecoverable outage.

        Reaping claims the SAME insert-once finalization marker that settle
        claims, so reaper-vs-late-settle is first-writer-wins and never both.
        A late settle that loses simply returns False, exactly like a
        redelivered settle does today.
        """
        now = iso_now()

        def reap(conn: Any) -> dict[str, int]:
            # `expires_at IS NOT NULL` is the "still outstanding" predicate:
            # settling a deferred authorization CLEARS it (see finalize), so
            # this needs no boolean comparison. That matters beyond taste —
            # `body ->> 'settled'` yields the text 'false' on Postgres and the
            # integer 0 on SQLite, so a predicate written against one dialect
            # silently matches nothing on the other, and a reaper that matches
            # nothing looks exactly like a reaper with nothing to do.
            rows = conn.execute(
                "SELECT id FROM tr_entities"
                " WHERE kind = %s"
                "   AND body ->> 'settlement' = %s"
                "   AND body ->> 'expires_at' IS NOT NULL"
                "   AND body ->> 'expires_at' < %s"
                " ORDER BY id"
                " LIMIT %s",
                (
                    _GATEWAY_AUTHORIZATION_KIND,
                    _DEFERRED_HOME_SETTLEMENT,
                    now,
                    int(limit),
                ),
                prepare=False,
            ).fetchall()

            reaped = 0
            skipped_settled = 0
            for (authorization_id,) in rows:
                authorization = self._read_entity_tx(
                    conn,
                    _GATEWAY_AUTHORIZATION_KIND,
                    str(authorization_id),
                    GatewayAuthorization,
                )
                if authorization is None or authorization.settled:
                    continue
                # OUTBOX-GUARDED, ported from the Spanner reaper: an
                # authorization whose settle already enqueued its debt must
                # never be reaped. Reaping it would hand back an estimate that
                # settle has already trued up, double-crediting the counter.
                enqueued = conn.execute(
                    "SELECT 1 FROM tr_home_settlement_outbox WHERE authorization_id = %s",
                    (str(authorization_id),),
                    prepare=False,
                ).fetchone()
                if enqueued is not None:
                    skipped_settled += 1
                    continue
                won = self._insert_entity_once_tx(
                    conn,
                    _GATEWAY_FINALIZATION_KIND,
                    str(authorization_id),
                    {"success": False, "actual_microdollars": 0, "operation": "reap"},
                )
                if not won:
                    # A settle is committing concurrently, or already did.
                    skipped_settled += 1
                    continue
                self._adjust_deferred_outstanding_tx(
                    conn,
                    authorization.workspace_id,
                    -int(authorization.estimated_microdollars),
                )
                self._release_key_hold_tx(
                    conn,
                    authorization.key_hash,
                    authorization.estimated_microdollars,
                    usage_type=authorization.usage_type,
                    window_amount=0,
                )
                authorization.record_finalization(
                    success=False,
                    actual_microdollars=0,
                    selected_usage_type=authorization.usage_type,
                    generation=None,
                )
                authorization.expires_at = None
                self._write_entity_tx(
                    conn,
                    _GATEWAY_AUTHORIZATION_KIND,
                    str(authorization_id),
                    authorization,
                )
                reaped += 1
            return {
                "examined": len(rows),
                "reaped": reaped,
                "skipped_settled": skipped_settled,
            }

        return self._run_transaction(reap)

    # Generations + activity -------------------------------------------------

    def add_generation(self, generation: Generation) -> None:
        self._not_implemented("add_generation")

    def record_client_events_batch(self, payload: dict[str, Any]) -> None:
        outbox = self._operational_analytics_outbox
        if outbox is None:
            log.warning(
                "postgres.client_events_outbox_disabled_drop",
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
                    "postgres.client_events_duplicate",
                    extra={
                        "tenant": str(payload["tenant_id"])[:12],
                        "batch_id": payload["batch_id"],
                    },
                )
                return
            log.exception(
                "postgres.client_events_enqueue_failed",
                extra={
                    "tenant": str(payload["tenant_id"])[:12],
                    "batch_id": payload["batch_id"],
                    "error_class": type(exc).__name__,
                },
            )
            raise

    def record_provider_benchmark(self, sample: ProviderBenchmarkSample) -> None:
        self._run_transaction(
            lambda conn: self._write_entity_tx(
                conn,
                "provider_benchmark",
                sample.id,
                sample,
            )
        )

    def provider_benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        date_pattern = None if date is None else f"{date}%"

        def list_samples(conn: Any) -> list[ProviderBenchmarkSample]:
            rows = conn.execute(
                "SELECT body FROM tr_entities "
                "WHERE kind = 'provider_benchmark' "
                "AND (%s::text IS NULL OR body ->> 'created_at' LIKE %s) "
                "AND (%s::text IS NULL OR body ->> 'provider' = %s) "
                "AND (%s::text IS NULL OR body ->> 'model' = %s) "
                "ORDER BY body ->> 'created_at' DESC, id DESC "
                "LIMIT %s",
                (
                    date_pattern,
                    date_pattern,
                    provider,
                    provider,
                    model,
                    model,
                    max(0, int(limit)),
                ),
            ).fetchall()
            samples: list[ProviderBenchmarkSample] = []
            for row in rows:
                raw = row[0]
                data = json.loads(raw) if isinstance(raw, str) else dict(raw)
                known = {field.name for field in dataclasses.fields(ProviderBenchmarkSample)}
                samples.append(
                    ProviderBenchmarkSample(
                        **{key: value for key, value in data.items() if key in known}
                    )
                )
            return samples

        return self._run_transaction(list_samples)

    def record_synthetic_probe_sample(self, sample: SyntheticProbeSample) -> None:
        def record(conn: Any) -> None:
            self._write_indexed_entity_tx(
                conn,
                "synthetic_probe",
                sample.id,
                sample,
                indexed_at=sample.created_at,
                index_date=sample.created_at[:10],
                index_target=sample.target,
                index_probe_type=sample.probe_type,
                index_monitor_region=sample.monitor_region,
            )
            # Delivery intent commits with the sample. The Spanner path
            # enqueues best-effort in a SEPARATE transaction and logs when
            # that fails, because its sample write is not one transaction;
            # here the whole record IS one transaction, so the stronger
            # guarantee is free and a probe can never be recorded without
            # its status event.
            if self._operational_analytics_outbox is not None:
                self._operational_analytics_outbox.enqueue_synthetic_tx(conn, sample)
            for period, component in sample_rollup_ids(sample):
                update = new_rollup_for_sample(
                    sample,
                    period=period,
                    component=component,
                )
                marker_id = f"{update.id}:{sample.id}"
                if not self._insert_entity_once_tx(
                    conn,
                    "synthetic_rollup_seen",
                    marker_id,
                    {"seen": True},
                ):
                    continue
                if self._insert_indexed_entity_once_tx(
                    conn,
                    "synthetic_rollup",
                    update.id,
                    update,
                    indexed_at=update.period_start,
                    index_period=update.period,
                ):
                    continue
                existing = self._read_entity_tx(
                    conn,
                    "synthetic_rollup",
                    update.id,
                    SyntheticRollup,
                    for_update=True,
                )
                if existing is None:
                    raise StoreConflict("Synthetic rollup disappeared during update")
                apply_sample_to_rollup(existing, sample)
                self._write_indexed_entity_tx(
                    conn,
                    "synthetic_rollup",
                    existing.id,
                    existing,
                    indexed_at=existing.period_start,
                    index_period=existing.period,
                )

        self._run_transaction(record)

    def synthetic_probe_samples(
        self,
        *,
        date: str | None = None,
        target: str | None = None,
        probe_type: str | None = None,
        monitor_region: str | None = None,
        limit: int = 1000,
    ) -> list[SyntheticProbeSample]:
        def list_samples(conn: Any) -> list[SyntheticProbeSample]:
            # Bounded on BOTH sides. The lower bound is retention; the
            # upper bound keeps future-dated poison out of every read:
            # indexed_at = sample.created_at, ORDER BY indexed_at DESC —
            # so without the upper bound a single year-7748 fixture row
            # sorts first in every response forever, and retention (a
            # lower bound) never expires it.
            predicates = [
                "kind = %s",
                "indexed_at >= %s",
                "indexed_at <= %s",
            ]
            params: list[Any] = [
                "synthetic_probe",
                utcnow() - dt.timedelta(days=RAW_SYNTHETIC_RETENTION_DAYS),
                utcnow() + dt.timedelta(seconds=FUTURE_SAMPLE_SKEW_SECONDS),
            ]
            if date is not None:
                predicates.append("index_date = %s")
                params.append(date)
            if target is not None:
                predicates.append("index_target = %s")
                params.append(target)
            if probe_type is not None:
                predicates.append("index_probe_type = %s")
                params.append(probe_type)
            if monitor_region is not None:
                predicates.append("index_monitor_region = %s")
                params.append(monitor_region)
            params.append(max(0, int(limit)))
            query = (
                "SELECT body FROM tr_entities WHERE "  # noqa: S608 - fixed predicates.
                + " AND ".join(predicates)
                + " ORDER BY indexed_at DESC, id DESC LIMIT %s"
            )
            rows = conn.execute(query, params).fetchall()
            return [_dataclass_from_json(row[0], SyntheticProbeSample) for row in rows]

        return self._run_transaction(list_samples)

    def operational_analytics_outbox_freshness(self) -> OutboxFreshness:
        """The age of the oldest row the drain has not delivered yet.

        This is the AWS and Azure answer, and it is the one the outage of
        2026-08-02..17 needed: on AWS-EU the outbox held 470,370 rows and the
        only process that could have said so had never been installed. The
        control plane already holds the DSQL connection, so publishing this
        costs one index seek behind the status cache and needs no new IAM, no
        new unit, and no reachability into the VPC.

        BOUNDED ON BOTH AXES, the same way `readiness_check` above is, and for
        a sharper reason: this read sits on the PUBLIC /status.json build path,
        inside an async handler. A blocking call there does not tie up one
        worker thread, it stops the event loop, so an unbounded wait on a hung
        DSQL cluster would take the status page -- and every other request this
        process is serving -- down with it. Two separate waits have to be
        capped, because either one alone is unbounded:

        * `connection(timeout=)`  -- the wait for a pool slot. An exhausted or
          unreachable pool blocks here having issued no SQL at all, so a
          statement timeout never gets the chance to fire.
        * `SET LOCAL statement_timeout` -- the server-side cap on the query,
          which is what covers a connection that is up but a cluster that is
          not answering.

        Deliberately NOT run through `_run_transaction`: that retries
        serialization failures, and a retry loop is the one thing that turns a
        bounded read back into an unbounded one.

        Every failure -- timeout included -- degrades to `unreachable`. It must
        never raise: the caller publishes this section unconditionally, and an
        exception escaping here would drop the key, which the fleet checker
        reads as "this deployment is running code too old to publish drain
        lag" and answers by telling somebody to redeploy a healthy service.
        """
        outbox = self._operational_analytics_outbox
        if outbox is None:
            return OutboxFreshness.unavailable(BACKEND_POSTGRES, REASON_NOT_CONFIGURED)
        try:
            with self._pool.connection(timeout=OUTBOX_FRESHNESS_TIMEOUT_SECONDS) as conn:
                with conn.transaction():
                    conn.execute(
                        f"SET LOCAL statement_timeout = '{OUTBOX_FRESHNESS_TIMEOUT_SECONDS:g}s'"
                    )
                    oldest = outbox.oldest_enqueued_at_tx(conn)
        except Exception as exc:
            log.exception(
                "postgres.operational_analytics_outbox_freshness_failed",
                extra={"error_class": type(exc).__name__, "error_message": str(exc)[:500]},
            )
            return OutboxFreshness.unavailable(BACKEND_POSTGRES, REASON_UNREACHABLE)
        return OutboxFreshness(backend=BACKEND_POSTGRES, oldest_enqueued_at=oldest)

    def synthetic_rollups(
        self,
        *,
        period: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_histograms: bool = True,
        limit: int = 1000,
    ) -> list[SyntheticRollup]:
        def list_rollups(conn: Any) -> list[SyntheticRollup]:
            predicates = [
                "kind = %s",
                "indexed_at >= %s",
            ]
            params: list[Any] = [
                "synthetic_rollup",
                _rollup_retention_cutoff(utcnow()),
            ]
            if period is not None:
                predicates.append("index_period = %s")
                params.append(period)
            if since is not None:
                predicates.append("indexed_at >= %s")
                params.append(_parse_timestamp(since))
            if until is not None:
                predicates.append("indexed_at <= %s")
                params.append(_parse_timestamp(until))
            params.append(max(0, int(limit)))
            query = (
                "SELECT body FROM tr_entities WHERE "  # noqa: S608 - fixed predicates.
                + " AND ".join(predicates)
                + " ORDER BY indexed_at DESC, id DESC LIMIT %s"
            )
            rows = conn.execute(query, params).fetchall()
            rollups = [_dataclass_from_json(row[0], SyntheticRollup) for row in rows]
            if not include_histograms:
                return [
                    dataclasses.replace(
                        rollup,
                        latency_histogram={},
                        ttfb_histogram={},
                        dns_histogram={},
                        tcp_connect_histogram={},
                        tls_handshake_histogram={},
                        gateway_processing_histogram={},
                    )
                    for rollup in rollups
                ]
            return rollups

        return self._run_transaction(list_rollups)

    def get_generation(self, generation_id: str) -> Generation | None:
        # Gateway-reachable (gateway.py:1285) on the settle REPLAY path,
        # where it decides whether a stable generation_id may be echoed back.
        # A miss simply omits the field, which is the documented behaviour
        # when no generation exists for the authorization.
        return self._read_entity("generation", generation_id, Generation)

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
        self._not_implemented("activity")

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
        self._not_implemented("activity_events")

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
        self._not_implemented("activity_result")

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
        self._not_implemented("activity_events_result")

    def usage_series(
        self,
        workspace_id: str,
        *,
        window_minutes: int,
        granularity: str,
        api_key_hash: str | None = None,
        by_model: bool = False,
    ) -> dict[str, Any]:
        self._not_implemented("usage_series")

    def reconcile_generation_activity(
        self,
        workspace_id: str,
        *,
        date: str | None = None,
        limit: int = 1000,
    ) -> int:
        self._not_implemented("reconcile_generation_activity")

    # Rate limiting ----------------------------------------------------------

    def hit_rate_limit(
        self,
        *,
        namespace: str,
        subject: str,
        limit: int,
        window_seconds: int,
        now: dt.datetime | None = None,
    ) -> RateLimitHit:
        self._not_implemented("hit_rate_limit")


def _dataclass_from_json(raw: Any, cls: type[T]) -> T:
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    known = {field.name for field in dataclasses.fields(cast(Any, cls))}
    return cls(**{key: value for key, value in data.items() if key in known})


def _parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _timestamp_string(value: dt.datetime | str) -> str:
    parsed = _parse_timestamp(value) if isinstance(value, str) else _as_utc(value)
    return parsed.isoformat().replace("+00:00", "Z")


def _rollup_retention_cutoff(now: dt.datetime) -> dt.datetime:
    current = now.astimezone(dt.UTC)
    cutoff_month = current.year * 12 + current.month - 1 - ROLLUP_RETENTION_MONTHS + 1
    year, zero_based_month = divmod(cutoff_month, 12)
    return dt.datetime(year, zero_based_month + 1, 1, tzinfo=dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Normalize a DB timestamp for comparison against a tz-aware floor.

    psycopg returns TIMESTAMPTZ as tz-aware, but a column written before the
    type was settled (or a backend returning naive values) would raise
    "can't compare offset-naive and offset-aware datetimes" mid-reserve —
    turning a limit check into a 500. Assume UTC for naive values, which is
    what every writer here stores.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _gateway_idempotency_id(workspace_id: str, key_hash: str, idempotency_key: str) -> str:
    """Stable id for the gateway idempotency index entity.

    Hashed rather than concatenated so an idempotency key containing the
    separator cannot collide with a different (workspace, key) pair — the
    consequence of a collision is one customer's authorization being handed
    to another, so this is not a stylistic choice.
    """
    digest = hashlib.sha256(
        b"\x00".join(x.encode("utf-8") for x in (workspace_id, key_hash, idempotency_key))
    ).hexdigest()
    return f"gwidem_{digest}"
