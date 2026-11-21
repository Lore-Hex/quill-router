"""The fast path's enabler (docs/design/fast-admission-widening.md section 5;
section 11 item 6's tests): its eligibility, rank and margin, cause
precedence, the fact-change guard and the rank pace, the watermark freeze,
the guarded writes, its report, and its dormancy with the mode off. The SQL
is answered by tests/fakes/fastpath_enabler_spanner.py; the same writes on
the Spanner emulator are tests/conformance/test_fastpath_enabler_native.py."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from google.cloud.spanner_v1 import COMMIT_TIMESTAMP, param_types
from pydantic import ValidationError

from tests.fakes.fastpath_enabler_spanner import (
    T0,
    Credit,
    FakeDatabase,
    go_arm,
    go_disable,
    go_disable_all,
    go_pause,
)
from trusted_router import fastpath_enabler as fe
from trusted_router.config import Settings
from trusted_router.security import lookup_hash_api_key

ROOT = Path(__file__).resolve().parents[1]
NOW = T0 + timedelta(minutes=10)
BASE = fe.EnablerConfig(mode="apply", max_rank=10)


def _seed(db: FakeDatabase, workspace: str, *, usage: int = 0, enabled: bool | None = None,
          entity: dict[str, Any] | None = None, override: tuple[int, bool] | None = None,
          shards: int = 1, **credit: Any) -> None:
    db.credit[workspace] = [Credit(shard=s, usage=usage if s == 0 else 0, **credit) for s in range(shards)]
    db.entities[("workspace", workspace)] = json.dumps(entity if entity is not None else {"id": workspace})
    if override is not None:
        db.overrides[workspace] = override
    if enabled is not None:
        db.switch[workspace] = (enabled, T0 - timedelta(days=1))


def _armed(**kwargs: Any) -> FakeDatabase:
    db = FakeDatabase(**kwargs)
    go_arm(db)
    return db


def _run(db: FakeDatabase, config: fe.EnablerConfig = BASE, **kwargs: Any) -> fe.RunResult:
    return fe.run(db, param_types, config, now=NOW, commit_timestamp=COMMIT_TIMESTAMP, **kwargs)


def _hold_writes(db: FakeDatabase) -> list[tuple[str, str]]:
    return [w for w in db.writes if w[0] == "tr_fastpath_workspace_hold"]


def _overrun(db: FakeDatabase, workspace: str, by: int = 1) -> None:
    """Give the workspace one more shard, overrun by a microdollar, with no
    row marked: a row a settle left negative before the debt rules. Its
    other rows keep its balance, its tier and its marks as they were."""
    rows = db.credit[workspace]
    rows.append(Credit(shard=len(rows), credits=1_000_000, usage=1_000_000 + by))


# -- the fixture --------------------------------------------------------------


def test_the_package_carries_the_go_fixture_byte_for_byte() -> None:
    """The image holds src alone, so the enabler reads a copy of
    fastpath/testdata/switch_statements_v1.json from the package: the same
    bytes, or this fails, so the two cannot drift."""
    assert fe.FIXTURE.read_bytes() == (ROOT / "fastpath" / "testdata" / "switch_statements_v1.json").read_bytes()


def test_the_enablers_writes_are_the_fixtures_less_any_hold() -> None:
    fixture = fe.load_switch_operations()
    ours = fe.enabler_operations()
    assert ours["disable_unheld"] == fixture["disable_unheld"]
    assert ours["enable"]["mutations"] == fixture["enable"]["mutations"]
    removed = [s for s in fixture["enable"]["statements"] if s not in ours["enable"]["statements"]]
    assert [s["counts"] for s in removed] == ["holds_removed"]
    for op in ours.values():
        assert not fe._writes_hold_table(op)


def test_a_fixture_whose_enabler_writes_would_hold_is_refused(tmp_path: Path) -> None:
    fixture = json.loads(fe.FIXTURE.read_text())
    fixture["operations"]["disable_unheld"] = fixture["operations"]["disable"]
    bad = tmp_path / "switch.json"
    bad.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="writes the hold table"):
        fe.enabler_operations(bad)
    fixture = json.loads(fe.FIXTURE.read_text())
    fixture["version"] = 2
    bad.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="version"):
        fe.load_switch_operations(bad)



def test_the_hold_table_is_found_whatever_its_case(tmp_path: Path) -> None:
    """Spanner identifiers are case-insensitive, so a fixture statement
    naming the hold table in capitals is still a write to it."""
    fixture = json.loads(fe.FIXTURE.read_text())
    unheld = fixture["operations"]["disable_unheld"]
    unheld["statements"].append({"sql": "DELETE FROM TR_FASTPATH_WORKSPACE_HOLD WHERE workspace_id = @workspace_id",
                                 "counts": "shouting"})
    bad = tmp_path / "switch.json"
    bad.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="writes the hold table"):
        fe.enabler_operations(bad)

def test_the_writes_execute_the_fixtures_mutations_and_statements() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-on")
    _seed(db, "ws-synthetic-off", enabled=True, tier=2)
    db.open_leases["ws-synthetic-off"] = 3
    result = _run(db)
    fixture = fe.load_switch_operations()
    assert ("tr_fastpath_workspace", "ws-synthetic-on") in db.writes
    assert db.switch["ws-synthetic-on"][0] is True and db.switch["ws-synthetic-off"][0] is False
    revoke = fixture["disable_unheld"]["statements"][0]["sql"]
    assert revoke in db.queries
    assert ("tr_lease", "ws-synthetic-off") in db.writes and result.report["revoked_leases"] == 3
    # The switch row's changed_at is the transaction's commit timestamp.
    assert db.switch["ws-synthetic-on"][1] in {fe.datetime.fromisoformat(t) for t in result.report["commit_times"]}


def test_the_snapshot_reads_as_the_tier_job_reads() -> None:
    """The workspaces, the overrides and the entities are read by the
    tier job's own SQL (trust_tier_bulk), and the enable's override read by
    the store's (storage_gcp_trust), the parameter's name aside."""
    from trusted_router.storage_gcp_trust import TRUST_OVERRIDE_SQL
    from trusted_router.trust_tier_bulk import (
        BULK_ENTITIES_BY_ID_SQL,
        BULK_OVERRIDES_SQL,
        BULK_WORKSPACE_IDS_SQL,
    )

    assert fe.ENABLER_WORKSPACE_IDS_SQL == BULK_WORKSPACE_IDS_SQL
    assert fe.ENABLER_OVERRIDES_SQL == BULK_OVERRIDES_SQL
    assert fe.ENABLER_ENTITIES_SQL == BULK_ENTITIES_BY_ID_SQL
    assert fe.ENABLER_OVERRIDE_SQL == TRUST_OVERRIDE_SQL.replace("workspace_id=@pk", "workspace_id = @workspace")


def test_apply_refuses_to_start_without_the_commit_timestamp_sentinel() -> None:
    with pytest.raises(ValueError, match="commit timestamp"):
        fe.run(FakeDatabase(), param_types, BASE, now=NOW)


# -- eligibility ----------------------------------------------------------------


ELIGIBLE: dict[str, Any] = {}
FLIPS: list[tuple[str, dict[str, Any], str]] = [
    ("tier below 3", {"tier": 2}, "tier"),
    ("tier absent", {"tier": None}, "tier"),
    ("latched", {"latched_at": T0}, "latched"),
    ("paused", {"causes": ["chargeback"]}, "paused"),
    ("in debt", {"in_debt": True}, "in_debt"),
    ("under $100 to join", {"credits": 99_999_999}, "balance"),
    ("bypass off the include list", {"override": (3, True)}, "bypass"),
    ("deleted", {"entity": {"deleted": True}}, "deleted"),
    ("no workspace entity", {"entity": "missing"}, "deleted"),
    ("federated home", {"entity": {"federated_home": "aws-eu"}}, "federated"),
    ("on the exclude list", {"config": {"exclude": frozenset({"ws-synthetic-x"})}}, "excluded"),
    ("async shadow", {"config": {"async_shadow": frozenset({"ws-synthetic-x"})}}, "async_shadow"),
    ("synthetic client events", {"config": {"synthetic": frozenset({"ws-synthetic-x"})}}, "probe"),
    ("held", {"hold": "operator"}, "held"),
    ("a shard missing", {"gap": True}, "credit_rows"),
    ("a negative shard with none marked", {"overrun": True}, "debt_rules"),
]


@pytest.mark.parametrize(("name", "flip", "reason"), FLIPS, ids=[f[0] for f in FLIPS])
def test_each_exclusion_flips_one_fact(name: str, flip: dict[str, Any], reason: str) -> None:
    db = FakeDatabase()
    flip = dict(flip)
    config = dataclasses.replace(BASE, mode="dry-run", **flip.pop("config", {}))
    entity = flip.pop("entity", None)
    override = flip.pop("override", None)
    hold = flip.pop("hold", None)
    gap = flip.pop("gap", False)
    overrun = flip.pop("overrun", False)
    _seed(db, "ws-synthetic-x", entity=None if entity in (None, "missing") else entity, override=override, **flip)
    if entity == "missing":
        del db.entities[("workspace", "ws-synthetic-x")]
    if hold:
        db.holds["ws-synthetic-x"] = (hold, T0)
    if gap:
        db.credit["ws-synthetic-x"][0].shard = 1
    if overrun:
        _overrun(db, "ws-synthetic-x")
    report = _run(db, config).report
    assert report["excluded"] == {reason: 1}, name
    assert report["would_enable"] == {} and report["eligible"] == 0
    # The control: unflipped, the same workspace is eligible.
    clean = FakeDatabase()
    _seed(clean, "ws-synthetic-x")
    assert _run(clean, dataclasses.replace(BASE, mode="dry-run")).report["would_enable"] == {"rank": 1}


def test_an_enabled_workspace_stays_down_to_the_disable_balance() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-stay", enabled=True, credits=40_000_000)
    _seed(db, "ws-synthetic-join", credits=40_000_000)
    _seed(db, "ws-synthetic-leave", enabled=True, credits=39_999_999)
    report = _run(db).report
    assert db.switch["ws-synthetic-stay"][0] is True and "ws-synthetic-join" not in db.switch
    assert db.switch["ws-synthetic-leave"][0] is False
    assert report["would_disable"] == {"balance": 1}


def test_the_probes_keys_workspaces_are_left_out_by_their_reason(caplog: pytest.LogCaptureFixture) -> None:
    db = FakeDatabase()
    for workspace in ("ws-synthetic-monitor", "ws-synthetic-probe", "ws-synthetic-plain"):
        _seed(db, workspace)
    monitor, probe = lookup_hash_api_key("monitor-raw-key"), lookup_hash_api_key("probe-raw-key")
    db.entities[("api_key_lookup", monitor)] = json.dumps({"key_id": "k-mon"})
    db.entities[("api_key_lookup", probe)] = json.dumps({"key_id": "k-probe"})
    db.entities[("api_key", "k-mon")] = json.dumps({"workspace_id": "ws-synthetic-monitor"})
    db.entities[("api_key", "k-probe")] = json.dumps({"workspace_id": "ws-synthetic-probe"})
    hashes = fe.probe_hashes_from_environment({"FASTPATH_ENABLER_MONITOR_KEY": "monitor-raw-key",
                                               "FASTPATH_ENABLER_STAGE_D_PROBE_KEY": "probe-raw-key"})
    assert hashes == {monitor: "monitor_key", probe: "probe"}
    with caplog.at_level(logging.INFO, logger="trusted_router.fastpath_enabler"):
        report = _run(db, dataclasses.replace(BASE, mode="dry-run"), probe_hashes=hashes).report
    assert report["excluded"] == {"monitor_key": 1, "probe": 1} and report["would_enable"] == {"rank": 1}
    # Their IDs go apart, for the exclude list; never the keys.
    lists = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fastpath_enabler.list ")]
    assert "fastpath_enabler.list probes=ws-synthetic-monitor:monitor_key,ws-synthetic-probe:probe" in lists
    assert not any("raw-key" in line or monitor in line or probe in line for line in lists)


# -- the rank, the include list and the margin -------------------------------------


def _fleet(db: FakeDatabase, n: int, *, enabled: bool = False) -> list[str]:
    names = [f"ws-synthetic-{i:03d}" for i in range(n)]
    for i, name in enumerate(names):
        _seed(db, name, usage=(n - i) * 1_000, enabled=enabled or None)
    return names


def test_the_rank_takes_the_largest_and_the_include_list_is_exempt() -> None:
    db = _armed()
    names = _fleet(db, 15)
    _seed(db, "ws-synthetic-small", usage=1, override=(3, True))
    config = dataclasses.replace(BASE, max_rank=10, include=frozenset({"ws-synthetic-small"}))
    report = _run(db, config).report
    assert sorted(w for w, (on, _) in db.switch.items() if on) == sorted([*names[:10], "ws-synthetic-small"])
    assert report["would_enable"] == {"include": 1, "rank": 10}
    assert report["bypass"] == {"included": 1, "excluded": 0}


def test_ties_rank_by_id() -> None:
    db = FakeDatabase()
    for name in ("ws-synthetic-b", "ws-synthetic-a", "ws-synthetic-c"):
        _seed(db, name, usage=5)
    plan = fe.plan(fe.read_snapshot(db, param_types), dataclasses.replace(BASE, max_rank=2), now=NOW)
    assert plan.enables == ["ws-synthetic-a", "ws-synthetic-b"]


def test_the_margin_keeps_near_ties_and_paces_a_real_fall() -> None:
    """max_rank 10 leaves only past 15 (a margin of five): rank 11 is kept
    and counted toward nothing; ranks 16 and past are disabled, counted in
    the rank's pace, not the fact-change guard."""
    db = _armed()
    names = _fleet(db, 18, enabled=True)
    result = _run(db)
    report = result.report
    assert all(db.switch[w][0] for w in names[:15])
    assert [w for w in names if not db.switch[w][0]] == names[15:]
    assert report["would_disable"] == {"rank": 3} and report["capped"]["guarded"] == 0
    assert result.plan is not None and not result.plan.mass_disable


