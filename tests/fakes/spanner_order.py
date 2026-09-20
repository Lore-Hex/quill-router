"""Record locking reads and writes; assert the credit-before-key invariant."""

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
    return calls


def transaction_statements(calls: StatementCalls) -> list[str]:
    assert len({id(tx) for tx, _ in calls}) == 1
    return [sql for _, sql in calls]


def credit_before_key(statements: list[str], *, key_last: bool = False) -> None:
    """No credit/recovery/pause read or write may follow the first key statement.

    Release paths additionally require the key UPDATE to be the final statement.
    Authorize only requires credit before key: request INSERTs follow the key.
    """
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
