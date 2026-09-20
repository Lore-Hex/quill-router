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


def record_statements(monkeypatch: pytest.MonkeyPatch) -> StatementCalls:
    calls: StatementCalls = []
    execute_update = _FakeTransaction.execute_update
    execute_sql = _FakeTransaction.execute_sql

    def update(transaction: Any, sql: str, **kwargs: Any) -> int:
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


def credit_before_key(statements: list[str], *, key_last: bool = False) -> None:
    """Hot billing DML: no credit/recovery/pause access follows the first key.

    Release paths additionally require the key UPDATE to be the final statement.
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
    assert credit and key, statements
    assert max(credit) < min(key), statements
    if key_last:
        assert key[-1] == len(statements) - 1, statements
        assert statements[-1].startswith("update tr_key_limit"), statements


def authorize_credit_before_key(statements: list[str]) -> None:
    credit_before_key(statements)
    first_key = next(i for i, sql in enumerate(statements) if "tr_key_limit" in sql)
    # The spend-lease hook now precedes the key too. Only request INSERTs remain.
    assert all("tr_key_limit" in sql or sql.startswith((
        "insert into tr_reservation ", "insert into tr_gateway_authorization ",
        "insert into tr_entities ", "insert into tr_operational_analytics_outbox ",
    )) for sql in statements[first_key + 1:]), statements