def test_no_margin_at_max_rank_zero() -> None:
    db = _armed()
    names = _fleet(db, 3, enabled=True)
    config = dataclasses.replace(BASE, max_rank=0, include=frozenset({names[0]}))
    report = _run(db, config).report
    assert db.switch[names[0]][0] and not db.switch[names[1]][0] and not db.switch[names[2]][0]
    assert report["would_disable"] == {"rank": 2} and report["would_enable"] == {}


def test_the_rank_pace_disables_at_most_the_cap_lowest_rank_first() -> None:
    db = _armed()
    names = _fleet(db, 60, enabled=True)
    result = _run(db, dataclasses.replace(BASE, max_rank=10))
    off = [w for w in names if not db.switch[w][0]]
    # Cap: the larger of 20 and a tenth of 60. Lowest rank first.
    assert off == names[-20:]
    assert result.report["capped"]["rank"] == 60 - 15 - 20
    assert result.plan is not None and not result.plan.mass_disable


def test_enables_are_capped_per_run() -> None:
    db = _armed()
    _fleet(db, fe.MAX_ENABLES + 7)
    report = _run(db, dataclasses.replace(BASE, max_rank=10_000)).report
    assert report["writes"] == {"enabled": fe.MAX_ENABLES} and report["capped"]["enables"] == 7


