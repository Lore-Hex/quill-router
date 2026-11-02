"""The edge's key-window vectors are what Python's window check computes now.

fastpath/internal/keywindow is held to fastpath/testdata/key_window_vectors_v1.json;
this test holds the file to Python's own functions
(scripts/fastpath/key_window_vectors.py): a change to the window floors, to
decide_key_window_limits, to key_window_limit_decision's query or arithmetic,
or to the route's rule for when the check runs fails here until the vectors,
and with them the Go port, follow. The generator pins utcnow in its own
process, so it runs in a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "fastpath" / "testdata" / "key_window_vectors_v1.json"
GENERATOR = ROOT / "scripts" / "fastpath" / "key_window_vectors.py"


def _vectors() -> dict[str, Any]:
    return json.loads(VECTORS.read_text())


def _cases() -> dict[str, dict[str, Any]]:
    return {case["name"]: case for case in _vectors()["decide"]}


def test_the_vectors_are_what_the_python_window_check_computes_now() -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository generator, no shell or caller input
        [sys.executable, str(GENERATOR), "--print"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, cwd=ROOT,
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert VECTORS.read_text() == result.stdout, (
        "fastpath/testdata/key_window_vectors_v1.json is stale: run "
        "`uv run python scripts/fastpath/key_window_vectors.py` and check fastpath/internal/keywindow against it"
    )


def test_every_label_and_outcome_is_exercised_by_a_named_case() -> None:
    named = [case for case in _vectors()["decide"] if "random" not in case["labels"]]
    labels = Counter(label for case in named for label in case["labels"])
    for label in (
        "null_start", "null_usage", "stale_start", "start_at_floor", "multi_shard", "incomplete_shards",
        "no_rows", "zero_limit", "negative_limit", "exact_headroom", "over_by_one", "zero_estimate",
        "used_over_limit", "int64_edge", "tie", "boundary", "config_drift", "alert_only", "no_limits",
        "multi_window", "overflow", "shard_count_invalid", "dst", "year_range",
        "allow", "deny", "none", "error",
    ):
        assert labels[label] >= 1, (label, labels)
    edges = Counter(case["edge"] for case in named)
    for edge in ("decide", "no_check", "pass:key_window.rows", "pass:key_window.config_drift",
                 "pass:key_window.boundary", "pass:key_window.overflow", "pass:key_window.exceeded"):
        assert edges[edge] >= 1, (edge, edges)
    floors = Counter(label for case in _vectors()["floors"] for label in case["labels"])
    for label in ("midnight", "iso_week", "month_end", "leap_day", "year_end", "dst", "offset", "random"):
        assert floors[label] >= 1, (label, floors)


def test_the_clock_is_the_case_instant() -> None:
    # Positive control for the pinned utcnow: the same row one microsecond
    # either side of a midnight counts or does not, so the wall clock is not
    # what decided.
    cases = _cases()
    assert cases["start_at_floor_counts"]["python"]["verdict"] == "deny"
    assert cases["stale_start_one_us"]["python"]["verdict"] == "allow"
    last = cases["boundary_last_us_of_day"]
    first = cases["boundary_at_daily_floor"]
    assert first["at_us"] - last["at_us"] == 1
    assert last["python"]["reset_seconds"] == 1
    assert first["python"]["reset_seconds"] == 86_400
    # Wherever the edge decides, Python allowed.
    for case in cases.values():
        if case["edge"] == "decide":
            assert case["python"]["verdict"] == "allow", case["name"]


def test_the_edge_never_admits_what_python_refuses() -> None:
    for case in _vectors()["decide"]:
        if case["python"]["verdict"] != "allow":
            assert case["edge"] != "decide", case["name"]
        if not case["python"]["enforced"]:
            assert case["python"]["verdict"] == "none" and case["edge"] == "no_check", case["name"]


def test_alert_only_keys_are_never_checked() -> None:
    cases = _cases()
    for name in ("alert_only_daily", "alert_only_all_windows", "alert_only_no_rows"):
        python = cases[name]["python"]
        assert python["configured"] and python["enforced"] == {}, name
        assert python["verdict"] == "none" and cases[name]["edge"] == "no_check", name


def test_the_keys_are_synthetic() -> None:
    text = VECTORS.read_text()
    assert "sk-tr" not in text
    assert "kh_synthetic_window_0001" not in text  # key hashes never reach the file


def test_python_cannot_reach_year_10000_and_the_edge_passes() -> None:
    cases = _cases()
    for name in ("year_9999_december_monthly", "year_9999_last_day_daily", "year_9999_last_week_weekly"):
        assert cases[name]["python"]["verdict"] == "error", name
        assert cases[name]["python"]["error"] == "OverflowError", name
    for name in ("year_9999_january_daily", "year_1_daily"):
        assert cases[name]["python"]["verdict"] == "allow", name  # Go may pass what Python allows
    for name in ("year_9999_december_monthly", "year_9999_january_daily", "year_1_daily"):
        assert cases[name]["edge"] == "pass:key_window.overflow", name
    for name in ("year_9998_december_monthly", "year_2_daily"):
        assert cases[name]["edge"] == "decide", name


def test_the_facts_cases_cover_every_body_kind() -> None:
    facts = _vectors()["facts"]
    labels = Counter(label for case in facts for label in case["labels"])
    for label in (
        "integer", "zero", "negative", "int64_edge", "past_int64", "float", "string", "bool", "null", "none",
        "multi_window", "alert_only", "alert_only_type", "shards", "shard_count_invalid",
    ):
        assert labels[label] >= 1, (label, labels)
    outcomes = Counter("facts" if isinstance(case["go"], dict) else case["go"].split(":")[0] for case in facts)
    assert outcomes["facts"] >= 10 and outcomes["none"] >= 3 and outcomes["key_shape"] >= 10, outcomes
    assert len({case["name"] for case in facts}) == len(facts)


def test_where_go_reads_a_key_body_it_reads_python_s_windows() -> None:
    for case in _vectors()["facts"]:
        python, go = case["python"], case["go"]
        assert case["exact"] == (isinstance(go, dict) or go == "none"), case["name"]
        if not case["exact"]:
            assert go.startswith("key_shape:"), case["name"]
            continue
        assert python["error"] is None, case["name"]
        if go == "none":
            assert python["configured"] == {}, case["name"]
            continue
        configured = {w: v for w, v in go["limits"].items() if v is not None}
        assert configured == python["configured"], case["name"]
        assert python["enforced"] == ({} if go["alert_only"] else configured), case["name"]
        assert go["shards"] == python["shards"], case["name"]


def test_alert_only_keys_read_as_alert_only_and_others_pass() -> None:
    cases = {case["name"]: case for case in _vectors()["facts"]}
    assert cases["alert_only_true"]["go"]["alert_only"] is True
    assert cases["alert_only_true"]["python"]["enforced"] == {}
    # Python reads "false" as truthy and 0 as falsy; Go reads neither.
    assert cases["alert_only_string_false"]["python"]["enforced"] == {}
    assert cases["alert_only_number_zero"]["python"]["enforced"] == {"daily": 1000}
    for name in ("alert_only_string_false", "alert_only_number_zero"):
        assert cases[name]["go"] == "key_shape:budget_alert_only", name
    # Without a window budget_alert_only decides nothing, and Go does not read it.
    assert cases["no_window_alert_only_not_read"]["go"] == "none"
