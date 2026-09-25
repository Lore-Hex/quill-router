"""Check credit-before-key call order at two instrumented fake funnels.

Every read-write transaction that reaches the two instrumented funnels is
checked during the function-scoped fixture's observation window: Spanner's
_FakeTransaction methods, and PostgresStore._run_transaction using
SqlitePostgresConn. These funnels are not the only way to reach the database.
The paths in KNOWN_UNCOVERED_PATHS below are not covered.

The observation is table-level call order, not row contention or deadlock
freedom. Spanner read-write SELECTs count as reads that acquire locks; Postgres
SELECTs count only with explicit locking clauses. Buffered counter writes take no
call-order lock, so such a transaction is reported as UNPROVED (see
``recorder.unproved``) rather than ordered -- absence of a violation there is
not evidence of safety. The hot billing paths are separately required to use
DML by spanner_order.no_counter_mutations. The fake returns eager lists;
the real Spanner SDK streams lazily, so observed and execution order may differ.
"""

from __future__ import annotations

import contextlib
import functools
import re
import threading
from collections.abc import Iterator
from typing import Any

KNOWN_UNCOVERED_PATHS = (
    "SqlitePostgresConn.transaction() outside PostgresStore._run_transaction",
    "conn._raw.execute",
    "cursors returned by execute",
    "local fake transactions in test_trust_tier_slice1a.py",
    "local fake transactions in test_operational_analytics_outbox_postgres.py",
    "PostgresStore methods taking a connection directly outside the funnel",
    "broader-scoped fixture setup and teardown outside the observation window",
    "real Spanner SDK lazy streaming execution order",
)
CREDIT_TABLES = {"tr_credit_balance", "tr_trust_event"}
KEY_TABLE = "tr_key_limit"
Step = tuple[frozenset[str], str]


def _tokens(sql: str) -> list[str]:
    """Remove comments/literals before interpreting identifiers or SQL keywords."""
    lexer = re.compile(
        r"--[^\n]*(?:\n|$)|/\*|'(?:''|[^'])*'|"
        r'"(?:""|[^"])*"|`[^`]*`|[a-zA-Z_][a-zA-Z_0-9$]*|\s+|[^\s]',
    )
    result = []
    pos = 0
    while pos < len(sql):
        match = lexer.match(sql, pos)
        assert match is not None
        word, pos = match.group(), match.end()
        if word == "/*":
            # Quotes inside comments have no SQL string-literal semantics.
            depth = 1
            for boundary in re.finditer(r"/\*|\*/", sql[pos:]):
                depth += 1 if boundary.group() == "/*" else -1
                if depth == 0:
                    pos += boundary.end()
                    break
            else:
                pos = len(sql)
        elif word.startswith("--") or word.isspace():
            continue
        elif word.startswith("'"):
            # A placeholder avoids joining tokens across a blanked literal.
            result.append("?")
        else:
            result.append(word.lower())
    return result


def _identifier(tokens: list[str], start: int) -> tuple[str, int]:
    if start >= len(tokens) or not re.fullmatch(r'[\w$]+|"[^"]+"|`[^`]+`', tokens[start]):
        return "", start
    name = tokens[start].strip('"`')
    end = start + 1
    while end + 1 < len(tokens) and tokens[end] == ".":
        name = tokens[end + 1].strip('"`')
        end += 2
    return name, end