# -- precedence, the guard and the whole exclude list ---------------------------------


def test_a_mass_latch_is_disabled_uncapped_and_raises_nothing(caplog: pytest.LogCaptureFixture) -> None:
    db = _armed()
    names = _fleet(db, 40, enabled=True)
    for name in names:
        db.credit[name][0].latched_at = T0
    db.credit[names[0]][0].credits = 0  # latched and under the balance: counted once, as latched
    with caplog.at_level(logging.ERROR, logger="trusted_router.fastpath_enabler"):
        result = _run(db, dataclasses.replace(BASE, max_rank=100))
    assert all(not db.switch[w][0] for w in names)
    assert result.report["would_disable"] == {"latched": 40}
    assert result.plan is not None and not result.plan.mass_disable
    assert "fastpath_enabler_mass_disable" not in caplog.text


def test_a_mass_change_of_facts_writes_none_of_them_and_alerts(caplog: pytest.LogCaptureFixture) -> None:
    db = _armed()
    names = _fleet(db, 40, enabled=True)
    for name in names[:21]:
        db.credit[name][0].tier = 1
    db.credit[names[30]][0].causes = ["chargeback"]
    with caplog.at_level(logging.ERROR, logger="trusted_router.fastpath_enabler"):
        result = _run(db, dataclasses.replace(BASE, max_rank=100))
    assert all(db.switch[w][0] for w in names[:21])
    assert not db.switch[names[30]][0]  # the uncapped cause still goes
    assert result.report["capped"]["guarded"] == 21 and result.report["capped"]["cap"] == 20
    assert "ERROR" in caplog.text and "fastpath_enabler_mass_disable guarded=21 cap=20" in caplog.text
    # At the cap itself the guard holds: twenty go.
    db2 = _armed()
    names2 = _fleet(db2, 40, enabled=True)
    for name in names2[:20]:
        db2.credit[name][0].tier = 1
    _run(db2, dataclasses.replace(BASE, max_rank=100))
    assert [w for w in names2 if not db2.switch[w][0]] == names2[:20]


