"""Aurora PostgreSQL Store implementation for trustedrouter.eu.

The trustedrouter.eu stack runs entirely on AWS Frankfurt
(eu-central-1) with no GCP dependencies — see
docs/plans/trustedrouter-eu.md for the full architecture rationale.

This module is the PostgreSQL-backed equivalent of storage_gcp.py.
Same Store Protocol contract, same entity-kind taxonomy, same JSON
body shapes, same idempotency semantics (the audit in
tests/test_credit_ledger_idempotency_audit.py applies unchanged).

The schema is the single table `tr_entities (kind, id, body, updated_at)`
mirroring Spanner's shape — see infra-aws/migrations/0001_initial.sql.
PostgreSQL transactional reads-modify-writes substitute cleanly for
Spanner's `database.run_in_transaction()`: `engine.begin()` opens a
serializable transaction, the body runs, commit on success / rollback
on exception. Behavior is the same.

This is Phase 1 of the trustedrouter.eu plan. Phase 1 day 1 ships:
  * The class skeleton (every method raises NotImplementedError)
  * ONE working method (`ensure_user`) end-to-end against local
    Docker Postgres
  * Schema migration in infra-aws/migrations/0001_initial.sql

Subsequent days port methods in batches of ~10 each, each batch
landing as a PR with matching test coverage from the existing
705-test suite. Each method's GCP implementation in storage_gcp.py
serves as the reference; the AWS port keeps the same return types and
the same idempotency contract, just swaps the storage primitives.
"""

from __future__ import annotations

import json
import logging
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
from trusted_router.types import UsageType

T = TypeVar("T")
log = logging.getLogger(__name__)


def _normalize_email(value: str) -> str:
    """Mirror storage_gcp.py's _normalize_email — emails are lower-cased
    and missing-@ values get the trustedrouter.local suffix."""
    normalized = value.strip().lower()
    if "@" not in normalized:
        normalized = f"{normalized}@trustedrouter.local"
    return normalized


