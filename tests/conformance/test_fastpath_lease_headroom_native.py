"""The lease-aware insufficient-credit precheck on the native Spanner emulator
(docs/design/fast-admission-widening.md section 11 item 7).

tests/test_fastpath_lease_headroom_503.py checks the decision over the fake;
this runs its GoogleSQL against the production schema: the forced
tr_lease_by_state seek, the join to the donors, the live states, and the
workspace bound, beside the credit rows read in the same snapshot. Ids are
synthetic.
"""
from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from google.cloud.spanner_v1 import param_types as pt

from tests.conformance.spanner_ddl import DDL
from trusted_router import storage_gcp_authorize as authorize

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")

EXPIRY = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)


@pytest.fixture(params=["spanner-emulator"], ids=lambda backend: f"backend={backend}")
def database(request, native_emulator_resources):
    assert request.param == "spanner-emulator"
    schema = tuple(DDL)
    # The SDK Database exposes its Instance only as `_instance` (no public accessor).
    database = native_emulator_resources[0]._instance.database(  # noqa: SLF001 - SDK has no public accessor
        "lease503-" + uuid4().hex[:12], ddl_statements=schema[:20],
    )
    database.create().result(timeout=120)
    try:
        for offset in range(20, len(schema), 20):
            database.update_ddl(schema[offset:offset + 20]).result(timeout=120)
        yield database
    finally:
        database.close()
        database.drop()


def _seed(database, rows: list[tuple[int, int, int]], leases: list[tuple[str, list[tuple[int, int, int]]]],
          *, workspace: str | None = None) -> str:
    """rows: (total_credits, total_usage, reserved) per shard; leases: (state,
    [(credit shard, allocation, consumed)])."""
    workspace = workspace or "ws-" + uuid4().hex[:16]
    with database.batch() as batch:
        if rows:
            batch.insert(
                "tr_credit_balance",
                columns=("workspace_id", "shard", "total_credits", "total_usage", "reserved"),
                values=[(workspace, shard, *row) for shard, row in enumerate(rows)],
            )
        for state, donors in leases:
            lease_id = uuid4().hex[:22]
            allocation = sum(a for _s, a, _c in donors)
            consumed = sum(c for _s, _a, c in donors)
            draining = state != "open"
            closed = state == "closed"
            batch.insert(
                "tr_lease",
                columns=("workspace_id", "lease_id", "region", "workspace_shard", "owner_node", "owner_epoch",
                         "state", "granted", "allocation", "consumed", "expiry", "key_status_version",
                         "fence_time", "drained_by", "closed_at", "close_kind"),
                values=[(workspace, lease_id, "us-central1", 0, "node-a", 1, state, allocation, allocation,
                         consumed, EXPIRY, 0,
                         EXPIRY if draining else None, "owner" if draining else None,
                         EXPIRY if closed else None, "auditor" if closed else None)],
            )
            batch.insert(
                "tr_lease_donor",
                columns=("workspace_id", "lease_id", "credit_shard", "allocation", "consumed"),
                values=[(workspace, lease_id, shard, a, c) for shard, a, c in donors],
            )
    return workspace


def _precheck(database, workspace: str, estimate: int, *, lease_aware: bool = True) -> str:
    return authorize.credit_exhaustion_precheck(
        database, pt, workspace_id=workspace, estimate=estimate, lease_aware=lease_aware,
    )


@pytest.mark.parametrize(
    ("rows", "leases", "estimate", "verdict"),
    [
        # No lease: today's verdict.
        ([(1_000, 1_000, 0)], [], 100, authorize.EXHAUSTED),
        # An open lease holds the shortfall, and exactly covering counts.
        ([(1_000, 0, 900)], [("open", [(0, 900, 0)])], 450, authorize.LEASE_HELD),
        ([(1_000, 0, 600)], [("open", [(0, 600, 0)])], 1_000, authorize.LEASE_HELD),
        # A draining lease's remaining allocation counts; one short does not.
        ([(1_000, 1, 600)], [("draining", [(0, 900, 300)])], 1_000, authorize.EXHAUSTED),
        ([(1_000, 0, 600)], [("draining", [(0, 900, 300)])], 1_000, authorize.LEASE_HELD),
        # A closed lease holds nothing.
        ([(1_000, 0, 900)], [("closed", [(0, 900, 0)])], 450, authorize.EXHAUSTED),
        # Donors on two shards, live and closed together.
        ([(600, 0, 500), (400, 0, 300)],
         [("open", [(0, 500, 0), (1, 300, 0)]), ("closed", [(1, 100, 0)])], 500, authorize.LEASE_HELD),
    ],
)
def test_lease_aware_precheck_reads_the_live_leases(database, rows, leases, estimate, verdict) -> None:
    workspace = _seed(database, rows, leases)
    assert _precheck(database, workspace, estimate) == verdict
    # Not lease aware, the verdict is today's.
    today = authorize.EXHAUSTED if verdict == authorize.LEASE_HELD else verdict
    assert _precheck(database, workspace, estimate, lease_aware=False) == today


def test_another_workspaces_leases_are_not_its_headroom(database) -> None:
    workspace = _seed(database, [(1_000, 0, 0)], [])
    database.run_in_transaction(lambda tx: tx.execute_update(
        "UPDATE tr_credit_balance SET total_usage = 1000 WHERE workspace_id=@ws AND shard=0",
        params={"ws": workspace}, param_types={"ws": pt.STRING},
    ))
    # A neighbour sorting just after it, and one just before, hold plenty.
    _seed(database, [(10_000, 0, 9_000)], [("open", [(0, 9_000, 0)])], workspace=workspace + "0")
    _seed(database, [(10_000, 0, 9_000)], [("open", [(0, 9_000, 0)])], workspace=workspace[:-1])
    assert _precheck(database, workspace, 100) == authorize.EXHAUSTED


def test_a_marked_workspace_stays_exhausted_whatever_its_leases_hold(database) -> None:
    # Unmarked, the leases would explain the shortfall: -100 outside them.
    workspace = _seed(database, [(1_000, 600, 500)], [("open", [(0, 500, 0)])])
    assert _precheck(database, workspace, 100) == authorize.LEASE_HELD
    database.run_in_transaction(lambda tx: tx.execute_update(
        "UPDATE tr_credit_balance SET in_debt = TRUE WHERE workspace_id=@ws",
        params={"ws": workspace}, param_types={"ws": pt.STRING},
    ))
    assert _precheck(database, workspace, 100) == authorize.EXHAUSTED