def test_an_exclude_entry_is_applied_whole() -> None:
    db = _armed()
    names = _fleet(db, 40, enabled=True)
    result = _run(db, dataclasses.replace(BASE, max_rank=100, exclude=frozenset(names[:30])))
    assert [w for w in names if not db.switch[w][0]] == names[:30]
    assert result.report["would_disable"] == {"excluded": 30}
    assert result.plan is not None and not result.plan.mass_disable


# -- the debt rules ------------------------------------------------------------------


def _debt_rows(headroom: list[int], marks: list[bool] | None = None, *, reserved: int = 0, tier: int = 3,
               latched: bool = False) -> list[fe.CreditRow]:
    """Credit rows with the headroom given, each row's credit being its
    headroom and its reservation over no usage."""
    marks = marks if marks is not None else [False] * len(headroom)
    return [fe.CreditRow(shard=shard, tier=tier, latched=latched, paused=False, in_debt=mark,
                         credits=value + reserved, usage=0, reconciled_through=T0, reserved=reserved)
            for shard, (value, mark) in enumerate(zip(headroom, marks, strict=True))]


INT64_MAX = 2**63 - 1
DEBT_RULES: list[tuple[str, list[int], list[bool] | None, bool]] = [
    ("no row negative, none marked", [100, 0, 5], None, False),
    ("a negative row unmarked", [100, -1], None, True),  # Go's own case, identity_test.go
    ("one row, negative, unmarked", [-1], None, True),
    ("a negative sum, none marked", [-100, 30], None, True),
    ("in debt and marked on every row", [-100, 30], [True, True], False),
    ("marked while solvent", [70], [True], True),  # Go's: "marked and their signed sum is 70"
    ("marked, a row negative, the sum zero", [-5, 5], [True, True], True),
    ("the marks differ", [100, 100], [True, False], True),  # Go's: "the rows' marks differ"
    ("the marks differ in real debt", [-100, 30], [False, True], True),
    # Exact sums, as Go's big.Int: an int64 would wrap these to the other sign.
    ("marked, a sum past int64", [INT64_MAX, INT64_MAX, -1], [True, True, True], True),
    ("marked, a sum below int64", [-INT64_MAX, -INT64_MAX, 1], [True, True, True], False),
]


@pytest.mark.parametrize(("name", "headroom", "marks", "breaks"), DEBT_RULES, ids=[c[0] for c in DEBT_RULES])
def test_the_debt_rules_are_read_as_the_nodes_audit_reads_them(name: str, headroom: list[int],
                                                              marks: list[bool] | None, breaks: bool) -> None:
    assert fe.breaks_debt_rules(_debt_rows(headroom, marks)) is breaks, name
    # A row's reservations are off its headroom, as they are in the audit's read.
    assert fe.breaks_debt_rules(_debt_rows(headroom, marks, reserved=7)) is breaks, name


def test_the_debt_rules_are_the_switch_the_nodes_audit_ends_with() -> None:
    """breaks_debt_rules is a copy of the switch that ends Go's
    store.CheckIdentity, on the same columns. If the audit's rule or its
    read changes, this fails, and the enabler's copy changes with it."""
    go = (ROOT / "fastpath" / "internal" / "store" / "identity.go").read_text()
    assert "SELECT shard, reserved, total_credits - total_usage - reserved, COALESCE(in_debt, FALSE)" in go
    switch = go[go.index("negative, marked, alike := false, marks[0], true"):]
    assert re.findall(r"^\tcase (.+):$", switch, re.M) == [
        "!alike", "negative && !marked", "marked && signed.Sign() >= 0"]
    # And the enabler reads those columns, in the snapshot and in the enable.
    for sql in (fe.ENABLER_BALANCE_SQL, fe.ENABLER_CREDIT_ROWS_SQL):
        assert "in_debt, total_credits, total_usage, reserved" in sql
    assert fe.breaks_debt_rules([]) is False  # no rows is "credit_rows", as the audit's ErrCreditRowsIncomplete


def test_a_reservation_counts_against_a_rows_headroom() -> None:
    """The headroom is credit less usage less reservations: a row whose
    reservations alone exceed what it has left is negative."""
    row = fe._row((0, 3, None, None, None, False, 100, 50, 60, T0))
    assert row.reserved == 60 and fe.breaks_debt_rules([row])
    assert not fe.breaks_debt_rules([fe._row((0, 3, None, None, None, None, 100, 50, 50, T0))])
    db = FakeDatabase()
    _seed(db, "ws-synthetic-x", shards=2)
    db.credit["ws-synthetic-x"][1].reserved = db.credit["ws-synthetic-x"][1].credits + 1
    report = _run(db, dataclasses.replace(BASE, mode="dry-run")).report
    assert report["excluded"] == {"debt_rules": 1} and report["would_enable"] == {}


def test_a_negative_shard_with_none_marked_is_never_enabled(caplog: pytest.LogCaptureFixture) -> None:
    """The state the fleet audit faulted on 2026-11-21, a day the join
    balance came down: sixteen shards, one overrun before the debt rules
    and never covered, the signed sum well over the join balance, no row
    marked. It is left out as debt_rules and listed nowhere to enable,
    while its twin, with a row at zero in place of the negative one, joins."""
    db = _armed()
    for name in ("ws-synthetic-legacy", "ws-synthetic-twin"):
        _seed(db, name, shards=16, credits=10_000_000)
    db.credit["ws-synthetic-legacy"][4].usage = 10_400_000
    db.credit["ws-synthetic-twin"][4].usage = 10_000_000
    with caplog.at_level(logging.INFO, logger="trusted_router.fastpath_enabler"):
        result = _run(db)
    assert {w for w, (on, _) in db.switch.items() if on} == {"ws-synthetic-twin"}
    assert "ws-synthetic-legacy" not in db.switch
    assert result.report["excluded"] == {"debt_rules": 1} and result.report["eligible"] == 1
    assert result.report["would_enable"] == {"rank": 1} and result.report["writes"] == {"enabled": 1}
    assert "fastpath_enabler.list enable=ws-synthetic-twin" in [r.getMessage() for r in caplog.records]
    # Covered, as scripts/settle_negative_shards.py covers it (the negative
    # row from the lowest positive one), it joins at the next run.
    db.credit["ws-synthetic-legacy"][0].credits -= 400_000
    db.credit["ws-synthetic-legacy"][4].credits += 400_000
    assert _run(db).report["writes"] == {"enabled": 1} and db.switch["ws-synthetic-legacy"][0] is True


