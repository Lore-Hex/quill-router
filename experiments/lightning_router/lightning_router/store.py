import hashlib
import hmac
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    insert,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from .credentials import Credentials
from .money import MAX_MICRODOLLARS, MAX_MSATS, msats
from .rates import Rate

metadata = MetaData()
settings = Table("lr_settings", metadata, Column("id", String(32), primary_key=True),
                 Column("value", String(64), nullable=False))
checkouts = Table(
    "lr_checkouts", metadata,
    Column("key_hash", String(64), primary_key=True),
    Column("credit_account_id", String(128), nullable=True),
    Column("pending_key", LargeBinary, nullable=True),
    Column("created_at", BigInteger, nullable=False, server_default="0"),
    CheckConstraint("(credit_account_id IS NULL AND pending_key IS NOT NULL) OR "
                    "(credit_account_id IS NOT NULL AND pending_key IS NULL)"),
)
invoices = Table(
    "lr_invoices", metadata,
    Column("id", String(32), primary_key=True),
    Column("key_hash", String(64), ForeignKey("lr_checkouts.key_hash"), nullable=False),
    Column("request_id", String(32), nullable=False),
    Column("payment_hash", String(64), nullable=False, unique=True),
    Column("bolt11", String(8192), nullable=False, default=""),
    Column("state", String(12), nullable=False, default="OPEN"),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("amount_msat", BigInteger, nullable=False, default=0),
    Column("requested_msat", BigInteger, nullable=False),
    Column("usd_cents", BigInteger, nullable=False),
    Column("usd_per_btc", String(40), nullable=False),
    Column("fx_margin_bps", Integer, CheckConstraint("fx_margin_bps >= 0 AND fx_margin_bps < 10000"), nullable=False, server_default="0"),
    Column("settle_index", BigInteger, nullable=True),
    Column("settled_at", BigInteger, nullable=True),
    Column("failure_code", String(32), nullable=False, server_default=""),
    Column("failure_since", BigInteger, nullable=False, server_default="0"),
    Column("next_attempt_at", BigInteger, nullable=False, server_default="0"),
    Column("credit_microdollars", BigInteger, nullable=False, default=0),
    Column("credited_at", BigInteger, nullable=True),
    Column("last_checked", BigInteger, nullable=False, default=0),
    UniqueConstraint("key_hash", "request_id"),
    CheckConstraint("state IN ('OPEN', 'ACCEPTED', 'SETTLED', 'CANCELED')"),
    CheckConstraint(f"amount_msat >= 0 AND amount_msat <= {MAX_MSATS}"),
    CheckConstraint(f"credit_microdollars >= 0 AND credit_microdollars <= {MAX_MICRODOLLARS}"),
)
Index("lr_invoice_reconcile", invoices.c.state, invoices.c.last_checked, invoices.c.id)
Index("lr_invoice_credit_age", invoices.c.state, invoices.c.credited_at, invoices.c.created_at)
Index("lr_invoice_failure", invoices.c.failure_code, invoices.c.failure_since)
deposits = Table(
    "lr_deposits", metadata,
    Column("payment_hash", String(64), primary_key=True),
    Column("key_hash", String(64), ForeignKey("lr_checkouts.key_hash"), nullable=False),
    Column("amount_msat", BigInteger, nullable=False),
    Column("credit_microdollars", BigInteger, nullable=False),
    Column("usd_per_btc", String(40), nullable=False),
    Column("settled_at", BigInteger, nullable=False),
    CheckConstraint(f"amount_msat > 0 AND amount_msat <= {MAX_MSATS}"),
    CheckConstraint(f"credit_microdollars > 0 AND credit_microdollars <= {MAX_MICRODOLLARS}"),
)
limits = Table(
    "lr_rate_limits", metadata,
    Column("id", String(96), primary_key=True),
    Column("window", BigInteger, nullable=False),
    Column("count", Integer, nullable=False),
    Column("available_at", BigInteger, nullable=False, server_default="0"),
)


