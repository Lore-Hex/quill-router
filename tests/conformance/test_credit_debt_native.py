"""The debt rules (fast-admission design section 4.7) on the native Spanner emulator.

tests/test_credit_debt_spanner.py checks the writers' logic over the fake;
these run their GoogleSQL: DML returning an expression, the mark predicates,
moves conditional on the headroom read, a DML read of a row an earlier DML
statement inserted, absorption of a real payment claim, and the mark-aware
exhaustion precheck. Two reproductions from review are here too: a recreated
shard row that was left unmarked beside a marked one, and a precheck that
did not read the mark.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from google.cloud.spanner_v1 import param_types as pt

from tests.conformance.spanner_ddl import DDL
from tests.conformance.spanner_sql_builders import NOW
from trusted_router import storage_gcp_authorize as authorize
from trusted_router import storage_gcp_counter_dml as counters
from trusted_router.storage_gcp_credit_debt import cover_or_mark, take_inflow
from trusted_router.storage_gcp_federated_settlement import _book_usage

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")

# (total_credits, total_usage, reserved, in_debt) per shard; None leaves the mark NULL.
Row = tuple[int, int, int, bool | None]


@pytest.fixture(params=["spanner-emulator"], ids=lambda backend: f"backend={backend}")
def database(request, native_emulator_resources):
    assert request.param == "spanner-emulator"
    schema = tuple(DDL)
    # The SDK Database exposes its Instance only as `_instance` (no public accessor).
    database = native_emulator_resources[0]._instance.database(  # noqa: SLF001 - SDK has no public accessor
        "debt-" + uuid4().hex[:12], ddl_statements=schema[:20],
    )
    database.create().result(timeout=120)
    try:
        for offset in range(20, len(schema), 20):
            database.update_ddl(schema[offset:offset + 20]).result(timeout=120)
        yield database
    finally:
        database.close()
        database.drop()


def _seed(database, rows: list[Row], *, first_shard: int = 0) -> str:
    workspace = "debt-" + uuid4().hex
    with database.batch() as batch:
        batch.insert(
            "tr_credit_balance",
            columns=("workspace_id", "shard", "total_credits", "total_usage", "reserved", "in_debt"),
            values=[(workspace, first_shard + index, *row) for index, row in enumerate(rows)],
        )
    return workspace


def _state(database, workspace: str) -> tuple[list[int], list[bool]]:
    with database.snapshot() as snapshot:
        rows = list(snapshot.execute_sql(
            "SELECT total_credits - total_usage - reserved, COALESCE(in_debt, FALSE) "
            "FROM tr_credit_balance WHERE workspace_id=@ws ORDER BY shard",
            params={"ws": workspace}, param_types={"ws": pt.STRING},
        ))
    return [int(row[0]) for row in rows], [bool(row[1]) for row in rows]


@pytest.mark.parametrize(
    ("rows", "headroom", "marks"),
    [
        # A negative shard the sum covers: covered from the lowest positive shard.
        ([(0, 50, 0, None), (100, 0, 0, None)], [0, 50], [False, False]),
        # A negative sum: every shard marked, nothing moved.
        ([(0, 150, 0, None), (100, 0, 0, None)], [-150, 100], [True, True]),
        # A mark the balance no longer bears out: cleared.
        ([(50, 0, 0, True), (50, 0, 0, True)], [50, 50], [False, False]),
        # Marks that disagree: made to agree.
        ([(0, 150, 0, False), (100, 0, 0, True)], [-150, 100], [True, True]),
    ],
)
def test_cover_or_mark(database, rows: list[Row], headroom: list[int], marks: list[bool]) -> None:
    workspace = _seed(database, rows)
    database.run_in_transaction(lambda tx: cover_or_mark(tx, pt, workspace, now=NOW))
    assert _state(database, workspace) == (headroom, marks)


def test_money_in_repays_the_lowest_negative_shard_first(database) -> None:
    workspace = _seed(database, [(0, 160, 0, True), (0, 30, 0, True), (100, 0, 0, True)])
    database.run_in_transaction(
        lambda tx: take_inflow(tx, pt, workspace, 40, landing_shard=None, absorb=None, now=NOW)
    )
    assert _state(database, workspace) == ([-120, -30, 100], [True, True, True])
    database.run_in_transaction(
        lambda tx: take_inflow(tx, pt, workspace, 100, landing_shard=None, absorb=None, now=NOW)
    )
    assert _state(database, workspace) == ([0, 0, 50], [False, False, False])


def test_an_overrun_release_covers_its_shard(database) -> None:
    workspace = _seed(database, [(100, 0, 50, None), (100, 0, 0, None)])
    assert database.run_in_transaction(
        lambda tx: counters.release_credit(tx, pt, workspace, 50, 180, shard=0)
    ) == 1
    assert _state(database, workspace) == ([0, 20], [False, False])


def test_a_return_on_a_marked_workspace_repays_a_lower_shard(database) -> None:
    workspace = _seed(database, [(0, 160, 0, True), (100, 0, 100, True)])
    assert database.run_in_transaction(
        lambda tx: counters.release_credit(tx, pt, workspace, 100, 0, shard=1)
    ) == 1
    assert _state(database, workspace) == ([-60, 0], [True, True])


def test_a_return_on_a_negative_shard_absorbs_only_what_is_left(database) -> None:
    # The shard was negative before the release: 100 - 95 - 10.
    workspace = _seed(database, [(100, 95, 10, None)])
    with database.batch() as batch:
        batch.insert(
            "tr_trust_event",
            columns=("workspace_id", "event_id", "kind", "provider", "occurred_at",
                     "recorded_at", "unrecovered_micro", "recovered_micro", "recovery_target"),
            values=[(workspace, "claim", "payment", "stripe", NOW, NOW, 10, 0, 10)],
        )
    assert database.run_in_transaction(
        lambda tx: counters.release_credit(tx, pt, workspace, 10, 0, shard=0)
    ) == 1
    assert _state(database, workspace) == ([0], [False])
    with database.snapshot() as snapshot:
        assert list(snapshot.execute_sql(
            "SELECT unrecovered_micro, recovered_micro FROM tr_trust_event WHERE workspace_id=@ws",
            params={"ws": workspace}, param_types={"ws": pt.STRING},
        )) == [[5, 5]]


@pytest.mark.parametrize("marked", [None, True])
def test_reservations_refuse_a_marked_row(database, marked: bool | None) -> None:
    workspace = _seed(database, [(100, 0, 0, marked)])
    accepted = not marked

    def reserve(tx) -> list[object]:
        sql, params, types = counters.reserve_credit_statement(pt, workspace, 10, check_pause=True)
        return [
            counters.reserve_credit(tx, pt, workspace, 10),
            counters.reserve_credit_with_pause(tx, pt, workspace, 10)[0],
            tx.execute_update(sql, params=params, param_types=types) == 1,
        ]

    assert database.run_in_transaction(reserve) == [accepted] * 3


@pytest.mark.parametrize("marked", [None, True])
def test_the_batch_release_falls_back_for_a_marked_row(database, marked: bool | None) -> None:
    workspace = _seed(database, [(100, 0, 100, marked)])
    statement = counters.release_credit_no_debt_statement(pt, workspace, 100, 70, shard=0)
    status, counts = database.run_in_transaction(lambda tx: tx.batch_update([statement]))
    assert status.code == 0
    assert counts == [0 if marked else 1]


def test_a_recreated_shard_row_is_marked_with_the_others(database) -> None:
    # Shard 0 is missing and shard 1 is marked: federated booking recreates
    # shard 0 at zero credits, and the rows end marked alike.
    workspace = _seed(database, [(100, 0, 0, True)], first_shard=1)
    database.run_in_transaction(lambda tx: _book_usage(tx, pt, workspace, 150, NOW))
    assert _state(database, workspace) == ([-150, 100], [True, True])


@pytest.mark.parametrize(
    ("rows", "estimate", "verdict"),
    [
        # Marked with a negative sum: refused whatever one shard holds.
        ([(0, 160, 0, True), (100, 0, 0, True)], 10, authorize.EXHAUSTED),
        # A stale mark: deferred to the transaction, which refuses and heals.
        ([(50, 0, 0, True)], 100, authorize.HEADROOM),
        ([(50, 0, 0, True)], 10, authorize.HEADROOM),
        # Unmarked and short: refused, as before.
        ([(50, 0, 0, None)], 100, authorize.EXHAUSTED),
    ],
)
def test_the_exhaustion_precheck_reads_the_mark(
    database, rows: list[Row], estimate: int, verdict: str,
) -> None:
    workspace = _seed(database, rows)
    assert authorize.credit_exhaustion_precheck(
        database, pt, workspace_id=workspace, estimate=estimate,
    ) == verdict
