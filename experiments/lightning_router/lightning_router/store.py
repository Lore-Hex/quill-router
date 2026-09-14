from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from .money import MAX_MSATS, btc, msats

metadata = MetaData()
accounts = Table(
    "lr_accounts", metadata,
    Column("key_hash", String(64), primary_key=True),
    Column("balance_msat", BigInteger, nullable=False, default=0),
    CheckConstraint(f"balance_msat >= 0 AND balance_msat <= {MAX_MSATS}"),
)
invoices = Table(
    "lr_invoices", metadata,
    Column("id", String(32), primary_key=True),
    Column("key_hash", String(64), ForeignKey("lr_accounts.key_hash"), nullable=False),
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
    Column("settle_index", BigInteger, nullable=True, unique=True),
    Column("last_checked", BigInteger, nullable=False, default=0),
    UniqueConstraint("key_hash", "request_id"),
    CheckConstraint("state IN ('OPEN', 'ACCEPTED', 'SETTLED', 'CANCELED')"),
    CheckConstraint(f"amount_msat >= 0 AND amount_msat <= {MAX_MSATS}"),
)
Index("lr_invoice_reconcile", invoices.c.state, invoices.c.last_checked, invoices.c.id)
deposits = Table(
    "lr_deposits", metadata,
    Column("payment_hash", String(64), primary_key=True),
    Column("key_hash", String(64), ForeignKey("lr_accounts.key_hash"), nullable=False),
    Column("amount_msat", BigInteger, nullable=False),
    Column("settled_at", BigInteger, nullable=False),
    CheckConstraint(f"amount_msat > 0 AND amount_msat <= {MAX_MSATS}"),
)
limits = Table(
    "lr_rate_limits", metadata,
    Column("id", String(96), primary_key=True),
    Column("window", BigInteger, nullable=False),
    Column("count", Integer, nullable=False),
)


class Store:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(url, pool_pre_ping=True)
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
        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    def _account(self, conn: Connection, key_hash: str, *, create: bool) -> Any:
        # PostgreSQL locks serialize invoice creation and balance updates per key.
        # SQLite uses BEGIN IMMEDIATE; both run the identical contract tests.
        if create:
            self._insert_once(conn, accounts, {"key_hash": key_hash, "balance_msat": 0})
        row = conn.execute(select(accounts).where(accounts.c.key_hash == key_hash).with_for_update()).mappings().first()
        if row is None:
            raise KeyError("Unknown API key")
        return row

    def _insert_once(self, conn: Connection, table: Table, values: dict[str, Any]) -> None:
        statement = pg_insert(table) if self.engine.dialect.name == "postgresql" else sqlite_insert(table)
        conn.execute(statement.values(**values).on_conflict_do_nothing())

    def balance(self, key_hash: str) -> dict[str, str]:
        with self.transaction() as conn:
            row = self._account(conn, key_hash, create=False)
            return {"balance_msat": str(row["balance_msat"]), "balance_btc": btc(row["balance_msat"])}

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
                payment_hash: str, now: int, *, new: bool, requested_msat: int,
                usd_cents: int, usd_per_btc: str) -> dict[str, Any]:
        msats(requested_msat)
        if requested_msat <= 0 or not 1 <= usd_cents <= 100_000:
            raise ValueError("Invalid invoice amount")
        with self.transaction() as conn:
            self._account(conn, key_hash, create=new)
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
            account = self._account(conn, owner, create=False)
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
            values: dict[str, Any] = {"state": state, "last_checked": now}
            if state == "SETTLED":
                if amount < row["requested_msat"] or settle_index <= 0:
                    raise ValueError("Invalid settled invoice")
                total = msats(account["balance_msat"] + amount)
                conn.execute(insert(deposits).values(
                    payment_hash=payment_hash, key_hash=owner, amount_msat=amount, settled_at=now,
                ))
                conn.execute(update(accounts).where(accounts.c.key_hash == owner).values(balance_msat=total))
                values.update(amount_msat=amount, settle_index=settle_index)
            elif row["state"] == "ACCEPTED" and state == "OPEN":
                values["state"] = "ACCEPTED"
            conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(**values))

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute(select(invoices).where(
                invoices.c.state.in_(["OPEN", "ACCEPTED"]),
            ).order_by(invoices.c.last_checked, invoices.c.id).limit(min(limit, 100))).mappings()]

    def checked(self, invoice_id: str, now: int) -> None:
        with self.transaction() as conn:
            conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(last_checked=now))

    def rate_limit(self, identity: str, now: int, maximum: int = 10) -> bool:
        window = now // 900
        with self.transaction() as conn:
            self._insert_once(conn, limits, {"id": identity, "window": window, "count": 0})
            row = conn.execute(select(limits).where(limits.c.id == identity).with_for_update()).mappings().one()
            count = row["count"] if row["window"] == window else 0
            if count >= maximum:
                return False
            conn.execute(update(limits).where(limits.c.id == identity).values(window=window, count=count + 1))
            return True