def test_the_enables_own_transaction_rechecks_the_debt_rules() -> None:
    """The enable reads the rows again in its own transaction, their
    reservations too, and writes nothing for rows that break the rules."""
    for column in ("usage", "reserved"):
        db = _armed()
        _seed(db, "ws-synthetic-x", shards=2)

        def goes_negative(db: FakeDatabase, column: str = column) -> None:
            row = db.credit["ws-synthetic-x"][1]
            setattr(row, column, row.credits + 1)

        db.before_transaction.append(goes_negative)
        report = _run(db).report
        assert "ws-synthetic-x" not in db.switch and report["writes"] == {"ineligible:debt_rules": 1}, column


def test_an_enabled_workspace_that_breaks_the_debt_rules_is_disabled_unheld(caplog: pytest.LogCaptureFixture) -> None:
    """An enabled workspace found with a negative shard and no mark leaves
    at the next run, as for any guarded cause: the switch off, its leases
    revoked, no hold. It does not come back while its rows stay so."""
    db = _armed()
    names = _fleet(db, 40, enabled=True)
    for name in names[:2]:
        _overrun(db, name)
        db.open_leases[name] = 1
    with caplog.at_level(logging.INFO, logger="trusted_router.fastpath_enabler"):
        result = _run(db, dataclasses.replace(BASE, max_rank=100))
    assert [w for w in names if not db.switch[w][0]] == names[:2]
    assert result.report["would_disable"] == {"debt_rules": 2} and result.report["excluded"] == {"debt_rules": 2}
    assert result.report["writes"] == {"disabled": 2} and result.report["revoked_leases"] == 2
    assert result.plan is not None and not result.plan.mass_disable
    assert _hold_writes(db) == [] and not any(name in db.holds for name in names)
    lists = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fastpath_enabler.list ")]
    assert f"fastpath_enabler.list disable={names[0]}:debt_rules,{names[1]}:debt_rules" in lists
    again = _run(db, dataclasses.replace(BASE, max_rank=100)).report
    assert again["would_enable"] == {} and again["would_disable"] == {} and again["excluded"] == {"debt_rules": 2}
    assert [w for w in names if not db.switch[w][0]] == names[:2]


def test_a_mass_of_rows_that_break_the_debt_rules_is_held_by_the_guard(caplog: pytest.LogCaptureFixture) -> None:
    """The grant does not refuse a workspace for a negative unmarked row,
    so this disable is a real change and is guarded: past the cap none of
    them goes and the run alerts, since that many at once is a writer or
    a reading gone wrong; at the cap they go."""
    db = _armed()
    names = _fleet(db, 40, enabled=True)
    for name in names[:21]:
        _overrun(db, name)
    db.credit[names[30]][0].in_debt = True
    with caplog.at_level(logging.ERROR, logger="trusted_router.fastpath_enabler"):
        result = _run(db, dataclasses.replace(BASE, max_rank=100))
    assert all(db.switch[w][0] for w in names[:21])
    assert not db.switch[names[30]][0]  # the uncapped cause still goes
    assert result.report["would_disable"] == {"debt_rules": 21, "in_debt": 1}
    assert result.report["capped"]["guarded"] == 21 and result.report["capped"]["cap"] == 20
    assert "fastpath_enabler_mass_disable guarded=21 cap=20" in caplog.text
    db2 = _armed()
    names2 = _fleet(db2, 40, enabled=True)
    for name in names2[:20]:
        _overrun(db2, name)
    _run(db2, dataclasses.replace(BASE, max_rank=100))
    assert [w for w in names2 if not db2.switch[w][0]] == names2[:20]


def test_the_debt_rules_cause_follows_the_marks_and_leads_the_tier() -> None:
    """Counted once, under the first cause: rows with any mark are in_debt
    (uncapped, refused by the grant) though they break the rules too; a
    latch or a pause leads as before; incomplete rows are credit_rows
    alone, as the audit gives no verdict on them; and a negative unmarked
    row leads the tier and the balance, so the report's debt_rules counts
    every such workspace that no cause before it already keeps out."""
    def causes(rows: list[fe.CreditRow]) -> list[str]:
        return fe.credit_causes(rows, None, enabled=False, included=False, config=BASE)

    rich = 1_000_000_000
    assert causes(_debt_rows([rich, -1])) == ["debt_rules"]
    assert causes(_debt_rows([5, -1], tier=0)) == ["debt_rules", "tier", "balance"]
    assert causes(_debt_rows([rich, rich], [True, False])) == ["in_debt", "debt_rules"]
    assert causes(_debt_rows([rich], [True])) == ["in_debt", "debt_rules"]
    assert causes(_debt_rows([-100, 30], [True, True])) == ["in_debt", "balance"]
    assert causes(_debt_rows([rich, -1], latched=True)) == ["latched", "debt_rules", "tier"]
    gap = [dataclasses.replace(row, shard=row.shard + 1) for row in _debt_rows([rich, -1])]
    assert causes(gap) == ["credit_rows"]
    order = fe.PRECEDENCE
    assert order.index("in_debt") < order.index("credit_rows") < order.index("debt_rules") < order.index("tier")
    assert "debt_rules" in fe.GUARDED and "debt_rules" not in fe.UNCAPPED + fe.WHOLE


