"""Suite-wide guard: credit-class rows are locked before ``tr_key_limit``.

The billing plane's founding production incident was a Spanner deadlock between
gateway authorize and settle (``Aborted: Deadlock with higher priority
transaction``). #1236 removed the circular wait by giving every hot counter
transaction one order -- credit row first, key row second -- and that order is
the only thing standing between the fleet and the same deadlock storm.

``tests/fakes/spanner_order.py`` already checks the order, but only inside
tests that remember to call it, and only on Spanner. A transaction added
tomorrow -- or the Postgres path that now serves the AWS and Azure control
planes, where nothing checked the order at all -- inherits no protection from
an opt-in helper. This module records EVERY read-write transaction both fakes
execute and fails the test that produced an inversion, so a new transaction is
covered by default rather than by memory.

Scope, stated plainly: this compares lock ORDER per transaction, at table
granularity. It proves the absence of this specific inversion. It is not a
general proof of deadlock freedom -- two transactions must also contend on the
same ROW to deadlock, which table names alone cannot decide.
"""

from __future__ import annotations

from typing import Any

# Kept identical to tests/fakes/spanner_order.credit_before_key so the two
# checks cannot drift into disagreeing about what "credit-class" means.
CREDIT_MARKERS = ("tr_credit_balance", "tr_trust_event", "billing_pause", "pause_epoch")
KEY_TABLE = "tr_key_limit"


class LockOrderError(AssertionError):
    """A transaction took a credit-class lock after a tr_key_limit lock."""


class _Recorder:
    def __init__(self) -> None:
        self._tx: dict[str, list[tuple[str, str]]] = {}
        self._n = 0
        self.installed = False
        self.both_tables_seen = 0

    def reset(self) -> None:
        self._tx.clear()

    def next_key(self, backend: str) -> str:
        self._n += 1
        return f"{backend}#{self._n}"

    def record(self, key: str, sql: str) -> None:
        text = " ".join(str(sql).split()).lower()
        if KEY_TABLE in text:
            kind = "key"
        elif any(marker in text for marker in CREDIT_MARKERS):
            kind = "credit"
        else:
            return
        self._tx.setdefault(key, []).append((kind, " ".join(str(sql).split())[:160]))

    def violations(self) -> list[tuple[str, list[tuple[str, str]]]]:
        found = []
        for key, steps in self._tx.items():
            kinds = [kind for kind, _ in steps]
            if "key" not in kinds or "credit" not in kinds:
                continue
            self.both_tables_seen += 1
            last_key = max(i for i, k in enumerate(kinds) if k == "key")
            if any(k == "credit" for k in kinds[last_key + 1:]):
                found.append((key, steps))
        return found

    def check(self, where: str) -> None:
        found = self.violations()
        if not found:
            return
        report = [
            f"credit-class row locked AFTER {KEY_TABLE} in {len(found)} transaction(s); "
            f"this is the deadlock shape #1236 removed (test: {where})",
        ]
        for key, steps in found:
            report.append(f"  transaction {key}:")
            report.extend(f"    {kind:6} {sql}" for kind, sql in steps)
        raise LockOrderError("\n".join(report))


recorder = _Recorder()


def install() -> None:
    """Patch both storage fakes. Raises if a hook point is gone.

    Fail-closed on purpose: a renamed method must break the guard loudly rather
    than leave it recording nothing while every test still passes.
    """
    if recorder.installed:
        return

    from tests.fakes import postgres as pg_fake
    from tests.fakes import spanner as spanner_fake
    from trusted_router import storage_postgres

    transaction_cls = spanner_fake._FakeTransaction
    for name in ("execute_update", "execute_sql"):
        if not hasattr(transaction_cls, name):
            raise RuntimeError(f"lock-order guard: _FakeTransaction.{name} is gone")
        original = getattr(transaction_cls, name)

        def statement(original: Any = original) -> Any:
            def wrapper(self: Any, sql: Any, *args: Any, **kwargs: Any) -> Any:
                key = getattr(self, "_lock_order_key", None)
                if key is None:
                    # A counter stamped on the object: id() is recycled by the
                    # garbage collector and would splice two transactions into
                    # one trace, inventing orders neither of them performed.
                    key = recorder.next_key("spanner")
                    object.__setattr__(self, "_lock_order_key", key)
                recorder.record(key, sql)
                return original(self, sql, *args, **kwargs)

            return wrapper

        setattr(transaction_cls, name, statement())

    if not hasattr(storage_postgres.PostgresStore, "_run_transaction"):
        raise RuntimeError("lock-order guard: PostgresStore._run_transaction is gone")
    run_transaction = storage_postgres.PostgresStore._run_transaction
    state: dict[str, Any] = {"depth": 0, "key": None}

    def run_wrapper(self: Any, work: Any, *args: Any, **kwargs: Any) -> Any:
        state["depth"] += 1
        previous = state["key"]
        if state["depth"] == 1:
            state["key"] = recorder.next_key("postgres")
        try:
            return run_transaction(self, work, *args, **kwargs)
        finally:
            state["depth"] -= 1
            state["key"] = previous if state["depth"] else None

    storage_postgres.PostgresStore._run_transaction = run_wrapper

    if not hasattr(pg_fake.SqlitePostgresConn, "execute"):
        raise RuntimeError("lock-order guard: SqlitePostgresConn.execute is gone")
    execute = pg_fake.SqlitePostgresConn.execute

    def execute_wrapper(self: Any, sql: Any, params: Any = (), **kwargs: Any) -> Any:
        key = state["key"]
        if key is not None:
            text = " ".join(str(sql).split()).lower()
            # Row locks under READ COMMITTED: writes, and reads that ask for one.
            if text.startswith(("insert", "update", "delete")) or "for update" in text:
                recorder.record(key, sql)
        return execute(self, sql, params, **kwargs)

    pg_fake.SqlitePostgresConn.execute = execute_wrapper
    recorder.installed = True