class Store:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def configure_sqlite(dbapi_connection: Any, _: Any) -> None:
                dbapi_connection.isolation_level = None
                dbapi_connection.execute("PRAGMA foreign_keys=ON")
                dbapi_connection.execute("PRAGMA busy_timeout=10000")

            @event.listens_for(self.engine, "begin")
            def begin_sqlite(connection: Connection) -> None:
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    def migrate(self) -> None:
        # Add columns before creating new indexes on an existing table.
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                conn.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                conn.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
            additions = {
                "lr_checkouts": {"created_at": "BIGINT NOT NULL DEFAULT 0"},
                "lr_invoices": {"settled_at": "BIGINT", "failure_code": "VARCHAR(32) NOT NULL DEFAULT ''",
                                "failure_since": "BIGINT NOT NULL DEFAULT 0", "next_attempt_at": "BIGINT NOT NULL DEFAULT 0",
                                "fx_margin_bps": "INTEGER NOT NULL DEFAULT 0 CHECK (fx_margin_bps >= 0 AND fx_margin_bps < 10000)"},
                "lr_rate_limits": {"available_at": "BIGINT NOT NULL DEFAULT 0"},
            }
            for table, columns in additions.items():
                if not inspect(conn).has_table(table):
                    continue
                current = {col["name"] for col in inspect(conn).get_columns(table)}
                for name, definition in columns.items():
                    if name not in current:
                        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                        if table == "lr_checkouts" and name == "created_at":
                            conn.execute(update(checkouts).values(created_at=int(time.time())))
            if inspect(conn).has_table("lr_invoices"):
                for constraint in inspect(conn).get_unique_constraints("lr_invoices"):
                    if constraint["column_names"] == ["settle_index"]:
                        if self.engine.dialect.name == "sqlite":
                            # SQLite cannot drop a UNIQUE constraint. Rebuild
                            # atomically; no table references invoices by FK.
                            replacement_metadata = MetaData()
                            checkouts.to_metadata(replacement_metadata)
                            replacement = invoices.to_metadata(replacement_metadata, name="lr_invoices_upgrade")
                            replacement.indexes.clear()
                            replacement.create(conn)
                            conn.execute(insert(replacement).from_select(list(invoices.c.keys()), select(invoices)))
                            conn.exec_driver_sql("DROP TABLE lr_invoices")
                            conn.exec_driver_sql("ALTER TABLE lr_invoices_upgrade RENAME TO lr_invoices")
                            break
                        constraint_name = constraint["name"]
                        if not constraint_name:
                            raise ValueError("Unnamed settlement index constraint")
                        quoted = conn.dialect.identifier_preparer.quote(constraint_name)
                        conn.exec_driver_sql(f"ALTER TABLE lr_invoices DROP CONSTRAINT {quoted}")
            metadata.create_all(conn)
            for index in invoices.indexes:
                index.create(conn, checkfirst=True)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    def _account(self, conn: Connection, key_hash: str) -> Any:
        # PostgreSQL locks serialize invoice creation and settlement per key.
        # SQLite uses BEGIN IMMEDIATE; both run the identical contract tests.
        row = conn.execute(select(checkouts).where(checkouts.c.key_hash == key_hash).with_for_update()).mappings().first()
        if row is None:
            raise KeyError("Unknown API key")
        return row

    def _insert_once(self, conn: Connection, table: Table, values: dict[str, Any]) -> None:
        statement = pg_insert(table) if self.engine.dialect.name == "postgresql" else sqlite_insert(table)
        conn.execute(statement.values(**values).on_conflict_do_nothing())

    def bind_account(self, key_hash: str, credit_account_id: str) -> None:
        if not credit_account_id or len(credit_account_id) > 128:
            raise ValueError("Invalid credit account")
        with self.transaction() as conn:
            self._insert_once(conn, checkouts, {"key_hash": key_hash, "credit_account_id": credit_account_id, "created_at": int(time.time())})
            row = self._account(conn, key_hash)
            if row["credit_account_id"] not in {None, credit_account_id}:
                raise ValueError("API key cannot change credit accounts")
            conn.execute(update(checkouts).where(checkouts.c.key_hash == key_hash).values(
                credit_account_id=credit_account_id, pending_key=None,
            ))

    def pin_credentials(self, credentials: Credentials) -> None:
        with self.transaction() as conn:
            # Validate existing recovery material before establishing the first pin.
            prior = conn.execute(select(invoices.c.id, invoices.c.payment_hash).limit(1)).first()
            if prior and not hmac.compare_digest(hashlib.sha256(credentials.invoice_preimage(prior.id)).hexdigest(), prior.payment_hash):
                raise ValueError("Checkout secret changed; restore the existing version")
            pending = conn.execute(select(checkouts.c.pending_key, checkouts.c.key_hash).where(checkouts.c.pending_key.is_not(None)).limit(1)).first()
            if pending:
                credentials.open_pending_key(pending.pending_key, pending.key_hash)
            self._insert_once(conn, settings, {"id": "checkout-key", "value": credentials.key_id()})
            pinned = conn.execute(select(settings.c.value).where(settings.c.id == "checkout-key")).scalar_one()
            if not hmac.compare_digest(pinned, credentials.key_id()):
                raise ValueError("Checkout secret changed; restore the existing version")

    def prepare_checkout(self, key_hash: str, pending_key: bytes) -> None:
        # This is only invoice ownership/recovery metadata, not a TR identity.
        with self.transaction() as conn:
            self._insert_once(conn, checkouts, {"key_hash": key_hash, "pending_key": pending_key, "created_at": int(time.time())})
            self._account(conn, key_hash)
            conn.execute(update(checkouts).where(checkouts.c.key_hash == key_hash).values(created_at=int(time.time())))

    def checkout(self, key_hash: str) -> dict[str, Any]:
        with self.transaction() as conn:
            return dict(self._account(conn, key_hash))

    def credit_account(self, key_hash: str) -> str | None:
        with self.transaction() as conn:
            account_id = self._account(conn, key_hash)["credit_account_id"]
            return str(account_id) if account_id is not None else None

    def active(self, key_hash: str) -> str | None:
        with self.transaction() as conn:
            return conn.execute(select(invoices.c.id).where(
                invoices.c.key_hash == key_hash, invoices.c.state.in_(["OPEN", "ACCEPTED"]),
            ).limit(1)).scalar_one_or_none()

    def by_request(self, key_hash: str, request_id: str) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(select(invoices).where(
                invoices.c.key_hash == key_hash, invoices.c.request_id == request_id,
            )).mappings().first()
            return dict(row) if row else None

    def prepare(self, key_hash: str, request_id: str, invoice_id: str,
                payment_hash: str, now: int, *, requested_msat: int,
                usd_cents: int, usd_per_btc: str, fx_margin_bps: int = 0) -> dict[str, Any]:
        msats(requested_msat)
        Rate(Decimal(usd_per_btc), now, fx_margin_bps)
        if requested_msat <= 0 or not 1 <= usd_cents <= 100_000:
            raise ValueError("Invalid invoice amount")
        with self.transaction() as conn:
            self._account(conn, key_hash)
            previous = conn.execute(select(invoices).where(
                invoices.c.key_hash == key_hash, invoices.c.request_id == request_id,
            )).mappings().first()
            if previous:
                if previous["usd_cents"] != usd_cents:
                    raise ValueError("Idempotency key reused with a different amount")
                return dict(previous)
            # At most one unresolved invoice per key, including expired invoices
            # with an HTLC still in flight. Explicit LND cancellation resolves it.
            active = conn.execute(select(invoices.c.id).where(
                invoices.c.key_hash == key_hash, invoices.c.state.in_(["OPEN", "ACCEPTED"]),
            )).first()
            if active:
                raise ValueError("Resolve the existing invoice first")
            conn.execute(insert(invoices).values(
                id=invoice_id, key_hash=key_hash, request_id=request_id,
                payment_hash=payment_hash, created_at=now, expires_at=now + 900,
                requested_msat=requested_msat, usd_cents=usd_cents, usd_per_btc=usd_per_btc,
                fx_margin_bps=fx_margin_bps,
            ))
            return dict(conn.execute(select(invoices).where(invoices.c.id == invoice_id)).mappings().one())

    def invoice(self, invoice_id: str, key_hash: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(select(invoices).where(
                invoices.c.id == invoice_id, invoices.c.key_hash == key_hash,
            )).mappings().first()
            if row is None:
                raise KeyError("Unknown invoice")
            return dict(row)

    def attach(self, invoice_id: str, bolt11: str, expires_at: int) -> None:
        with self.transaction() as conn:
            conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(
                bolt11=bolt11, expires_at=expires_at,
            ))

    def expire_unissued(self, invoice_id: str) -> None:
        # A BOLT11 is only shown after attach+observe. This conditional update
        # cannot cancel a previously published invoice from a stale caller.
        with self.transaction() as conn:
            conn.execute(update(invoices).where(
                invoices.c.id == invoice_id, invoices.c.bolt11 == "",
                invoices.c.state == "OPEN",
            ).values(state="CANCELED"))

    def observe(self, invoice_id: str, *, state: str, payment_hash: str,
                amount_msat: int, settle_index: int, now: int) -> None:
        if state not in {"OPEN", "ACCEPTED", "SETTLED", "CANCELED"}:
            raise ValueError("Unknown LND invoice state")
        amount = msats(amount_msat)
        settle_index = msats(settle_index)
        with self.transaction() as conn:
            # Lock order is always account then invoice, including concurrent
            # browser polling, cancellation, and background reconciliation.
            owner = conn.execute(select(invoices.c.key_hash).where(invoices.c.id == invoice_id)).scalar_one()
            self._account(conn, owner)
            row = conn.execute(select(invoices).where(invoices.c.id == invoice_id).with_for_update()).mappings().one()
            if row["payment_hash"] != payment_hash:
                raise ValueError("LND returned a different payment hash")
            if row["state"] == "SETTLED":
                if state == "SETTLED" and (row["amount_msat"] != amount or row["settle_index"] != settle_index):
                    raise ValueError("Settlement replay changed amount or index")
                return
            if row["state"] == "CANCELED":
                if state == "SETTLED":
                    raise ValueError("Canceled invoice unexpectedly settled")
                return
            values: dict[str, Any] = {"state": state, "last_checked": now, "failure_code": "", "failure_since": 0, "next_attempt_at": 0}
            if state == "SETTLED":
                if amount < row["requested_msat"] or settle_index <= 0:
                    raise ValueError("Invalid settled invoice")
                credit = Rate(Decimal(row["usd_per_btc"]), row["created_at"]).credit_microdollars(amount)
                if credit <= 0:
                    raise ValueError("Settled invoice has no USD value")
                conn.execute(insert(deposits).values(
                    payment_hash=payment_hash, key_hash=owner, amount_msat=amount, settled_at=now,
                    credit_microdollars=credit, usd_per_btc=row["usd_per_btc"],
                ))
                values.update(amount_msat=amount, settle_index=settle_index, credit_microdollars=credit, settled_at=now)
            elif row["state"] == "ACCEPTED" and state == "OPEN":
                values["state"] = "ACCEPTED"
            conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(**values))

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute(select(invoices).where(
                or_(invoices.c.state.in_(["OPEN", "ACCEPTED"]),
                    (invoices.c.state == "SETTLED") & invoices.c.credited_at.is_(None)),
            ).where(invoices.c.next_attempt_at <= int(time.time())).order_by(invoices.c.last_checked, invoices.c.id).limit(min(limit, 100))).mappings()]

    def mark_credited(self, invoice_id: str, now: int) -> None:
        with self.transaction() as conn:
            conn.execute(update(invoices).where(
                invoices.c.id == invoice_id, invoices.c.state == "SETTLED",
                invoices.c.credited_at.is_(None),
            ).values(credited_at=now, failure_code="", failure_since=0, next_attempt_at=0))

    def failed(self, invoice_id: str, code: str, now: int, *, review: bool) -> None:
        with self.transaction() as conn:
            row = conn.execute(select(invoices).where(invoices.c.id == invoice_id).with_for_update()).mappings().one()
            if row["credited_at"] is not None:
                return
            conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(
                failure_code=code, failure_since=row["failure_since"] or now,
                next_attempt_at=now + 300 if review else 0,
            ))

    def delivery_health(self, now: int) -> dict[str, int]:
        with self.transaction() as conn:
            uncredited, oldest = conn.execute(select(func.count(), func.min(func.coalesce(invoices.c.settled_at, invoices.c.created_at))).where(
                invoices.c.state == "SETTLED", invoices.c.credited_at.is_(None),
            )).one()
            reviews = conn.execute(select(func.count()).select_from(invoices).where(
                invoices.c.failure_code.not_in(["", "credit_unavailable", "invoice_unavailable"]), invoices.c.credited_at.is_(None),
            )).scalar_one()
        return {"uncredited_count": uncredited, "oldest_uncredited_seconds": max(0, now - oldest) if oldest is not None else 0,
                "review_required": reviews}

    def prune(self, now: int, *, limit: int = 100) -> dict[str, int]:
        limit = min(max(1, limit), 100)
        cutoff = now - 30 * 86400
        with self.transaction() as conn:
            candidates = list(conn.execute(select(invoices.c.id, invoices.c.key_hash).where(
                invoices.c.state == "CANCELED", invoices.c.amount_msat == 0,
                invoices.c.failure_code == "", invoices.c.expires_at < cutoff,
            ).order_by(invoices.c.expires_at, invoices.c.id).limit(limit)))
            removed = 0
            for invoice_id, owner in candidates:
                conn.execute(select(checkouts.c.key_hash).where(checkouts.c.key_hash == owner).with_for_update()).first()
                deleted = conn.execute(delete(invoices).where(invoices.c.id == invoice_id, invoices.c.state == "CANCELED",
                    invoices.c.amount_msat == 0, invoices.c.failure_code == "", invoices.c.expires_at < cutoff).returning(invoices.c.id)).first()
                removed += int(deleted is not None)
            owners = list(conn.execute(select(checkouts.c.key_hash).where(
                checkouts.c.credit_account_id.is_(None), checkouts.c.created_at > 0, checkouts.c.created_at < cutoff,
                ~select(invoices.c.id).where(invoices.c.key_hash == checkouts.c.key_hash).exists(),
                ~select(deposits.c.payment_hash).where(deposits.c.key_hash == checkouts.c.key_hash).exists(),
            ).limit(limit)).scalars())
            # Lock ownership against concurrent invoice creation before rechecking.
            for owner in owners:
                conn.execute(select(checkouts.c.key_hash).where(checkouts.c.key_hash == owner).with_for_update()).first()
                conn.execute(delete(checkouts).where(checkouts.c.key_hash == owner,
                    checkouts.c.credit_account_id.is_(None),
                    checkouts.c.created_at > 0, checkouts.c.created_at < cutoff,
                    ~select(invoices.c.id).where(invoices.c.key_hash == owner).exists(),
                    ~select(deposits.c.payment_hash).where(deposits.c.key_hash == owner).exists()))
            stale = list(conn.execute(select(limits.c.id).where(
                limits.c.available_at < now - 900, limits.c.window < now // 900 - 1,
            ).limit(limit)).scalars())
            if stale:
                conn.execute(delete(limits).where(limits.c.id.in_(stale), limits.c.available_at < now - 900, limits.c.window < now // 900 - 1))
        return {"invoices": removed, "rate_limits": len(stale)}

    def checked(self, invoice_id: str, now: int) -> None:
        with self.transaction() as conn:
            conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(last_checked=now))

    def rate_limit(self, identity: str, now: int, maximum: int = 10) -> bool:
        if not 1 <= maximum <= 900:
            raise ValueError("Invalid rate limit")
        window = now // 900
        interval = (900 + maximum - 1) // maximum
        with self.transaction() as conn:
            self._insert_once(conn, limits, {"id": identity, "window": window, "count": 0})
            row = conn.execute(select(limits).where(limits.c.id == identity).with_for_update()).mappings().one()
            available = row["available_at"]
            if available > now + (maximum - 1) * interval:
                return False
            count = row["count"] if row["window"] == window else 0
            conn.execute(update(limits).where(limits.c.id == identity).values(window=window, count=count + 1,
                                                                          available_at=max(now, available) + interval))
            return True