def test_marked_rows_that_break_the_rules_leave_uncapped_as_in_debt(caplog: pytest.LogCaptureFixture) -> None:
    """Marks that differ, or a mark over a balance that is not negative,
    break the rules too, but the grant refuses any marked row, so they
    leave as in_debt, uncapped: thirty at once raise nothing."""
    db = _armed()
    names = _fleet(db, 40, enabled=True)
    for name in names[:15]:
        db.credit[name][0].in_debt = True  # marked while solvent
    for name in names[15:30]:
        db.credit[name].append(Credit(shard=1, in_debt=True))  # the marks differ
    with caplog.at_level(logging.ERROR, logger="trusted_router.fastpath_enabler"):
        result = _run(db, dataclasses.replace(BASE, max_rank=100))
    assert [w for w in names if not db.switch[w][0]] == names[:30]
    assert result.report["would_disable"] == {"in_debt": 30} and result.report["capped"]["guarded"] == 0
    assert "fastpath_enabler_mass_disable" not in caplog.text


def test_the_documents_name_the_debt_rules_exclusion() -> None:
    """The report's excluded and would_disable counts carry debt_rules;
    the design's exclusion set and guard, and the runbook's rows for the
    audit's fault and the guard's alert, say what it is and what to do."""
    design = (ROOT / "docs" / "design" / "fast-admission-widening.md").read_text()
    runbook = (ROOT / "docs" / "runbooks" / "fastpath-alerts.md").read_text()
    exclusions = design[design.index("### (c) The exclusion set"):design.index("### (d) The enabler job")]
    assert "counted in the report as `debt_rules`" in exclusions
    run = design[design.index("### 5.3 What a run does"):design.index("### 5.4 The report")]
    assert "credit rows that break the debt rules" in run
    fault = next(line for line in runbook.splitlines() if line.startswith("| `fleet_audit_fault` |"))
    assert "`debt_rules`" in fault and "scripts/settle_negative_shards.py" in fault
    guard = next(line for line in runbook.splitlines() if line.startswith("| `mass_disable` |"))
    assert "credit rows that break the debt rules" in guard


# -- the watermark freeze ------------------------------------------------------------


def test_a_stale_or_absent_watermark_freezes_the_enable_and_no_disable(caplog: pytest.LogCaptureFixture) -> None:
    db = _armed()
    _seed(db, "ws-synthetic-stale", reconciled=NOW - timedelta(seconds=3_601))
    _seed(db, "ws-synthetic-absent", reconciled=None)
    _seed(db, "ws-synthetic-fresh", reconciled=NOW - timedelta(seconds=3_600))
    _seed(db, "ws-synthetic-override", reconciled=None, override=(3, False))
    _seed(db, "ws-synthetic-on-stale", enabled=True, reconciled=None)
    _seed(db, "ws-synthetic-on-latched", enabled=True, reconciled=None, latched_at=T0)
    with caplog.at_level(logging.ERROR, logger="trusted_router.fastpath_enabler"):
        report = _run(db).report
    on = {w for w, (enabled, _) in db.switch.items() if enabled}
    assert on == {"ws-synthetic-fresh", "ws-synthetic-override", "ws-synthetic-on-stale"}
    assert report["frozen"] == 2
    # Three of five eligible are stale: more than half, the reconciler's.
    assert report["stale"] == 3 and report["eligible"] == 5
    assert "fastpath_enabler_trust_stale stale=3 eligible=5" in caplog.text


def test_the_enables_own_transaction_rechecks_the_watermark() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-x")

    def reconciler_stalls(db: FakeDatabase) -> None:
        db.credit["ws-synthetic-x"][0].reconciled = None

    db.before_transaction.append(reconciler_stalls)
    report = _run(db).report
    assert "ws-synthetic-x" not in db.switch and report["writes"] == {"frozen": 1}


# -- the guarded writes ---------------------------------------------------------------


def test_nothing_is_written_without_the_arm() -> None:
    db = FakeDatabase()
    _seed(db, "ws-synthetic-on")
    _seed(db, "ws-synthetic-off", enabled=True, tier=1)
    report = _run(db).report
    assert db.writes == [] and report["stopped"] == "unarmed"
    assert report["writes"] == {"unarmed": 1} and report["held"]["armed"] is False


@pytest.mark.parametrize("row", [fe.FLEET_HOLD, fe.PAUSE_ROW])
def test_the_fleet_hold_or_the_pause_stops_every_write(row: str) -> None:
    db = _armed()
    db.holds[row] = ("kill" if row == fe.FLEET_HOLD else "pause", T0)
    _seed(db, "ws-synthetic-on")
    _seed(db, "ws-synthetic-off", enabled=True, tier=1)
    report = _run(db).report
    assert db.writes == [] and report["stopped"] == ("fleet" if row == fe.FLEET_HOLD else "paused")


def test_a_disable_between_the_read_and_the_enable_leaves_the_workspace_off() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-x")
    db.before_transaction.append(lambda d: go_disable(d, "ws-synthetic-x"))
    report = _run(db).report
    assert db.switch["ws-synthetic-x"][0] is False and report["writes"] == {"held": 1}


def test_a_disable_all_between_the_read_and_the_enable_leaves_the_workspace_off() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-x")
    _seed(db, "ws-synthetic-on", enabled=True)
    db.before_transaction.append(go_disable_all)
    report = _run(db).report
    assert "ws-synthetic-x" not in db.switch and db.switch["ws-synthetic-on"][0] is False
    assert report["stopped"] == "fleet"


def test_a_pause_between_the_read_and_a_disable_stops_that_disable() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-off", enabled=True, tier=1)
    db.before_transaction.append(go_pause)
    report = _run(db).report
    assert db.switch["ws-synthetic-off"][0] is True and report["stopped"] == "paused"
    assert not [w for w in db.writes if w[0] == "tr_fastpath_workspace"]


def test_the_credit_recheck_refuses_an_enable_whose_rows_changed() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-x")

    def spends(db: FakeDatabase) -> None:
        db.credit["ws-synthetic-x"][0].usage = db.credit["ws-synthetic-x"][0].credits

    db.before_transaction.append(spends)
    report = _run(db).report
    assert "ws-synthetic-x" not in db.switch and report["writes"] == {"ineligible:balance": 1}


