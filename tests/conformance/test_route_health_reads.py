"""PostgresStore's route-health benchmark read, against a real Postgres.

The remediator's route-quarantine detector calls
`provider_route_benchmark_samples` on every pass. Filtered on JSON fields only,
that statement read every provider_benchmark row ever written: ~320 MB per pass
on AWS DSQL (measured 2026-09-25). These tests pin the rule that replaced it:
the window is kind plus a range on `indexed_at`, the leading columns of
tr_entities_recent (kind, indexed_at, id).

They run against TR_CONFORMANCE_POSTGRES_DSN (CI's postgres:17 service) and skip
without it, like the rest of the Postgres conformance backend. That database is
shared, and these tests read whole windows and backfill every legacy row they
can see, so each test gets an empty schema of its own, dropped when it
finishes. The plan test uses a temporary table, which Aurora DSQL does not
have; the module targets stock Postgres.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
from collections.abc import Iterator
from typing import Any

import pytest

from .conftest import _postgres_store, _with_connect_timeout, make_benchmark_sample

pytestmark = pytest.mark.xdist_group("conformance-postgres")


@pytest.fixture
def pg_store(unique: str) -> Iterator[Any]:
    """A PostgresStore whose search_path is a new, empty schema."""
    from trusted_router.storage_postgres import PostgresStore

    shared = _postgres_store()
    schema = f"route_health_{unique}"
    shared._run_transaction(lambda conn: conn.execute(f"CREATE SCHEMA {schema}"))
    dsn = os.environ["TR_CONFORMANCE_POSTGRES_DSN"]
    separator = "&" if "?" in dsn else "?"
    store = PostgresStore(
        _with_connect_timeout(f"{dsn}{separator}options=-csearch_path%3D{schema}"),
        postgres_iam_auth=os.environ.get("TR_POSTGRES_IAM_AUTH", ""),
        postgres_iam_region=os.environ.get("TR_POSTGRES_IAM_REGION", ""),
    )
    try:
        store.apply_schema()
        yield store
    finally:
        store.close()
        try:
            shared._run_transaction(lambda conn: conn.execute(f"DROP SCHEMA {schema} CASCADE"))
        finally:
            shared.close()


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _plan_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


class _StatementRecorder:
    """Records the store's statements and runs them unchanged."""

    def __init__(self, conn: Any, statements: list[tuple[str, Any]]) -> None:
        self._conn = conn
        self._statements = statements

    def execute(self, sql: str, params: Any = None) -> Any:
        self._statements.append((sql, params))
        return self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_route_benchmark_window_is_a_range_on_the_recent_index(
    pg_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = dt.datetime.now(dt.UTC)
    pg_store.record_provider_benchmark(
        make_benchmark_sample(
            sample_id="recent",
            provider="acme",
            model="acme/m1",
            created_at=_iso(now - dt.timedelta(hours=1)),
        )
    )
    statements: list[tuple[str, Any]] = []
    run_transaction = pg_store._run_transaction
    monkeypatch.setattr(
        pg_store,
        "_run_transaction",
        lambda operation: run_transaction(
            lambda conn: operation(_StatementRecorder(conn, statements))
        ),
    )
    rows = pg_store.provider_route_benchmark_samples(
        cutoff=_iso(now - dt.timedelta(hours=48)), per_route_limit=48, limit=1_000
    )
    monkeypatch.undo()
    assert [row.id for row in rows] == ["recent"]
    [(sql, params)] = statements

    def explain(conn: Any) -> tuple[list[str], dict[str, Any]]:
        # The store's statement, explained against an empty temporary
        # tr_entities whose only index has tr_entities_recent's key columns,
        # with sequential scans disabled. With no statistics and no other
        # index involved, the plan shows which predicates that index can
        # bound. The savepoint undoes the table and the setting.
        conn.execute("SAVEPOINT recent_index_plan")
        try:
            columns = [
                row[0]
                for row in conn.execute(
                    "SELECT pg_get_indexdef(i.indexrelid, k, false) FROM pg_index i, "
                    "generate_series(1, i.indnkeyatts) AS k "
                    "WHERE i.indexrelid = 'tr_entities_recent'::regclass ORDER BY k"
                ).fetchall()
            ]
            [table] = conn.execute(
                "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.oid = 'tr_entities'::regclass"
            ).fetchone()
            conn.execute(f"CREATE TEMP TABLE tr_entities (LIKE {table})")
            conn.execute(f"CREATE INDEX ON pg_temp.tr_entities ({', '.join(columns)})")
            conn.execute("SET LOCAL enable_seqscan = off")
            [explained] = conn.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()
            return columns, explained[0]["Plan"]
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT recent_index_plan")

    columns, plan = pg_store._run_transaction(explain)

    assert columns[:2] == ["kind", "indexed_at"]
    index_conds = [node["Index Cond"] for node in _plan_nodes(plan) if "Index Cond" in node]
    assert any("kind" in cond and "indexed_at" in cond for cond in index_conds), plan


def test_route_benchmark_window_limit_and_order(pg_store: Any) -> None:
    now = dt.datetime.now(dt.UTC)
    for label, hours_ago in (("old", 72), ("h3", 3), ("h2", 2), ("h1", 1)):
        pg_store.record_provider_benchmark(
            make_benchmark_sample(
                sample_id=label,
                provider="acme",
                model="acme/m1",
                created_at=_iso(now - dt.timedelta(hours=hours_ago)),
            )
        )
    pg_store.record_provider_benchmark(
        dataclasses.replace(
            make_benchmark_sample(
                sample_id="organic",
                provider="acme",
                model="acme/m1",
                created_at=_iso(now - dt.timedelta(minutes=30)),
            ),
            source="organic",
        )
    )

    def window(per_route_limit: int) -> list[str]:
        rows = pg_store.provider_route_benchmark_samples(
            cutoff=_iso(now - dt.timedelta(hours=48)),
            per_route_limit=per_route_limit,
            limit=1_000,
        )
        return [row.id for row in rows]

    # Inside the window, synthetic only, newest first; then capped per route.
    assert window(48) == ["h1", "h2", "h3"]
    assert window(2) == ["h1", "h2"]


def test_route_benchmark_ties_and_limits(pg_store: Any) -> None:
    base = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    for label, model, micros in (
        ("1a", "m1", 1),
        ("1b", "m1", 1),  # same route and instant as 1a
        ("2", "m2", 2),
        ("3", "m3", 1),  # same instant as route m1's rows
    ):
        pg_store.record_provider_benchmark(
            make_benchmark_sample(
                sample_id=label,
                provider="acme",
                model=f"acme/{model}",
                created_at=_iso(base + dt.timedelta(microseconds=micros)),
            )
        )

    def window(per_route_limit: int, limit: int) -> list[str]:
        rows = pg_store.provider_route_benchmark_samples(
            cutoff=_iso(base), per_route_limit=per_route_limit, limit=limit
        )
        return [row.id for row in rows]

    # Newest first; equal instants by id descending, within a route and across
    # routes.
    assert window(per_route_limit=2, limit=10) == ["2", "3", "1b", "1a"]
    assert window(per_route_limit=1, limit=10) == ["2", "3", "1b"]
    assert window(per_route_limit=2, limit=2) == ["2", "3"]


def test_legacy_benchmark_rows_join_the_window_after_the_backfill(pg_store: Any) -> None:
    now = dt.datetime.now(dt.UTC)
    legacy = make_benchmark_sample(
        sample_id="legacy",
        provider="acme",
        model="acme/m1",
        created_at=_iso(now - dt.timedelta(hours=1)),
    )
    # The write path before indexed_at: body only, indexed_at NULL.
    pg_store._run_transaction(
        lambda conn: pg_store._write_entity_tx(conn, "provider_benchmark", legacy.id, legacy)
    )

    def window_ids() -> list[str]:
        rows = pg_store.provider_route_benchmark_samples(
            cutoff=_iso(now - dt.timedelta(hours=48)), per_route_limit=48, limit=1_000
        )
        return [row.id for row in rows]

    assert window_ids() == []

    cursor = None
    while True:
        examined = pg_store.backfill_provider_benchmark_indexed_at_page(after=cursor, limit=1_000)
        if not examined:
            break
        cursor = examined[-1]

    assert window_ids() == ["legacy"]