class PostgresStore:
    """Aurora PostgreSQL implementation of the Store Protocol.

    Drives the trustedrouter.eu stack on AWS Frankfurt. See module
    docstring for rationale + plan reference.

    Construction:
        PostgresStore(database_url="postgresql://user:pass@host/db")

    Local dev:
        docker compose up -d postgres
        export TR_STORAGE_BACKEND=aws-postgres
        export TR_AWS_POSTGRES_URL=postgresql://tr:tr@localhost:5433/trustedrouter
    """

    def __init__(self, database_url: str) -> None:
        # Lazy import sqlalchemy + psycopg so the GCP-only test/runtime
        # path doesn't need either installed. When this constructor is
        # actually invoked the dependencies must be importable; the
        # import error is the most informative failure mode for an
        # operator misconfigured to use this backend without the deps.
        try:
            from sqlalchemy import create_engine
        except ImportError as exc:  # pragma: no cover - operator guidance
            raise RuntimeError(
                "PostgresStore requires sqlalchemy + psycopg. Install with "
                "`uv pip install 'sqlalchemy[asyncio]>=2' 'psycopg[binary]>=3'`. "
                "If you don't intend to use the AWS backend, set "
                "TR_STORAGE_BACKEND=memory or spanner-bigtable instead."
            ) from exc

        # `psycopg` (3.x) is the modern driver — explicitly request it
        # in the URL so SQLAlchemy doesn't try the older `psycopg2`.
        # Operators may pass either shape; normalize here.
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )

        # Engine settings tuned for Aurora Serverless v2:
        #   pool_pre_ping=True   — drops connections that Aurora rotated
        #                          out from under us (Aurora reaps idle
        #                          connections aggressively at low ACUs)
        #   pool_recycle=600     — refresh every 10 min, well under
        #                          Aurora's idle timeout
        #   isolation_level="SERIALIZABLE" — match Spanner's strong-
        #                          read default so the GCP port doesn't
        #                          surface unexpected race conditions.
        self._engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=600,
            isolation_level="SERIALIZABLE",
            future=True,
        )

    # ── Generic entity I/O (Spanner's _read_entity_tx / _write_entity_tx) ─
    #
    # In storage_gcp.py these live on SpannerIO and are awaited inside
    # `database.run_in_transaction(txn_fn)`. Here we expose plain
    # connection-bound helpers; per-method transactional consistency is
    # achieved by wrapping the helpers in `with self._engine.begin() as
    # conn:` blocks. The two patterns are semantically equivalent for
    # our usage (all our txns are short and single-roundtrip-shaped).

    def _read_entity(
        self, conn: Any, kind: str, ident: str, model: type[T]
    ) -> T | None:
        from sqlalchemy import text

        row = conn.execute(
            text(
                "SELECT body FROM tr_entities WHERE kind = :kind AND id = :id"
            ),
            {"kind": kind, "id": ident},
        ).first()
        if row is None:
            return None
        body = row[0]
        # psycopg returns JSONB as a Python dict directly. The model
        # types are dataclasses constructed from kwargs — same shape
        # storage_gcp uses (it json.loads STRING(MAX) into a dict
        # before constructing the dataclass).
        if isinstance(body, str):  # belt-and-suspenders if a future
            body = json.loads(body)  # column type changes to TEXT
        return model(**body)

    def _write_entity(
        self, conn: Any, kind: str, ident: str, value: Any
    ) -> None:
        from sqlalchemy import text

        body = (
            value.__dict__ if hasattr(value, "__dict__") else dict(value)
        )
        conn.execute(
            text(
                "INSERT INTO tr_entities (kind, id, body) "
                "VALUES (:kind, :id, CAST(:body AS jsonb)) "
                "ON CONFLICT (kind, id) DO UPDATE SET "
                "body = EXCLUDED.body, updated_at = NOW()"
            ),
            {
                "kind": kind,
                "id": ident,
                "body": json.dumps(body, default=str),
            },
        )

    # ── Users (Phase 1 day 1: ensure_user is the first working method) ──

    def reset(self) -> None:
        """Test-only reset. Wipes the entire tr_entities table — only
        safe in local dev with the disposable Docker Postgres."""
        from sqlalchemy import text

        with self._engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE tr_entities"))

    def ensure_user(
        self, user_id: str, email: str | None = None
    ) -> User:
        """Create-or-return a user by id. If a user with this id exists,
        return them (optionally updating email if newly provided). If
        not, create with sensible defaults.

        Mirrors storage_gcp.SpannerBigtableStore.ensure_user — same
        idempotency: calling with the same user_id twice returns the
        same User and does NOT recreate it.
        """
        with self._engine.begin() as conn:
            existing = self._read_entity(conn, "user", user_id, User)
            if existing is not None:
                if email and not existing.email:
                    # Backfill email on the existing record if we
                    # didn't have one before.
                    existing.email = _normalize_email(email)
                    self._write_entity(conn, "user", user_id, existing)
                return existing
            user = User(
                id=user_id,
                email=_normalize_email(email) if email else None,
                email_verified=False,
                wallet_address=None,
                created_at=iso_now(),
            )
            self._write_entity(conn, "user", user_id, user)
            return user

    # ── Everything else: NotImplementedError until Phase 1 day N ────────
    #
    # The methods below are stubs that match the Protocol signature so
    # `create_store(settings)` returning a PostgresStore type-checks
    # against the Store protocol. Each batch of method ports lands as
    # its own PR; the ordering in subsequent PRs:
    #
    #   PR 2: workspaces + members (signup is the integration test)
    #   PR 3: auth sessions + verification tokens
    #   PR 4: api keys + key limits
    #   PR 5: credits + reservations + settle/refund
    #   PR 6: stripe events (idempotency-keyed credit grants)
    #   PR 7: gateway authorizations (idempotency-keyed)
    #   PR 8: BYOK + envelope encryption
    #   PR 9: broadcast + generations
    #   PR 10: rate limits + email blocks + synthetic monitoring
    #
    # Each PR runs the existing 705-test suite with
    # TR_STORAGE_BACKEND=aws-postgres pointed at local Postgres, gating
    # the PR on test parity with the Spanner backend. The Stripe
    # webhook handler's regression test, the credit-ledger idempotency
    # audit, and the OAuth/console tests are the most important parity
    # checks — they catch any subtle dedup-semantics drift.

    def _not_yet(self, name: str) -> Any:
        """Single chokepoint for the not-yet-ported error so future
        operators see one consistent message across the stubs."""
        raise NotImplementedError(
            f"PostgresStore.{name} not yet ported — see "
            f"docs/plans/trustedrouter-eu.md Phase 1 schedule. "
            f"Reference implementation: storage_gcp.SpannerBigtableStore."
            f"{name}"
        )

    # User-shaped
    def find_user_by_email(self, email: str) -> User | None:
        self._not_yet("find_user_by_email")

    def find_user_by_wallet(self, address: str) -> User | None:
        self._not_yet("find_user_by_wallet")

    def create_wallet_user(self, address: str) -> User:
        self._not_yet("create_wallet_user")

    def set_user_email(self, user_id: str, email: str) -> User | None:
        self._not_yet("set_user_email")

    def mark_user_email_verified(self, user_id: str) -> User | None:
        self._not_yet("mark_user_email_verified")

    def get_user(self, user_id: str) -> User | None:
        self._not_yet("get_user")

    def signup(
        self,
        email: str,
        workspace_name: str | None = None,
    ) -> SignupResult | None:
        self._not_yet("signup")

    # The remaining ~70 methods are also stubs. Listed here as
    # placeholder NotImplementedError-raisers so create_store(...)
    # type-checks; future PRs port them in batches.
    #
    # Rather than inlining all 80 stubs (which would obscure the day-1
    # `ensure_user` implementation), `__getattr__` returns a generic
    # raise-NotImplementedError shim for any unknown attribute. This is
    # safe because the Store Protocol's surface is well-defined — any
    # attribute miss is by definition an un-ported method, and the
    # shim's error message points the operator to the GCP reference.

    def __getattr__(self, name: str) -> Any:
        # `__getattr__` is only called when normal attribute lookup
        # fails — i.e., the method isn't yet implemented above. Wrap
        # the call in a closure so callers get the same TypeError
        # signature they'd get from a missing positional arg, but with
        # a clear "not yet ported" message in the body.
        if name.startswith("_"):
            # Internal attributes (_engine, etc.) that genuinely don't
            # exist should still raise AttributeError — that's a real
            # programming error, not a port-status issue.
            raise AttributeError(name)

        def _stub(*_args: Any, **_kwargs: Any) -> Any:
            raise NotImplementedError(
                f"PostgresStore.{name} not yet ported — see "
                f"docs/plans/trustedrouter-eu.md Phase 1 schedule. "
                f"Reference: storage_gcp.SpannerBigtableStore.{name}"
            )
        return _stub