def test_the_bypass_is_read_from_the_override_in_the_snapshot_and_the_enable() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-x", override=(3, False))
    _seed(db, "ws-synthetic-in", override=(3, True))
    config = dataclasses.replace(BASE, include=frozenset({"ws-synthetic-in"}))

    def bypassed(db: FakeDatabase) -> None:
        db.overrides["ws-synthetic-x"] = (3, True)

    db.before_transaction.append(bypassed)
    report = _run(db, config).report
    assert "ws-synthetic-x" not in db.switch and db.switch["ws-synthetic-in"][0] is True
    assert report["writes"] == {"enabled": 1, "ineligible:bypass": 1}


def test_no_code_path_writes_the_hold_table() -> None:
    """Enables, disables of every cause, and a held enabled row: the hold
    table is read, never written, and a disabled workspace has no hold."""
    db = _armed()
    _seed(db, "ws-synthetic-join")
    _seed(db, "ws-synthetic-held-on", enabled=True)
    db.holds["ws-synthetic-held-on"] = ("operator", T0)
    for i, flip in enumerate([{"tier": 1}, {"latched_at": T0}, {"causes": ["x"]}, {"in_debt": True}]):
        _seed(db, f"ws-synthetic-off-{i}", enabled=True, **flip)
    before = dict(db.holds)
    _run(db)
    assert _hold_writes(db) == [] and db.holds == before
    assert all(not db.switch[f"ws-synthetic-off-{i}"][0] for i in range(4))
    assert not any(w.startswith("ws-synthetic-off") for w in db.holds)
    assert db.switch["ws-synthetic-held-on"][0] is False  # disabled uncapped, hold kept as it was
    assert db.switch["ws-synthetic-join"][0] is True


def test_dry_run_writes_nothing() -> None:
    db = _armed()
    _seed(db, "ws-synthetic-on")
    _seed(db, "ws-synthetic-off", enabled=True, tier=1)
    report = _run(db, dataclasses.replace(BASE, mode="dry-run")).report
    assert db.writes == [] and db.transactions == 0
    assert report["would_enable"] == {"rank": 1} and report["would_disable"] == {"tier": 1}
    assert "writes" not in report


def test_a_failed_chunk_aborts_the_run_with_no_write() -> None:
    db = _armed(fail_on=fe.ENABLER_BALANCE_SQL)
    _fleet(db, 3)
    with pytest.raises(fe.SnapshotError, match="RuntimeError"):
        _run(db)
    assert db.writes == [] and db.transactions == 0
    db2 = _armed(fail_on=fe.ENABLER_BALANCE_SQL)
    _fleet(db2, 2_500)
    db2.fail_on = None
    calls = [0]
    answer = db2.answer

    def third_chunk_fails(sql: str, params: Any) -> Any:
        if sql == fe.ENABLER_BALANCE_SQL:
            calls[0] += 1
            if calls[0] == 3:
                raise RuntimeError("deadline exceeded")
        return answer(sql, params)

    db2.answer = third_chunk_fails  # type: ignore[method-assign]
    with pytest.raises(fe.SnapshotError):
        _run(db2, dataclasses.replace(BASE, max_rank=10_000))
    assert db2.writes == [] and calls[0] == 3



