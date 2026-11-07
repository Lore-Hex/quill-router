"""The fast path's enabler on the native Spanner emulator
(docs/design/fast-admission-widening.md sections 5.3, 5.5, 5.6 and 10; section
11 item 6's emulator tests). tests/test_fastpath_enabler.py checks its logic
over the fake; these run its GoogleSQL and its transactions: nothing written
without the arm; an enable that writes the switch row alone and a disable
that revokes and holds nothing; a disable and a disable-all committed
between the run's read and its enable leaving the workspace off; a pause
committed between the run's read and a disable stopping that disable; and
the switch read's latency at ten times the fleet.

The other writers write as Go writes them: fastpathctl disable is the
fixture's disable, fastpathctl disable-all Store.DisableAll's statements,
fastpathctl enabler pause and arm the hold table's rows at the commit
timestamp (fastpath/internal/store/workspace.go and hold.go)."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from google.cloud.spanner_v1 import COMMIT_TIMESTAMP
from google.cloud.spanner_v1 import param_types as pt

from tests.conformance.spanner_ddl import DDL
from trusted_router import fastpath_enabler as fe
from trusted_router.services import fastpath_frontdoor as fd

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")

CONFIG = fe.EnablerConfig(mode="apply", max_rank=10)
# Ten times the fleet the tier job judged on 2026-10-02 (1,596 workspaces,
# trust_tier_bulk's note), an upper bound on the enabler's eligible count
# until a dry-run reports the real one.
SWITCH_ROWS = 16_000


@pytest.fixture
def database(native_emulator_resources):
    schema = tuple(DDL)
    database = native_emulator_resources[0]._instance.database(  # noqa: SLF001 - SDK has no public accessor
        "enabler-" + uuid4().hex[:12], ddl_statements=schema[:20],
    )
    database.create().result(timeout=120)
    try:
        for offset in range(20, len(schema), 20):
            database.update_ddl(schema[offset:offset + 20]).result(timeout=120)
        yield database
    finally:
        database.close()
        database.drop()


def _seed(database: Any, workspace: str, *, enabled: bool | None = None, tier: int = 3,
          leases: int = 0) -> None:
    now = datetime.now(UTC)
    with database.batch() as batch:
        batch.insert("tr_credit_balance",
                     columns=("workspace_id", "shard", "total_credits", "total_usage", "reserved", "trust_tier",
                              "trust_reconciled_through", "in_debt"),
                     values=[(workspace, 0, 1_000_000_000, 0, 0, tier, now, False)])
        batch.insert("tr_entities", columns=("kind", "id", "body", "updated_at"),
                     values=[("workspace", workspace, json.dumps({"id": workspace}), COMMIT_TIMESTAMP)])
        if enabled is not None:
            batch.insert("tr_fastpath_workspace", columns=("workspace_id", "enabled", "changed_at"),
                         values=[(workspace, enabled, COMMIT_TIMESTAMP)])
        for i in range(leases):
            batch.insert("tr_lease", columns=("workspace_id", "lease_id", "region", "workspace_shard", "owner_node",
                                              "owner_epoch", "state", "granted", "allocation", "expiry",
                                              "key_status_version"),
                         values=[(workspace, f"lease{i}", "us-central1", 0, "node", 1, "open", 10, 10,
                                  now + timedelta(minutes=5), 1)])


def _hold_row(database: Any, row: str, reason: str) -> None:
    with database.batch() as batch:
        batch.insert("tr_fastpath_workspace_hold", columns=("workspace_id", "reason", "held_at"),
                     values=[(row, reason, COMMIT_TIMESTAMP)])


def _arm(database: Any) -> None:
    _hold_row(database, fe.ARMED_ROW, "armed")


def _go_disable(database: Any, workspace: str) -> None:
    ops = fe.load_switch_operations()

    def tx(transaction: Any) -> None:
        fe._apply(transaction, ops["disable"], {"workspace": workspace, "reason": "operator", "detail": None},
                  param_types=pt, commit_timestamp=COMMIT_TIMESTAMP)

    database.run_in_transaction(tx)


def _go_disable_all(database: Any) -> None:
    def tx(transaction: Any) -> None:
        transaction.execute_update(
            "UPDATE tr_fastpath_workspace SET enabled = FALSE, changed_at = PENDING_COMMIT_TIMESTAMP() WHERE enabled")
        transaction.execute_update(
            "UPDATE tr_lease SET revoked = TRUE, revoked_at = CURRENT_TIMESTAMP() WHERE state = 'open' AND NOT revoked")
        held = transaction.execute_update(
            "UPDATE tr_fastpath_workspace_hold SET reason = 'kill' WHERE workspace_id = @fleet",
            params={"fleet": fe.FLEET_HOLD}, param_types={"fleet": pt.STRING})
        transaction.insert("tr_fastpath_kill", columns=("kill_at",), values=[(COMMIT_TIMESTAMP,)])
        if held == 0:
            transaction.insert("tr_fastpath_workspace_hold", columns=("workspace_id", "reason", "held_at"),
                               values=[(fe.FLEET_HOLD, "kill", COMMIT_TIMESTAMP)])

    database.run_in_transaction(tx)


def _rows(database: Any, sql: str) -> list[tuple[Any, ...]]:
    with database.snapshot() as snapshot:
        return [tuple(row) for row in snapshot.execute_sql(sql)]


def _switch(database: Any) -> dict[str, bool]:
    return {w: e for w, e, _ in _rows(database, fe.ENABLER_SWITCH_SQL)}


def _holds(database: Any) -> dict[str, str]:
    return {w: r for w, r, _ in _rows(database, fe.ENABLER_HOLDS_SQL)}


def _then(other: Any) -> Any:
    """A reader that takes the run's snapshot and then commits another
    writer's change, before the run's first write."""
    def reader(database: Any, param_types: Any, **kwargs: Any) -> fe.Snapshot:
        snapshot = fe.read_snapshot(database, param_types, **kwargs)
        other(database)
        return snapshot

    return reader


def test_nothing_is_written_without_the_arm_and_the_writes_hold_nothing(database: Any) -> None:
    _seed(database, "ws-synthetic-join")
    _seed(database, "ws-synthetic-leave", enabled=True, tier=1, leases=2)
    report = fe.run(database, pt, CONFIG, commit_timestamp=COMMIT_TIMESTAMP).report
    assert report["stopped"] == "unarmed"
    assert _switch(database) == {"ws-synthetic-leave": True} and _holds(database) == {}
    _arm(database)
    report = fe.run(database, pt, CONFIG, commit_timestamp=COMMIT_TIMESTAMP).report
    assert report["writes"] == {"disabled": 1, "enabled": 1} and report["revoked_leases"] == 2
    assert _switch(database) == {"ws-synthetic-join": True, "ws-synthetic-leave": False}
    assert _holds(database) == {fe.ARMED_ROW: "armed"}
    assert _rows(database, "SELECT COUNT(*) FROM tr_lease WHERE revoked") == [(2,)]
    # Each row's changed_at is its write's commit timestamp, as the report has it.
    changed = {fe._stamp(at) for _, _, at in _rows(database, fe.ENABLER_SWITCH_SQL)}
    assert set(report["commit_times"]) <= changed and len(report["commit_times"]) == 2
    # A second run finds every state matching and writes nothing.
    assert fe.run(database, pt, CONFIG, commit_timestamp=COMMIT_TIMESTAMP).report["writes"] == {}


def test_a_disable_between_the_read_and_the_enable_leaves_the_workspace_off(database: Any) -> None:
    _arm(database)
    _seed(database, "ws-synthetic-x")
    report = fe.run(database, pt, CONFIG, commit_timestamp=COMMIT_TIMESTAMP, reader=_then(lambda db: _go_disable(db, "ws-synthetic-x"))).report
    assert report["writes"] == {"held": 1}
    assert _switch(database) == {"ws-synthetic-x": False} and _holds(database)["ws-synthetic-x"] == "operator"


def test_a_disable_all_between_the_read_and_the_enable_leaves_the_workspace_off(database: Any) -> None:
    _arm(database)
    _seed(database, "ws-synthetic-x")
    _seed(database, "ws-synthetic-on", enabled=True)
    report = fe.run(database, pt, CONFIG, commit_timestamp=COMMIT_TIMESTAMP, reader=_then(_go_disable_all)).report
    assert report["stopped"] == "fleet"
    assert _switch(database) == {"ws-synthetic-on": False} and fe.FLEET_HOLD in _holds(database)


def test_a_pause_between_the_read_and_a_disable_stops_that_disable(database: Any) -> None:
    _arm(database)
    _seed(database, "ws-synthetic-off", enabled=True, tier=1)
    report = fe.run(database, pt, CONFIG, commit_timestamp=COMMIT_TIMESTAMP,
                     reader=_then(lambda db: _hold_row(db, fe.PAUSE_ROW, "pause"))).report
    assert report["stopped"] == "paused" and _switch(database) == {"ws-synthetic-off": True}


def test_an_override_changed_to_a_bypass_after_the_read_refuses_the_enable(database: Any) -> None:
    _arm(database)
    _seed(database, "ws-synthetic-x")

    def bypass(db: Any) -> None:
        with db.batch() as batch:
            batch.insert("tr_trust_override", columns=("workspace_id", "tier", "identity_bypass",
                                                       "operator_identity", "reason", "set_at"),
                         values=[("ws-synthetic-x", 3, True, "operator", "test", datetime.now(UTC))])

    report = fe.run(database, pt, CONFIG, commit_timestamp=COMMIT_TIMESTAMP, reader=_then(bypass)).report
    assert report["writes"] == {"ineligible:bypass": 1} and _switch(database) == {}


def test_the_switch_read_at_ten_times_the_fleet(database: Any) -> None:
    """Section 10: each node reads every enabled row each second, and all
    mode's Python copy every 5 s within ENABLED_COPY_READ_TIMEOUT_SECONDS.
    At SWITCH_ROWS enabled rows on the emulator the read fits that timeout;
    the figure is printed for the record."""
    for start in range(0, SWITCH_ROWS, 2_000):
        with database.batch() as batch:
            batch.insert("tr_fastpath_workspace", columns=("workspace_id", "enabled", "changed_at"),
                         values=[(f"ws-synthetic-{i:06d}", True, COMMIT_TIMESTAMP)
                                 for i in range(start, start + 2_000)])
    read = fd.spanner_enabled_workspaces(database)
    read()  # warm the session
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        rows = read()
        timings.append(time.perf_counter() - started)
        assert len(rows) == SWITCH_ROWS
    worst = max(timings)
    print(f"switch read of {SWITCH_ROWS} enabled rows on the emulator: median "  # noqa: T201 - the record
          f"{sorted(timings)[2] * 1000:.0f} ms, worst {worst * 1000:.0f} ms")
    assert worst < fd.ENABLED_COPY_READ_TIMEOUT_SECONDS
