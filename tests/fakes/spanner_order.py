"""Record SQL and buffered mutations; check DML-only hot billing lock order.

The invariant covers DML statements in the hot billing transactions. Mutation
administrative repairs such as repair_typed_reserved are outside it: their
writes are buffered until commit, so call order does not establish lock order.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.spanner import _FakeTransaction

StatementCalls = list[tuple[Any, str]]

# The authorize-side reserves and the settle-side releases start with the
# exclusive-scan statement hint, as recorded here: whitespace-normalized and
# lower-cased. The one release that cannot carry it (the folded batch's credit
# release, which has a subquery) follows a hinted statement that only locks.
HINT = "@{lock_scanned_ranges=exclusive} "
RESERVE_CREDIT = "@{lock_scanned_ranges=exclusive} update tr_credit_balance set reserved = reserved + @est "
RESERVE_KEY = "@{lock_scanned_ranges=exclusive} update tr_key_limit set reserved = reserved + @est "
LOCK_CREDIT_RELEASE = (
    "@{lock_scanned_ranges=exclusive} update tr_credit_balance "
    "set reserved = reserved, total_usage = total_usage "
)
RELEASE_CREDIT_NO_DEBT = (
    "update tr_credit_balance set reserved = reserved - @hold, total_usage = total_usage + @actual "
)
RELEASE_KEY = "@{lock_scanned_ranges=exclusive} update tr_key_limit set reserved = reserved - @hold, "


def unhinted(sql: str) -> str:
    """A recorded statement without its leading exclusive-scan hint."""
    return sql.removeprefix(HINT)


def record_statements(monkeypatch: pytest.MonkeyPatch) -> StatementCalls:
    calls: StatementCalls = []
    execute_update = _FakeTransaction.execute_update
    execute_sql = _FakeTransaction.execute_sql

    def update(transaction: Any, sql: str, **kwargs: Any) -> int:
        if not getattr(transaction, "_in_returning", False):
            calls.append((transaction, " ".join(sql.split()).lower()))
        return execute_update(transaction, sql, **kwargs)

    def read(transaction: Any, sql: str, **kwargs: Any) -> Any:
        calls.append((transaction, " ".join(sql.split()).lower()))
        return execute_sql(transaction, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_update", update)
    monkeypatch.setattr(_FakeTransaction, "execute_sql", read)

    def record_mutation(name: str) -> None:
        original = getattr(_FakeTransaction, name)

        def mutation(transaction: Any, *args: Any, **kwargs: Any) -> Any:
            table = kwargs["table"] if "table" in kwargs else args[0]
            calls.append((transaction, f"mutation:{name} {table}".lower()))
            return original(transaction, *args, **kwargs)

        monkeypatch.setattr(_FakeTransaction, name, mutation)

    for name in ("insert", "update", "insert_or_update", "replace", "delete"):
        if hasattr(_FakeTransaction, name):
            record_mutation(name)
    return calls


def transaction_statements(calls: StatementCalls) -> list[str]:
    """Extract one hot billing transaction and require DML-only counter writes."""
    assert len({id(tx) for tx, _ in calls}) == 1
    statements = [sql for _, sql in calls]
    no_counter_mutations(statements)
    return statements


def no_counter_mutations(statements: list[str]) -> None:
    assert not any(
        sql.startswith("mutation:") and any(
            table in sql for table in ("tr_credit_balance", "tr_key_limit")
        )
        for sql in statements
    ), statements


def credit_before_key(
    statements: list[str], *, key_last: bool = False, require_both: bool = True,
    exclusive: bool = True,
) -> None:
    """Hot billing DML: no credit/recovery/pause access follows the first key.

    Release paths additionally require the key UPDATE to be the final statement,
    and (exclusive, the default) that a transaction which releases a hold takes
    each counter row exclusively at first touch. Only statement lists recorded
    from a frozen copy of an older finalize pass exclusive=False.
    Failed transactions may exit before either table; require_both=False still
    checks the order of every access that actually happened.
    Authorize only requires credit before key: request INSERTs follow the key.
    repair_typed_reserved and other mutation-based administrative repairs are
    outside this invariant because mutation writes are buffered until commit.
    """
    no_counter_mutations(statements)
    credit = [
        i for i, sql in enumerate(statements)
        if any(name in sql for name in (
            "tr_credit_balance", "tr_trust_event", "billing_pause", "pause_epoch",
        ))
    ]
    key = [i for i, sql in enumerate(statements) if "tr_key_limit" in sql]
    if require_both:
        assert credit and key, statements
    if credit and key:
        assert max(credit) < min(key), statements
    if key_last:
        assert key[-1] == len(statements) - 1, statements
        assert unhinted(statements[-1]).startswith("update tr_key_limit"), statements
    if exclusive:
        exclusive_first_touch(statements)


def exclusive_first_touch(statements: list[str]) -> None:
    """A transaction that writes a counter row takes it exclusively at first touch.

    The first statement to name tr_credit_balance, and the first to name
    tr_key_limit, in a transaction that releases a hold must carry the
    exclusive-scan hint: a shared first touch is the lock upgrade that makes
    simultaneous writers of one row abort each other.
    """
    for table in ("tr_credit_balance", "tr_key_limit"):
        touching = [sql for sql in statements if f"update {table} " in sql or f"from {table} " in sql]
        if not any(f"{table} set reserved = reserved - @hold" in sql for sql in touching):
            continue
        assert touching[0].startswith(HINT), touching[0]


def authorize_credit_before_key(statements: list[str]) -> None:
    credit_before_key(statements)
    first_key = next(i for i, sql in enumerate(statements) if "tr_key_limit" in sql)
    # Only request INSERTs may follow the key.
    assert all("tr_key_limit" in sql or sql.startswith((
        "insert into tr_reservation ", "insert into tr_gateway_authorization ",
        "insert into tr_entities ", "insert into tr_operational_analytics_outbox ",
    )) for sql in statements[first_key + 1:]), statements