def test_a_write_that_raises_reports_the_writes_before_it(caplog: pytest.LogCaptureFixture,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    db = _armed()
    _fleet(db, 3)
    real = fe.write_enable
    calls = [0]

    def second_fails(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("retries exhausted")
        return real(*args, **kwargs)

    monkeypatch.setattr(fe, "write_enable", second_fails)
    with caplog.at_level(logging.ERROR, logger="trusted_router.fastpath_enabler"), \
            pytest.raises(RuntimeError, match="retries exhausted"):
        _run(db, dataclasses.replace(BASE, max_rank=10))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fastpath_enabler.write_failed ")]
    assert len(lines) == 1
    report = json.loads(lines[0].split(" ", 1)[1])
    assert report["failed"] == "RuntimeError" and report["writes"] == {"enabled": 1}
    assert len(report["commit_times"]) == 1 and report["stopped"] is None
    assert "retries exhausted" not in lines[0]

# -- the report -------------------------------------------------------------------------


def test_unheld_disabled_counts_every_unheld_disabled_row_whatever_its_rank() -> None:
    db = _armed()
    kill = T0 - timedelta(hours=1)
    db.kills.append(kill)
    db.holds[fe.FLEET_HOLD] = ("kill", kill)
    _seed(db, "ws-synthetic-killed")
    db.switch["ws-synthetic-killed"] = (False, kill)
    _seed(db, "ws-synthetic-mine", usage=1)
    db.switch["ws-synthetic-mine"] = (False, T0)
    _seed(db, "ws-synthetic-held")
    db.switch["ws-synthetic-held"] = (False, T0)
    db.holds["ws-synthetic-held"] = ("operator", T0)
    db.switch["ws-synthetic-gone"] = (False, T0)  # no credit rows, no entity: excluded
    report = _run(db, dataclasses.replace(BASE, mode="dry-run", max_rank=0)).report
    assert report["unheld_disabled"] == {"total": 3, "under_kill": 1, "other": 2}
    assert report["held"] == {"fleet": True, "paused": False, "armed": True, "workspaces": 1}


def test_the_report_is_one_json_line_and_the_lists_go_apart(caplog: pytest.LogCaptureFixture) -> None:
    db = _armed()
    _seed(db, "ws-synthetic-on")
    _seed(db, "ws-synthetic-off", enabled=True, tier=1)
    with caplog.at_level(logging.INFO, logger="trusted_router.fastpath_enabler"):
        _run(db)
    reports = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fastpath_enabler.report ")]
    assert len(reports) == 1
    report = json.loads(reports[0].split(" ", 1)[1])
    assert report["mode"] == "apply" and report["writes"] == {"disabled": 1, "enabled": 1}
    assert len(report["commit_times"]) == 2
    assert "ws-synthetic" not in reports[0]
    lists = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fastpath_enabler.list ")]
    assert "fastpath_enabler.list enable=ws-synthetic-on" in lists
    assert "fastpath_enabler.list disable=ws-synthetic-off:tier" in lists


# -- dormancy and settings ---------------------------------------------------------------


def test_off_reads_nothing(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off is the default and reads nothing: no database is even given, and
    main builds no store."""
    assert Settings(_env_file=None, environment="test").fastpath_enabler_mode == "off"  # type: ignore[call-arg]
    with caplog.at_level(logging.INFO, logger="trusted_router.fastpath_enabler"):
        assert fe.run(None, None, fe.EnablerConfig()).report == {"mode": "off"}

    def no_store(*_: Any, **__: Any) -> None:
        raise AssertionError("a store was built with the mode off")

    monkeypatch.setattr("trusted_router.storage.create_store", no_store)
    monkeypatch.setenv("TR_FASTPATH_ENABLER_MODE", "off")
    assert fe.main([]) == 0


def test_main_refuses_any_store_but_the_typed_spanner_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TR_FASTPATH_ENABLER_MODE", "dry-run")
    monkeypatch.setenv("TR_STORAGE_BACKEND", "memory")
    assert fe.main([]) == 1


def test_the_settings_bound_the_include_list_and_order_the_balances() -> None:
    def settings(**kwargs: Any) -> Settings:
        return Settings(_env_file=None, environment="test", **kwargs)  # type: ignore[call-arg]

    s = settings(fastpath_enabler_include="ws-synthetic-a, ws-synthetic-b", fastpath_enabler_max_rank=10)
    config = fe.EnablerConfig.from_settings(s)
    assert config.include == frozenset({"ws-synthetic-a", "ws-synthetic-b"}) and config.max_rank == 10
    assert (config.enable_balance, config.disable_balance) == (100_000_000, 40_000_000)
    with pytest.raises(ValidationError, match="include set"):
        settings(fastpath_enabler_include=",".join(f"ws-synthetic-{i}" for i in range(33)))
    with pytest.raises(ValidationError, match="DISABLE_BALANCE"):
        settings(fastpath_enabler_disable_balance_micro=100_000_000)
    with pytest.raises(ValidationError):
        settings(fastpath_enabler_mode="on")
    with pytest.raises(ValidationError):
        settings(fastpath_enabler_max_rank=-1)


def test_the_required_tier_is_a_setting_three_by_default() -> None:
    """TR_FASTPATH_ENABLER_REQUIRED_TIER is 1 to 3, 3 unless set, so the
    enabler is unchanged until a later change lowers it after the nodes'."""
    def settings(**kwargs: Any) -> Settings:
        return Settings(_env_file=None, environment="test", **kwargs)  # type: ignore[call-arg]

    assert fe.DEFAULT_REQUIRED_TIER == 3 and fe.EnablerConfig().required_tier == 3
    assert fe.EnablerConfig.from_settings(settings()).required_tier == 3
    for tier in (1, 2, 3):
        assert fe.EnablerConfig.from_settings(settings(fastpath_enabler_required_tier=tier)).required_tier == tier
    for tier in (0, 4, -1):
        with pytest.raises(ValidationError):
            settings(fastpath_enabler_required_tier=tier)


def test_the_environment_sets_the_required_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TR_FASTPATH_ENABLER_REQUIRED_TIER", "2")
    s = Settings(_env_file=None, environment="test")  # type: ignore[call-arg]
    assert fe.EnablerConfig.from_settings(s).required_tier == 2


@pytest.mark.parametrize(("required", "eligible"), [(3, {3}), (2, {2, 3}), (1, {1, 2, 3})])
def test_eligibility_follows_the_required_tier(required: int, eligible: set[int]) -> None:
    """A workspace at each tier is eligible at the configured required tier
    or above and excluded for the tier below it, in the snapshot's plan and
    in the enable's own recheck; a latched one is tier 0 at any setting."""
    db = _armed()
    for tier in (0, 1, 2, 3):
        _seed(db, f"ws-synthetic-t{tier}", tier=tier, reconciled=T0)
    _seed(db, "ws-synthetic-latched", tier=3, latched_at=T0, reconciled=T0)
    config = dataclasses.replace(BASE, required_tier=required)
    report = _run(db, config).report
    on = {w for w, (enabled, _) in db.switch.items() if enabled}
    assert on == {f"ws-synthetic-t{tier}" for tier in eligible}
    assert report["excluded"] == {"tier": 4 - len(eligible), "latched": 1}
    # The enable's own transaction rechecks at the same tier.
    fresh = _armed()
    _seed(fresh, "ws-synthetic-low", tier=min(eligible) - 1, reconciled=T0)
    outcome = fe.write_enable(fresh, param_types, "ws-synthetic-low", included=False, config=config, now=NOW,
                              operations=fe.enabler_operations(), commit_timestamp=COMMIT_TIMESTAMP)
    assert outcome.result == "ineligible:tier"


def test_an_override_at_the_required_tier_counts_as_fresh() -> None:
    """The watermark freeze reads the same setting: an override at the
    required tier or above has nothing to reconcile, one below it does."""
    rows = [fe.CreditRow(shard=0, tier=2, latched=False, paused=False, in_debt=False, credits=0, usage=0,
                         reconciled_through=None)]
    lowered = dataclasses.replace(BASE, required_tier=2)
    assert not fe.watermark_stale(rows, fe.Override(tier=2, bypass=False), now=NOW, config=lowered)
    assert fe.watermark_stale(rows, fe.Override(tier=2, bypass=False), now=NOW, config=BASE)
    assert fe.watermark_stale(rows, fe.Override(tier=1, bypass=False), now=NOW, config=lowered)