def locked_relations(sql: str, *, read_locks: bool = False) -> set[str]:
    """Find write targets and locking-read relations in each query scope.

    DML CTEs and explicitly locked subqueries contribute their own locks. A
    mere EXISTS/table-name mention does not change the write target. OF lists
    restrict locking reads to the named aliases. Multiple classes in one SQL
    call have no established acquisition order and are recorded simultaneously.
    """
    def scope(tokens: list[str], inherited: dict[str, set[str]]) -> tuple[set[str], set[str]]:
        locked: set[str] = set()
        sources: set[str] = set()
        ctes = dict(inherited)
        flat: list[str] = []
        # Process CTE definitions first so references resolve to base relations.
        pos = 1 if tokens[:1] == ["with"] else 0
        if pos and tokens[pos:pos + 1] == ["recursive"]:
            pos += 1

        def group(start: int) -> tuple[list[str], int]:
            depth, end = 1, start + 1
            while end < len(tokens) and depth:
                depth += (tokens[end] == "(") - (tokens[end] == ")")
                end += 1
            return tokens[start + 1:end - 1], end

        if pos:
            while pos < len(tokens):
                name, pos = _identifier(tokens, pos)
                if tokens[pos:pos + 1] == ["("]:
                    _, pos = group(pos)  # optional CTE column names
                if tokens[pos:pos + 1] != ["as"]:
                    break
                pos += 1
                while tokens[pos:pos + 1] in (["not"], ["materialized"]):
                    pos += 1
                if tokens[pos:pos + 1] != ["("]:
                    break
                body, pos = group(pos)
                child_locks, child_sources = scope(body, ctes)
                locked.update(child_locks)
                ctes[name] = child_sources
                if tokens[pos:pos + 1] != [","]:
                    break
                pos += 1
        while pos < len(tokens):
            if tokens[pos] == "(":
                body, pos = group(pos)
                if any(t in body for t in ("select", "with", "update", "insert", "delete", "merge")):
                    child_locks, child_sources = scope(body, ctes)
                    locked.update(child_locks)
                    alias = f"__subquery_{len(flat)}"
                    ctes[alias] = child_sources
                    flat.append(alias)
                else:
                    flat.append("?")
            else:
                flat.append(tokens[pos])
                pos += 1

        aliases: dict[str, set[str]] = {}
        in_from = False
        for i, word in enumerate(flat):
            if word in {"where", "group", "order", "limit", "returning", "for", "set"}:
                in_from = False
            if word in {"from", "join"} or (word == "," and in_from):
                in_from = True
                name, end = _identifier(flat, i + 1)
                if name:
                    relations = ctes.get(name, {name})
                    sources.update(relations)
                    aliases[name] = relations
                    if flat[end:end + 1] == ["as"]:
                        end += 1
                    alias, _ = _identifier(flat, end)
                    if alias:
                        aliases[alias] = relations

        # Only the statement's target, not UPDATE in ON CONFLICT or FOR UPDATE.
        target_start = {"update": 1, "insert": 2, "delete": 2, "merge": 2}.get(
            flat[0] if flat else "",
        )
        if target_start is not None:
            if flat[target_start:target_start + 1] == ["only"]:
                target_start += 1
            target, _ = _identifier(flat, target_start)
            if target:
                locked.add(target)
        if read_locks:
            locked.update(sources)
        for i, word in enumerate(flat):
            if word != "for":
                continue
            end = i + 1
            while flat[end:end + 1] in (["no"], ["key"]):
                end += 1
            if flat[end:end + 1] not in (["update"], ["share"]):
                continue
            end += 1
            if flat[end:end + 1] != ["of"]:
                locked.update(sources)
                continue
            end += 1
            while end < len(flat):
                alias, end = _identifier(flat, end)
                locked.update(aliases.get(alias, set()))
                if flat[end:end + 1] != [","]:
                    break
                end += 1
        return locked, sources

    return scope(_tokens(str(sql)), {})[0]


class LockOrderError(AssertionError):
    """A trace has an inversion or counter writes whose order is unknown."""


class _Recorder:
    def __init__(self) -> None:
        self._tx: dict[str, list[Step]] = {}
        self._n = 0
        self._lock = threading.RLock()
        self.installed = False
        self.hooks: list[tuple[Any, str, Any]] = []
        self._unproved: set[str] = set()

    def reset(self) -> None:
        with self._lock:
            self._tx.clear()
            self._unproved.clear()

    def next_key(self, backend: str) -> str:
        with self._lock:
            self._n += 1
            return f"{backend}#{self._n}"

    @property
    def both_tables_seen(self) -> int:
        with self._lock:
            return sum(
                {"credit", "key"} <= set().union(*(kinds for kinds, _ in steps))
                for steps in self._tx.values()
            )

    def record(self, key: str, sql: str, *, read_locks: bool = False) -> None:
        relations = locked_relations(sql, read_locks=read_locks)
        kinds = set()
        if relations & CREDIT_TABLES:
            kinds.add("credit")
        if KEY_TABLE in relations:
            kinds.add("key")
        if kinds:
            self._append(key, kinds, sql)

    def _append(self, key: str, kinds: set[str], sql: str) -> None:
        with self._lock:
            self._tx.setdefault(key, []).append((frozenset(kinds), " ".join(sql.split())[:160]))

    @property
    def unproved(self) -> set[str]:
        """Transactions this guard cannot order (they write counters as buffered mutations)."""
        return set(self._unproved)

    def mutation(self, key: str, table: str, method: str) -> None:
        if table.lower() in {"tr_credit_balance", KEY_TABLE}:
            self._append(key, {"buffered"}, f"mutation:{method} {table}")

    def violations(self) -> list[tuple[str, list[Step]]]:
        with self._lock:
            found = []
            for key, steps in self._tx.items():
                credit = [i for i, (kinds, _) in enumerate(steps) if "credit" in kinds]
                keys = [i for i, (kinds, _) in enumerate(steps) if "key" in kinds]
                if any("buffered" in kinds for kinds, _ in steps):
                    # A buffered counter write is applied atomically at commit, so
                    # its call order takes no lock and this guard cannot order it:
                    # the transaction is UNPROVED. That is recorded, but it does NOT
                    # excuse the transaction -- an unorderable write cannot erase an
                    # inversion the recorded SQL accesses already establish, so the
                    # order rule below still runs. Presence of a buffered write
                    # alone is not a violation; the hot paths' separate requirement
                    # to use DML stays with spanner_order.no_counter_mutations.
                    self._unproved.add(key)
                if credit and keys and max(credit) >= min(keys):
                    found.append((key, list(steps)))
            return found

    def check(self, where: str) -> None:
        found = self.violations()
        if not found:
            return
        report = [f"lock-order guard: {len(found)} transaction(s) rejected (test: {where})"]
        for key, steps in found:
            report.append(
                f"  transaction {key}: credit-class lock after or simultaneous "
                f"with {KEY_TABLE}; deadlock shape #1236",
            )
            report.extend(f"    {'+'.join(sorted(kinds)):12} {sql}" for kinds, sql in steps)
        raise LockOrderError("\n".join(report))


