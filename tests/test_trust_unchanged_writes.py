"""The trust tier job skips workspaces whose stored trust values are already current.

Measured 2026-10-01 (SPANNER_SYS, one hour): the tier job rewrote every
workspace's tier and reconciled-through watermark, about 12.7k read-write
commits an hour (7% of all Spanner commits) although almost none changed. Both
writes now run only when a stored value differs, decided first on a lock-free
snapshot; the read-write path still recomputes under its own reads.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, CreditProvenance, User, Workspace
from trusted_router.storage_trust_reconciliation import (
    PostgresTrustReconciliationRepository,
    SpannerTrustReconciliationRepository,
)

NOW = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(hours=1)
WS = "workspace-unchanged"
POLICY: dict[str, Any] = {
    "qualifying_providers": frozenset({"stripe", "x402"}),
    "tier3_min_days": 30,
    "tier3_min_paid_microdollars": 50_000_000,
}


def _tier3_workspace() -> tuple[Any, Any]:
    store, database = make_fake_store()
    store._write_entity(
        "user", "owner", User(id="owner", email="owner@example.com", identity_status="approved")
    )
    store._write_entity("workspace", WS, Workspace(id=WS, name="tier", owner_user_id="owner"))
    store._write_entity("credit", WS, CreditAccount(workspace_id=WS, shard_count=2))
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard in range(2):
        table[(WS, shard)] = {
            "workspace_id": WS,
            "shard": shard,
            "total_credits": 0,
            "total_usage": 0,
            "reserved": 0,
            "trust_tier": 0,
            "trust_computed_at": None,
            "trust_latched_at": None,
            "trust_override_tier": None,
            "billing_pause_causes": [],
            "pause_epoch": 0,
            "trust_reconciled_through": None,
        }
    assert store.credit_workspace_typed_direct(
        WS,
        50_000_000,
        "payment-event",
        provenance=CreditProvenance("checkout", "stripe", "pi_tier3", NOW - dt.timedelta(days=31)),
        payment_amount_microdollars=50_000_000,
        currency="USD",
    )
    # Positive control: the first computation writes tier 3 to both shards.
    assert store.recompute_workspace_trust_tier(WS, now=NOW, **POLICY) == 3
    assert _tiers(database) == {(3, NOW)}
    return store, database


def _tiers(database: Any) -> set[tuple[Any, Any]]:
    return {
        (row["trust_tier"], row["trust_computed_at"])
        for row in database.typed[CREDIT_BALANCE_TABLE].values()
    }


def _count_transactions(monkeypatch: pytest.MonkeyPatch, database: Any) -> list[int]:
    counter = [0]
    original = database.run_in_transaction

    def counting(*args: Any, **kwargs: Any) -> Any:
        counter[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "run_in_transaction", counting)
    return counter


def test_unchanged_tier_runs_no_transaction_and_keeps_computed_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database = _tier3_workspace()
    transactions = _count_transactions(monkeypatch, database)

    assert store.recompute_workspace_trust_tier(WS, now=LATER, **POLICY) == 3

    assert transactions[0] == 0
    assert _tiers(database) == {(3, NOW)}


def test_a_stale_shard_is_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    store, database = _tier3_workspace()
    database.typed[CREDIT_BALANCE_TABLE][(WS, 1)]["trust_tier"] = 1
    transactions = _count_transactions(monkeypatch, database)

    assert store.recompute_workspace_trust_tier(WS, now=LATER, **POLICY) == 3

    assert transactions[0] == 1
    assert _tiers(database) == {(3, LATER)}


def test_a_tier_never_stamped_with_computed_at_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # New rows default to tier 0 with no computed_at; a matching tier alone
    # must not leave computed_at unset forever.
    store, database = _tier3_workspace()
    for row in database.typed[CREDIT_BALANCE_TABLE].values():
        row["trust_computed_at"] = None
    transactions = _count_transactions(monkeypatch, database)

    assert store.recompute_workspace_trust_tier(WS, now=LATER, **POLICY) == 3

    assert transactions[0] == 1
    assert _tiers(database) == {(3, LATER)}


def test_tier_snapshot_failure_falls_back_to_the_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database = _tier3_workspace()
    transactions = _count_transactions(monkeypatch, database)
    original_snapshot = database.snapshot
    failed = [0]

    def failing_snapshot(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("multi_use"):
            failed[0] += 1
            raise RuntimeError("snapshot unavailable")
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(database, "snapshot", failing_snapshot)

    assert store.recompute_workspace_trust_tier(WS, now=LATER, **POLICY) == 3

    assert failed[0] == 1
    assert transactions[0] == 1
    # The transaction's own reads found the tier current, so it wrote nothing.
    assert _tiers(database) == {(3, NOW)}


WATERMARK = NOW - dt.timedelta(minutes=20)
OLDER = NOW - dt.timedelta(hours=2)


class _Rows(list[tuple[Any, ...]]):
    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self)


class _Reader:
    """Answers the watermark queries for one workspace with ``stored`` per shard."""

    def __init__(self, stored: list[Any], *, providers: tuple[str, ...] = ("stripe",)) -> None:
        self.stored = stored
        self.providers = providers
        self.updates: list[Any] = []

    def execute_sql(self, sql: str, **kwargs: Any) -> list[tuple[Any, ...]]:
        if sql.startswith("SELECT DISTINCT provider"):
            return [(provider,) for provider in self.providers]
        if sql.startswith("SELECT closed_through"):
            return [(WATERMARK,)]
        if sql.startswith("SELECT shard, trust_reconciled_through"):
            return [(shard, value) for shard, value in enumerate(self.stored)]
        if sql.startswith("SELECT shard"):
            return [(shard,) for shard in range(len(self.stored))]
        raise AssertionError(sql)

    def execute_update(self, sql: str, **kwargs: Any) -> int:
        assert "trust_reconciled_through=@watermark" in sql
        self.updates.append(kwargs["params"])
        return len(self.stored)

    # Postgres connection surface.
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        normalized = sql.replace("%s", "@")
        if normalized.startswith("UPDATE tr_credit_balance"):
            self.updates.append(params)
            return SimpleNamespace(rowcount=len(self.stored))
        return _Rows(self.execute_sql(normalized))


def _spanner_repository(
    reader: _Reader, *, snapshot_fails: bool = False
) -> tuple[SpannerTrustReconciliationRepository, list[int]]:
    transactions: list[int] = []

    class Database:
        def snapshot(self, multi_use: bool = False) -> Any:
            if snapshot_fails:
                raise RuntimeError("snapshot unavailable")
            return contextlib.nullcontext(reader)

    def run_in_transaction(callback: Any) -> Any:
        transactions.append(1)
        return callback(reader)

    store = SimpleNamespace(
        _param_types=SimpleNamespace(STRING="STRING", TIMESTAMP="TIMESTAMP"),
        _database=Database(),
        _run_in_transaction=run_in_transaction,
    )
    return SpannerTrustReconciliationRepository(store), transactions


def test_unchanged_watermark_runs_no_transaction() -> None:
    reader = _Reader([WATERMARK, WATERMARK, WATERMARK])
    repository, transactions = _spanner_repository(reader)

    assert repository.replicate_workspace_watermark(WS, frozenset({"stripe"})) == WATERMARK

    assert transactions == []
    assert reader.updates == []


def test_workspace_without_qualifying_payments_keeps_its_null_watermark() -> None:
    reader = _Reader([None, None], providers=())
    repository, transactions = _spanner_repository(reader)

    assert repository.replicate_workspace_watermark(WS, frozenset({"stripe"})) is None

    assert transactions == []
    assert reader.updates == []


def test_a_moved_watermark_rewrites_every_shard() -> None:
    reader = _Reader([OLDER, WATERMARK, WATERMARK])
    repository, transactions = _spanner_repository(reader)

    assert repository.replicate_workspace_watermark(WS, frozenset({"stripe"})) == WATERMARK

    assert transactions == [1]
    assert reader.updates == [{"watermark": WATERMARK, "workspace_id": WS}]


def test_watermark_snapshot_failure_falls_back_to_the_transaction() -> None:
    reader = _Reader([WATERMARK, WATERMARK])
    repository, transactions = _spanner_repository(reader, snapshot_fails=True)

    assert repository.replicate_workspace_watermark(WS, frozenset({"stripe"})) == WATERMARK

    assert transactions == [1]
    assert reader.updates == [{"watermark": WATERMARK, "workspace_id": WS}]


@pytest.mark.parametrize(
    ("stored", "expected_updates"),
    [
        ([WATERMARK, WATERMARK], []),
        ([OLDER, WATERMARK], [(WATERMARK, WS)]),
    ],
    ids=["unchanged", "moved"],
)
def test_postgres_watermark_writes_only_when_a_shard_differs(
    stored: list[Any], expected_updates: list[Any]
) -> None:
    conn = _Reader(stored)
    store = SimpleNamespace(_run_transaction=lambda write: write(conn))

    result = PostgresTrustReconciliationRepository(store).replicate_workspace_watermark(
        WS, frozenset({"stripe"})
    )

    assert result == WATERMARK
    assert conn.updates == expected_updates
