"""An in-memory stand-in for the Spanner database the fast path's enabler
reads and writes (trusted_router.fastpath_enabler): it answers exactly the
enabler's own SQL, by the module's constants, and raises on anything else,
so a new query cannot pass unanswered. Writes are buffered per transaction
and applied at its commit, with a commit timestamp from a monotonic clock;
every write is logged by table, so a test can hold the enabler to the
tables it may write. ``before_transaction`` runs at the start of each
transaction, to commit another writer's change between a run's snapshot and
its write. The native GoogleSQL acceptance of the same SQL is
tests/conformance/test_fastpath_enabler_native.py."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.spanner_v1 import COMMIT_TIMESTAMP

from trusted_router import fastpath_enabler as fe

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
REVOKE_SQL_PREFIX = "UPDATE tr_lease SET revoked = TRUE"


@dataclass
class Credit:
    """One tr_credit_balance row."""

    shard: int = 0
    tier: int | None = 3
    latched_at: datetime | None = None
    override_tier: int | None = None
    causes: list[str] | None = None
    in_debt: bool | None = False
    credits: int = 1_000_000_000
    usage: int = 0
    reserved: int = 0
    reconciled: datetime | None = T0

    def values(self) -> tuple[Any, ...]:
        return (self.shard, self.tier, self.latched_at, self.override_tier, self.causes, self.in_debt,
                self.credits, self.usage, self.reserved, self.reconciled)


@dataclass
class FakeDatabase:
    credit: dict[str, list[Credit]] = field(default_factory=dict)
    overrides: dict[str, tuple[int, bool]] = field(default_factory=dict)
    entities: dict[tuple[str, str], str] = field(default_factory=dict)
    switch: dict[str, tuple[bool, datetime]] = field(default_factory=dict)
    holds: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    kills: list[datetime] = field(default_factory=list)
    open_leases: dict[str, int] = field(default_factory=dict)
    clock: datetime = T0
    writes: list[tuple[str, str]] = field(default_factory=list)  # (table, workspace)
    queries: list[str] = field(default_factory=list)
    fail_on: str | None = None
    before_transaction: list[Callable[[FakeDatabase], None]] = field(default_factory=list)
    transactions: int = 0

    def tick(self) -> datetime:
        self.clock += timedelta(microseconds=1)
        return self.clock

    # -- reads, at the database's state -------------------------------------

    def answer(self, sql: str, params: dict[str, Any] | None) -> list[tuple[Any, ...]]:
        self.queries.append(sql)
        if self.fail_on is not None and sql == self.fail_on:
            raise RuntimeError("deadline exceeded")
        params = params or {}
        if sql == fe.ENABLER_SWITCH_SQL:
            return [(w, e, at) for w, (e, at) in sorted(self.switch.items())]
        if sql == fe.ENABLER_HOLDS_SQL:
            return [(w, r, at) for w, (r, at) in sorted(self.holds.items())]
        if sql == fe.ENABLER_KILLS_SQL:
            return [(at,) for at in sorted(self.kills)]
        if sql == fe.ENABLER_WORKSPACE_IDS_SQL:
            return [(w,) for w in sorted(self.credit)]
        if sql == fe.ENABLER_BALANCE_SQL:
            return [(w, *row.values()) for w in params["ids"] for row in self.credit.get(w, [])]
        if sql == fe.ENABLER_OVERRIDES_SQL:
            return [(w, *self.overrides[w]) for w in params["ids"] if w in self.overrides]
        if sql == fe.ENABLER_ENTITIES_SQL:
            return [(i, self.entities[(params["kind"], i)]) for i in params["ids"]
                    if (params["kind"], i) in self.entities]
        if sql == fe.ENABLER_GUARD_SQL:
            return [(w,) for w in params["rows"] if w in self.holds]
        if sql == fe.ENABLER_SWITCH_ROW_SQL:
            row = self.switch.get(params["workspace"])
            return [] if row is None else [(row[0],)]
        if sql == fe.ENABLER_CREDIT_ROWS_SQL:
            return [row.values() for row in sorted(self.credit.get(params["workspace"], []), key=lambda r: r.shard)]
        if sql == fe.ENABLER_OVERRIDE_SQL:
            found = self.overrides.get(params["workspace"])
            return [] if found is None else [found]
        raise AssertionError(f"the fake does not answer: {sql}")

    @contextmanager
    def snapshot(self, multi_use: bool = False) -> Iterator[_Reader]:
        yield _Reader(self)

    def run_in_transaction(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        for hook in list(self.before_transaction):
            hook(self)
        self.transactions += 1
        txn = _Transaction(self)
        result = func(txn)
        txn.commit()
        return result


class _Reader:
    def __init__(self, database: FakeDatabase) -> None:
        self._database = database

    def execute_sql(self, sql: str, params: dict[str, Any] | None = None, param_types: Any = None,
                    request_options: Any = None, **_: Any) -> list[tuple[Any, ...]]:
        return self._database.answer(sql, params)


class _Transaction(_Reader):
    def __init__(self, database: FakeDatabase) -> None:
        super().__init__(database)
        self._mutations: list[tuple[str, list[str], list[Any]]] = []
        self._updates: list[tuple[str, dict[str, Any]]] = []
        self.committed: datetime | None = None

    def insert_or_update(self, table: str, columns: list[str], values: list[list[Any]]) -> None:
        for row in values:
            self._mutations.append((table, list(columns), list(row)))

    def execute_update(self, sql: str, params: dict[str, Any] | None = None, param_types: Any = None,
                       **_: Any) -> int:
        params = params or {}
        self._database.queries.append(sql)
        if sql.startswith(REVOKE_SQL_PREFIX):
            self._updates.append((sql, params))
            return self._database.open_leases.get(params["workspace"], 0)
        if sql.startswith("DELETE FROM tr_fastpath_workspace_hold"):
            self._updates.append((sql, params))
            return 1 if params["workspace"] in self._database.holds else 0
        raise AssertionError(f"the fake does not execute: {sql}")

    def commit(self) -> None:
        if not self._mutations and not self._updates:
            return
        db = self._database
        at = db.tick()
        for table, columns, row in self._mutations:
            values = dict(zip(columns, [at if v is COMMIT_TIMESTAMP else v for v in row], strict=True))
            db.writes.append((table, values["workspace_id"]))
            if table == "tr_fastpath_workspace":
                db.switch[values["workspace_id"]] = (values["enabled"], values["changed_at"])
            elif table == "tr_fastpath_workspace_hold":
                db.holds[values["workspace_id"]] = (values["reason"], values["held_at"])
            else:
                raise AssertionError(table)
        for sql, params in self._updates:
            if sql.startswith(REVOKE_SQL_PREFIX):
                db.writes.append(("tr_lease", params["workspace"]))
                db.open_leases[params["workspace"]] = 0
            else:
                db.writes.append(("tr_fastpath_workspace_hold", params["workspace"]))
                db.holds.pop(params["workspace"], None)
        self.committed = at


# -- other writers, as Go writes (store/workspace.go, store/hold.go) ------------


def go_disable(db: FakeDatabase, workspace: str) -> None:
    """fastpathctl disable: the switch off and a hold, one commit."""
    at = db.tick()
    db.switch[workspace] = (False, at)
    db.holds[workspace] = ("operator", at)
    db.open_leases[workspace] = 0


def go_disable_all(db: FakeDatabase) -> None:
    """fastpathctl disable-all: every enabled row off at the commit, the
    fleet hold and a kill row with that same timestamp."""
    at = db.tick()
    for workspace, (enabled, _) in list(db.switch.items()):
        if enabled:
            db.switch[workspace] = (False, at)
    db.holds[fe.FLEET_HOLD] = ("kill", at)
    db.kills.append(at)


def go_pause(db: FakeDatabase) -> None:
    db.holds.setdefault(fe.PAUSE_ROW, ("pause", db.tick()))


def go_arm(db: FakeDatabase) -> None:
    db.holds.setdefault(fe.ARMED_ROW, ("armed", db.tick()))
