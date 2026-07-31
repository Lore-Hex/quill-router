"""Postgres implementation of the portable Store contract, increment 1.

The generic ``tr_entities`` table mirrors the Spanner adapter's entity model.
Money remains in typed tables and is changed only with conditional DML.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import secrets
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Never, TypeVar, cast

import psycopg
from psycopg_pool import ConnectionPool

from trusted_router.money import DEFAULT_SIGNUP_CREDIT_MICRODOLLARS
from trusted_router.security import (
    hash_api_key,
    key_label,
    lookup_hash_api_key,
    new_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.storage_errors import StoreConflict, StoreUnavailable
from trusted_router.storage_gcp_codec import (
    json_body,
    member_id,
    normalize_email,
    workspace_key_id,
)
from trusted_router.storage_models import (
    AcquisitionAttribution,
    ApiKey,
    AuthSession,
    BroadcastDeliveryJob,
    BroadcastDestination,
    ByokProviderConfig,
    CreditAccount,
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
    VerificationToken,
    WalletChallenge,
    Workspace,
    _is_expired,
    iso_now,
    normalize_provider_access_role,
    normalize_provider_access_slug,
    utcnow,
)
from trusted_router.synthetic.rollups import (
    RAW_SYNTHETIC_RETENTION_DAYS,
    ROLLUP_RETENTION_MONTHS,
    apply_sample_to_rollup,
    new_rollup_for_sample,
    sample_rollup_ids,
)
from trusted_router.types import UsageType

T = TypeVar("T")

_AWS_DSQL_IAM_AUTH = "aws-dsql"
_AWS_DSQL_HOST_RE = re.compile(
    r"^[^.]+\.dsql\.(?P<region>[a-z0-9-]+)\.on\.aws$",
    re.IGNORECASE,
)


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
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    hostname = str(params.get("host") or "").rstrip(".")
    if not hostname:
        raise ValueError("AWS DSQL IAM auth requires a hostname in TR_POSTGRES_DSN")
    if params.get("password"):
        raise ValueError(
            "TR_POSTGRES_DSN must not contain a password when "
            "TR_POSTGRES_IAM_AUTH=aws-dsql"
        )

    region = region_override.strip()
    if not region:
        match = _AWS_DSQL_HOST_RE.fullmatch(hostname)
        if match is None:
            raise ValueError(
                "Could not infer the AWS region from TR_POSTGRES_DSN host "
                f"{hostname!r}; set TR_POSTGRES_IAM_REGION"
            )
        region = match.group("region")
    return hostname, region


def _aws_dsql_token_provider(hostname: str, region: str) -> Callable[[], str]:
    # Infrastructure SDK imports stay inside the selected adapter path so
    # ordinary Postgres deployments do not import or initialize boto3.
    import boto3

    client = boto3.client("dsql", region_name=region)

    def generate_token() -> str:
        return cast(
            str,
            client.generate_db_connect_admin_auth_token(
                Hostname=hostname,
                Region=region,
                ExpiresIn=900,
            ),
        )

    return generate_token


_RESERVATION_KIND = "reservation"
_RESERVATION_IDEMPOTENCY_KIND = "reservation_idempotency"
_RESERVATION_FINALIZATION_KIND = "reservation_finalization"


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
    without_comments = "\n".join(
        line.split("--", 1)[0] for line in schema.splitlines()
    )
    return [stmt.strip() for stmt in without_comments.split(";") if stmt.strip()]


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
                token_provider=_aws_dsql_token_provider(hostname, region),
                min_size=pool_min_size,
                max_size=pool_max_size,
                open=True,
            )
        else:
            raise ValueError(
                "Unsupported TR_POSTGRES_IAM_AUTH value "
                f"{postgres_iam_auth!r}; expected 'aws-dsql' or empty"
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
        conn.execute(
            statement.replace("CREATE INDEX", "CREATE INDEX ASYNC", 1), prepare=False
        )

    # Generic entity IO ------------------------------------------------------

    def _run_transaction(self, operation: Callable[[Any], T]) -> T:
        last_serialization_error: BaseException | None = None
        for _attempt in range(self._transaction_attempts):
            try:
                with self._pool.connection() as conn:
                    with conn.transaction():
                        return operation(conn)
            except psycopg.errors.TransactionRollback as exc:
                # Covers SerializationFailure (40001) *and* DeadlockDetected
                # (40P01). Both mean "the server rolled you back, try again",
                # and both are routine: 40001 is how Aurora DSQL reports every
                # optimistic-concurrency abort, and 40P01 is ordinary Postgres
                # lock ordering. Retrying only the first would surface
                # deadlocks to callers as hard failures.
                #
                # Safe to retry because the transaction rolled back whole: an
                # idempotency row inserted on the failed attempt is gone, so
                # the retry re-inserts and credits exactly once.
                last_serialization_error = exc
                continue
            except psycopg.IntegrityError as exc:
                raise StoreConflict("Postgres write conflict") from exc
            except psycopg.Error as exc:
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
        return self._run_transaction(
            lambda conn: self._read_entity_tx(conn, kind, entity_id, cls)
        )

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
        initial_total = 0 if trial_credit_microdollars is None else int(
            trial_credit_microdollars
        )
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
            utcnow() + dt.timedelta(seconds=max(ttl_seconds, 60))
        ).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _not_implemented(method: str) -> Never:
        raise NotImplementedError(
            f"PostgresStore.{method} is not implemented in increment 1"
        )

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
                    **(
                        json.loads(row[0])
                        if isinstance(row[0], str)
                        else dict(row[0])
                    )
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

    def create_acquisition_attribution(
        self, record: AcquisitionAttribution
    ) -> bool:
        self._not_implemented("create_acquisition_attribution")

    def get_acquisition_attribution(
        self, workspace_id: str
    ) -> AcquisitionAttribution | None:
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

    def list_google_ads_conversions(
        self,
        *,
        since: str,
        limit: int,
    ) -> list[GoogleAdsConversion]:
        self._not_implemented("list_google_ads_conversions")

    def backfill_google_ads_conversions(self, *, limit: int) -> int:
        self._not_implemented("backfill_google_ads_conversions")

    def claim_google_ads_deliveries(
        self,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[GoogleAdsConversion]:
        self._not_implemented("claim_google_ads_deliveries")

    def mark_google_ads_delivery_submitted(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        request_id: str,
    ) -> GoogleAdsConversion | None:
        self._not_implemented("mark_google_ads_delivery_submitted")

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
        self._not_implemented("mark_google_ads_delivery_failed")

    def repair_google_ads_delivery_queue(self, *, since: str, limit: int) -> int:
        self._not_implemented("repair_google_ads_delivery_queue")

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

    def upgrade_auth_session(
        self, raw_token: str, *, state: str
    ) -> AuthSession | None:
        self._not_implemented("upgrade_auth_session")

    def set_auth_session_workspace(
        self, raw_token: str, workspace_id: str
    ) -> AuthSession | None:
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
            return self._delete_entity_tx(
                conn,
                "auth_session_lookup",
                lookup_hash,
            ) == 1

        return self._run_transaction(delete)

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

    def consume_wallet_challenge(
        self, raw_nonce: str
    ) -> WalletChallenge | None:
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

    def consume_oauth_authorization_code(
        self, raw_code: str
    ) -> OAuthAuthorizationCode | None:
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

    def typed_key_usage(self, key_hash: str) -> dict[str, Any] | None:
        self._not_implemented("typed_key_usage")

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
                "DELETE FROM tr_key_limit "
                "WHERE workspace_id = %s AND key_hash = %s",
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
        self._not_implemented("reserve_key_limit")

    def settle_key_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        actual_microdollars: int,
        *,
        usage_type: UsageType | str,
    ) -> None:
        self._not_implemented("settle_key_limit")

    def refund_key_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: UsageType | str,
    ) -> None:
        self._not_implemented("refund_key_limit")

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

    def list_byok_providers(
        self, workspace_id: str
    ) -> list[ByokProviderConfig]:
        self._not_implemented("list_byok_providers")

    def get_byok_provider(
        self,
        workspace_id: str,
        provider: str,
    ) -> ByokProviderConfig | None:
        self._not_implemented("get_byok_provider")

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

    def list_custom_models_for_user(
        self, owner_user_id: str
    ) -> list[CustomModel]:
        self._not_implemented("list_custom_models_for_user")

    def get_custom_model(self, model_id: str) -> CustomModel | None:
        self._not_implemented("get_custom_model")

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

    def list_broadcast_destinations(
        self, workspace_id: str
    ) -> list[BroadcastDestination]:
        self._not_implemented("list_broadcast_destinations")

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

    def due_broadcast_deliveries(
        self, *, limit: int = 100
    ) -> list[BroadcastDeliveryJob]:
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

    # Credit ledger ----------------------------------------------------------

    def get_credit_account(
        self, workspace_id: str
    ) -> CreditAccount | None:
        self._not_implemented("get_credit_account")

    def credit_workspace_typed_direct(
        self,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
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
            cursor = conn.execute(
                "UPDATE tr_credit_balance "
                "SET total_credits = total_credits + %s, "
                "source_updated_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE workspace_id = %s AND shard = 0",
                (int(amount_microdollars), workspace_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"missing authoritative tr_credit_balance for workspace {workspace_id}"
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
                        raise RuntimeError(
                            "reservation idempotency row disappeared after conflict"
                        )
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

    def clear_stripe_payment_method(
        self, workspace_id: str
    ) -> CreditAccount | None:
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
        additional_cost_reservation_microdollars: int = 0,
    ) -> GatewayAuthorization:
        self._not_implemented("create_gateway_authorization")

    def get_gateway_authorization(
        self, authorization_id: str
    ) -> GatewayAuthorization | None:
        self._not_implemented("get_gateway_authorization")

    def get_gateway_authorization_by_idempotency_key(
        self,
        workspace_id: str,
        key_hash: str,
        idempotency_key: str,
    ) -> GatewayAuthorization | None:
        self._not_implemented(
            "get_gateway_authorization_by_idempotency_key"
        )

    def mark_gateway_authorization_settled(
        self, authorization_id: str
    ) -> None:
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
        self._not_implemented("finalize_gateway_authorization")

    # Generations + activity -------------------------------------------------

    def add_generation(self, generation: Generation) -> None:
        self._not_implemented("add_generation")

    def record_provider_benchmark(
        self, sample: ProviderBenchmarkSample
    ) -> None:
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
                known = {
                    field.name
                    for field in dataclasses.fields(ProviderBenchmarkSample)
                }
                samples.append(
                    ProviderBenchmarkSample(
                        **{
                            key: value
                            for key, value in data.items()
                            if key in known
                        }
                    )
                )
            return samples

        return self._run_transaction(list_samples)

    def record_synthetic_probe_sample(
        self, sample: SyntheticProbeSample
    ) -> None:
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
                    raise StoreConflict(
                        "Synthetic rollup disappeared during update"
                    )
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
            predicates = [
                "kind = %s",
                "indexed_at >= %s",
            ]
            params: list[Any] = [
                "synthetic_probe",
                utcnow() - dt.timedelta(days=RAW_SYNTHETIC_RETENTION_DAYS),
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
            return [
                _dataclass_from_json(row[0], SyntheticProbeSample)
                for row in rows
            ]

        return self._run_transaction(list_samples)

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
            rollups = [
                _dataclass_from_json(row[0], SyntheticRollup)
                for row in rows
            ]
            if not include_histograms:
                return [
                    dataclasses.replace(
                        rollup,
                        latency_histogram={},
                        ttfb_histogram={},
                    )
                    for rollup in rollups
                ]
            return rollups

        return self._run_transaction(list_rollups)

    def get_generation(self, generation_id: str) -> Generation | None:
        self._not_implemented("get_generation")

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
    known = {
        field.name
        for field in dataclasses.fields(cast(Any, cls))
    }
    return cls(
        **{
            key: value
            for key, value in data.items()
            if key in known
        }
    )


def _parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _rollup_retention_cutoff(now: dt.datetime) -> dt.datetime:
    current = now.astimezone(dt.UTC)
    cutoff_month = (
        current.year * 12
        + current.month
        - 1
        - ROLLUP_RETENTION_MONTHS
        + 1
    )
    year, zero_based_month = divmod(cutoff_month, 12)
    return dt.datetime(year, zero_based_month + 1, 1, tzinfo=dt.UTC)