recorder = _Recorder()
_funnel = threading.local()


def _spanner_key(transaction: Any) -> str:
    with recorder._lock:
        if not hasattr(transaction, "_lock_order_key"):
            transaction._lock_order_key = recorder.next_key("spanner")
        return str(transaction._lock_order_key)


def _connection_state(conn: Any) -> Any:
    with recorder._lock:
        if not hasattr(conn, "_lock_order_local"):
            conn._lock_order_local = threading.local()
            conn._lock_order_identity = recorder.next_key("postgres-connection")
            conn._lock_order_attempt = 0
        return conn._lock_order_local


def install() -> None:
    """Attach hooks once; fail closed if an installed callable was replaced."""
    if recorder.installed:
        for owner, name, wrapper in recorder.hooks:
            if getattr(owner, name, None) is not wrapper:
                raise RuntimeError(f"lock-order guard: {owner.__name__}.{name} hook replaced")
        return

    from tests.fakes import postgres as pg_fake
    from tests.fakes import spanner as spanner_fake
    from trusted_router import storage_postgres

    def patch(owner: Any, name: str, factory: Any) -> None:
        if not hasattr(owner, name):
            raise RuntimeError(f"lock-order guard: {owner.__name__}.{name} is gone")
        wrapper = factory(getattr(owner, name))
        wrapper._lock_order_hook = True
        setattr(owner, name, wrapper)
        recorder.hooks.append((owner, name, wrapper))

    def statement(original: Any) -> Any:
        @functools.wraps(original)
        def wrapper(self: Any, sql: str, *args: Any, **kwargs: Any) -> Any:
            recorder.record(_spanner_key(self), sql, read_locks=True)
            return original(self, sql, *args, **kwargs)
        return wrapper

    def mutation(original: Any) -> Any:
        @functools.wraps(original)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            table = kwargs["table"] if "table" in kwargs else args[0]
            recorder.mutation(_spanner_key(self), table, original.__name__)
            return original(self, *args, **kwargs)
        return wrapper

    for name in ("execute_update", "execute_sql"):
        patch(spanner_fake._FakeTransaction, name, statement)
    for name in ("insert", "update", "insert_or_update", "replace", "delete"):
        if hasattr(spanner_fake._FakeTransaction, name):
            patch(spanner_fake._FakeTransaction, name, mutation)

    def run_transaction(original: Any) -> Any:
        @functools.wraps(original)
        def wrapper(self: Any, work: Any, *args: Any, **kwargs: Any) -> Any:
            previous = getattr(_funnel, "active", False)
            _funnel.active = True
            try:
                return original(self, work, *args, **kwargs)
            finally:
                _funnel.active = previous
        return wrapper

    def transaction(original: Any) -> Any:
        @functools.wraps(original)
        @contextlib.contextmanager
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Iterator[Any]:
            local = _connection_state(self)
            previous = getattr(local, "key", None)
            with original(self, *args, **kwargs) as value:
                if previous is None and getattr(_funnel, "active", False):
                    with recorder._lock:
                        self._lock_order_attempt += 1
                        local.key = f"{self._lock_order_identity}/attempt-{self._lock_order_attempt}"
                try:
                    yield value
                finally:
                    local.key = previous
        return wrapper

    def execute(original: Any) -> Any:
        @functools.wraps(original)
        def wrapper(self: Any, sql: str, *args: Any, **kwargs: Any) -> Any:
            key = getattr(_connection_state(self), "key", None)
            if key is not None:
                recorder.record(key, sql)
            return original(self, sql, *args, **kwargs)
        return wrapper

    patch(storage_postgres.PostgresStore, "_run_transaction", run_transaction)
    patch(pg_fake.SqlitePostgresConn, "transaction", transaction)
    patch(pg_fake.SqlitePostgresConn, "execute", execute)
    recorder.installed = True
