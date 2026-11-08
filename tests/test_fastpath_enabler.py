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
    _seed(db, "ws-synthetic-x", entity=None if entity in (None, "missing") else entity, override=override, **flip)
    if entity == "missing":
        del db.entities[("workspace", "ws-synthetic-x")]
    if hold:
        db.holds["ws-synthetic-x"] = (hold, T0)
    if gap:
        db.credit["ws-synthetic-x"][0].shard = 1
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
